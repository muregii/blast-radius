"""End-to-end miss check: full suite on mutants vs what the selector printed.

For each mutant of a bench dir (devtools/mutants.py) whose printed selection
is not the whole suite, runs every test on the patched tree and reports any
test that fails there, passes on clean code in the same order, and was not
printed. Covers every way to miss (coverage holes, prover bugs, hash order),
not just the prover.

Usage: RTS_REPO=<sqlglot> RTS_MAP=<map> RTS_DATA=<bench> \
       python devtools/miss_check.py <selector output.jsonl> <n> <seed>
"""

import json
import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("RTS_TMP", "/tmp/rts_miss")
os.makedirs(os.environ["RTS_TMP"], exist_ok=True)
os.environ["RTS_TMP"] = os.path.realpath(os.environ["RTS_TMP"])
sys.path.insert(0, _ROOT)
import selector as S  # noqa: E402


def main():
    out, n, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    sel = S.Selector()
    printed = {}
    for line in open(out):
        o = json.loads(line)
        printed[o["commit"]] = {i for w in o["workers"] for i in w}
    total = len(sel.tests)
    heavy = {t["number"] for t in sel.tests if t["seconds"] >= 20}
    pool = [c for c, p in printed.items() if len(p) < 0.95 * total]
    random.Random(seed).shuffle(pool)
    # Half the budget on commits that print a heavy test: they hold most of
    # the cost, so they show how much a better proof could still save.
    hp = [c for c in pool if printed[c] & heavy][: n // 2]
    pool = hp + [c for c in pool if c not in hp]
    ws = S.PatchedTree(S.TMP / "m")
    ids = [t["id"] for t in sel.tests]
    t0 = time.time()
    ws.apply({})
    clean, info = ws.run(ids, timeout=1800)
    if clean is None:
        print(f"clean run unusable: {info}")
        return
    print(f"clean full run: {len(clean)} failing ({time.time() - t0:.0f}s)", flush=True)
    checked = missed = 0
    for cid in pool[:n]:
        patch = (S.DATA / "commits" / f"{cid}.patch").read_text()
        _, new_texts, why = sel.candidates(patch)
        ws.apply(new_texts)
        failed, info = ws.run(ids, timeout=1800)
        if failed is None:
            print(f"{cid}: UNUSABLE {info}", flush=True)
            continue
        checked += 1
        failing = {sel.by_id[i]["number"] for i in failed - clean}
        miss = failing - printed[cid]
        status = "OK" if not miss else f"MISS {len(miss)}: {[sel.tests[i]['id'] for i in sorted(miss)][:3]}"
        missed += bool(miss)
        # Heavy tests printed but not failing: the cost a better proof could save.
        hv = f" heavy printed={len(printed[cid] & heavy)} failing={len(failing & heavy)}"
        print(f"{cid}: failing={len(failing)} printed={len(printed[cid])}{hv} {status} | {why}"
              f" [{time.time() - t0:.0f}s]", flush=True)
    print(f"checked {checked} mutants, missed {missed}, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
