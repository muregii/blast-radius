"""Cost report for a selector run on a mutant benchmark (no answers needed).

Usage: python devtools/bench_cost.py <bench dir> <output.jsonl> [selector stderr]
Prints total, cost by kind, the 15 dearest commits and, given the selector's
stderr, cost grouped by the reason the selector logged for each file.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

bench, outp = Path(sys.argv[1]), Path(sys.argv[2])
ms = {t["number"]: round(t["seconds"] * 1000) for t in json.loads((bench / "tests.json").read_text())["tests"]}
kinds = json.loads((bench / "kinds.json").read_text())
cost = {}
for line in outp.read_text().splitlines():
    o = json.loads(line)
    w = [x for x in o["workers"] if x]
    cost[o["commit"]] = (max(sum(ms[i] for i in x) for x in w) + 2000 * len(w)) / 1000 if w else 0.0
missing = set(kinds) - set(cost)
n = len(cost)
print(f"commits {n}  total {sum(cost.values()):.1f}s  per commit {sum(cost.values()) / max(n, 1):.2f}s"
      f"  (sample-sized x30: {30 * sum(cost.values()) / max(n, 1):.1f}s)" + (f"  MISSING {len(missing)}" if missing else ""))
by = defaultdict(list)
for c, v in cost.items():
    by[kinds[c]].append(v)
for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
    print(f"  {k:8s} n={len(v):4d} total={sum(v):8.1f} mean={sum(v) / len(v):6.2f} zero={sum(1 for x in v if x == 0)}")
if len(sys.argv) > 3:
    reason = defaultdict(lambda: [0, 0.0])
    for line in Path(sys.argv[3]).read_text().splitlines():
        m = re.match(r"(\S+): (.*?) \| cands=", line)
        if not m or m.group(1) not in cost:
            continue
        for part in m.group(2).split("; "):
            r = part.split(": ", 1)[-1]
            r = re.sub(r"\d+/\d+", "N/N", re.sub(r"-> \d+", "", r)).strip()
            reason[r][0] += 1
            reason[r][1] += cost[m.group(1)]
    print("by logged reason (commit cost counted once per reason it shows):")
    for r, (k, s) in sorted(reason.items(), key=lambda kv: -kv[1][1])[:20]:
        print(f"  {s:8.1f}s  n={k:4d}  {r[:110]}")
print("dearest:")
for c, v in sorted(cost.items(), key=lambda kv: -kv[1])[:15]:
    print(f"  {v:6.1f}  {c}  {kinds[c]}")
