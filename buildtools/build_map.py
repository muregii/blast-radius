"""Build-time: run sqlglot's suite under the rtsmap plugin and write the
dependency map the selector reads.

Usage: python build_map.py <sqlglot_root> <tests.json> <out_map.json> [jobs]

The suite runs against an instrumented copy of sqlglot (instrument.py), so the
same pass records line coverage and the comparison / and-or facts used to
prove that an edit is silent for a group.

Groups:
  * every test at or above HEAVY_SECONDS runs alone, so its coverage is exact;
  * the other tests of one test file run together in one process. Inside a
    group we never trust per-test attribution (caches, setUpClass, lazy
    imports), so a line reached by the group selects every test in it.
"""

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

CHUNK_SECONDS = 6.0
HERE = os.path.dirname(os.path.abspath(__file__))


NOT_DATA = ("fn", "classmethod", "staticmethod", "property", "builtin", "class", "method", "enum")


def attr_names_from_state(state: dict) -> set[str]:
    """Names of class attributes holding data (not functions, classes or enum
    members): reads of these are recorded per test."""
    names = set()
    for k, v in state.items():
        if not k.startswith("C:"):
            continue
        name = k.split("|", 1)[1]
        if name.startswith("__") and name.endswith("__"):
            continue
        kind = v.split(":", 1)[0].split("#", 1)[0]
        if kind not in NOT_DATA:
            names.add(name)
    return names


def main() -> None:
    root, tests_json, out_path = sys.argv[1:4]
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else os.cpu_count() or 2
    root = os.path.realpath(root)
    tests = json.load(open(tests_json))["tests"]

    by_file: dict[str, list[dict]] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t)

    # Every test is its own selection unit: each one runs in a forked child
    # of a process that has collected (imported) its chunk, so what it
    # touches is recorded from the same post-collection state, without
    # caches warmed by the tests before it. A chunk is a slice of one test
    # file; lines run during collection count for every test of the chunk.
    chunks: list[list[dict]] = []
    for file, ts in sorted(by_file.items()):
        cur: list[dict] = []
        acc = 0.0
        for t in ts:
            if cur and acc + t["seconds"] > CHUNK_SECONDS:
                chunks.append(cur)
                cur, acc = [], 0.0
            cur.append(t)
            acc += t["seconds"]
        if cur:
            chunks.append(cur)
    # Longest chunks first so the pool finishes evenly.
    chunks.sort(key=lambda c: -sum(t["seconds"] + 0.05 for t in c))
    order = [t["id"] for c in chunks for t in c]
    gi_of = {tid: i for i, tid in enumerate(order)}
    groups = [{"tests": [t["id"] for t in c], "args": [t["id"] for t in c]} for c in chunks]

    tmp = tempfile.mkdtemp(prefix="rtsmap")
    instr = os.path.join(tmp, "instr")
    # Import-time state of the pinned commit (see importstate.py). The
    # selector compares a patched tree against it.
    state_path = os.path.splitext(os.path.abspath(out_path))[0] + ".state.json"
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("RTS_")}
    subprocess.run([sys.executable, os.path.join(HERE, "importstate.py"), state_path, root],
                   check=True, env={**clean_env, "PYTHONHASHSEED": "0", "PYTHONPATH": root,
                                    "PYTHONDONTWRITEBYTECODE": "1"}, cwd=tmp)
    state = json.load(open(state_path))["state"]
    attr_names = sorted(attr_names_from_state(state))
    names_path = os.path.join(tmp, "attr_names.json")
    with open(names_path, "w") as f:
        json.dump(attr_names, f)
    subprocess.run([sys.executable, os.path.join(HERE, "instrument.py"), root, instr, names_path],
                   check=True)
    sites = json.load(open(os.path.join(instr, "rts_sites.json")))
    run_root = instr
    env = dict(os.environ)
    env.update(
        SKIP_INTEGRATION="0",
        RTS_RECORD="1",
        RTS_FORK="1",
        RTS_ROOT=run_root,
        PYTHONPATH=os.pathsep.join([run_root, HERE]),
        PYTHONDONTWRITEBYTECODE="1",
    )

    def run(i: int) -> dict:
        out = os.path.join(tmp, f"{i}.json")
        argfile = os.path.join(tmp, f"{i}.args")
        with open(argfile, "w") as f:
            f.write("\n".join(groups[i]["args"]))
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "rtsmap_plugin",
               "-p", "no:cacheprovider", "-o", "addopts=", f"@{argfile}"]
        proc = subprocess.run(cmd, cwd=run_root, env={**env, "RTS_OUT": out},
                              capture_output=True, text=True)
        if not os.path.exists(out):
            sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-3000:])
            raise SystemExit(f"group {i} produced no map")
        res = json.load(open(out))
        missing = [t for t in groups[i]["tests"] if t not in res["outcomes"]]
        if missing:
            raise SystemExit(f"group {i}: tests did not run: {missing[:5]}")
        return res

    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(run, range(len(groups))))

    # Interned test sets keep the map small: most lines share their set.
    set_ids: dict[tuple, int] = {}
    sets: list[list[int]] = []

    def intern(gis) -> int:
        key = tuple(sorted(gis))
        i = set_ids.get(key)
        if i is None:
            i = set_ids[key] = len(sets)
            sets.append(list(key))
        return i

    lines: dict[str, dict[int, set[int]]] = {}
    opens: dict[str, set[int]] = {}
    attrs: dict[str, set[int]] = {}
    failed_baseline: list[str] = []
    facts = {"cmp": {}, "vals": {}, "truth": {}}  # site -> {facts json: set(gi)}
    incomplete: list[int] = []

    def add_facts(gis, cmp_, vals_, truth_, acc):
        for kind, d in (("cmp", cmp_), ("vals", vals_), ("truth", truth_)):
            for k, v in d.items():
                if kind == "vals":
                    if v == "overflow":
                        acc[kind].setdefault(k, "overflow")
                        acc[kind][k] = "overflow"
                        continue
                    if acc[kind].get(k) == "overflow":
                        continue
                    cur = acc[kind].setdefault(k, set())
                    cur.update(json.dumps(x) for x in v)
                else:
                    acc[kind].setdefault(k, set()).update(json.dumps(x) for x in v)

    for ci, res in enumerate(results):
        chunk_gis = [gi_of[t] for t in groups[ci]["tests"]]
        failed_baseline += [n for n, o in res["outcomes"].items() if o == "failed"]
        # Collection-time lines and opens: every test of the chunk.
        for f, ls in res["lines"].get("", {}).items():
            per = lines.setdefault(f, {})
            for ln in ls:
                per.setdefault(ln, set()).update(chunk_gis)
        for p in res["opens"].get("", []):
            opens.setdefault(p, set()).update(chunk_gis)
        for a in res.get("attrs", []):
            attrs.setdefault(a, set()).update(chunk_gis)
        if res.get("incomplete_children"):
            incomplete += chunk_gis
        for tid in groups[ci]["tests"]:
            gi = gi_of[tid]
            rec = res["per_test"].get(tid)
            if rec is None or rec.get("lost") or rec.get("incomplete"):
                incomplete.append(gi)
                if rec is None or rec.get("lost"):
                    continue
            for f, ls in rec["lines"].items():
                per = lines.setdefault(f, {})
                for ln in ls:
                    per.setdefault(ln, set()).add(gi)
            for p in rec["opens"]:
                opens.setdefault(p, set()).add(gi)
            for a in rec.get("attrs", []):
                attrs.setdefault(a, set()).add(gi)
            acc = {"cmp": {}, "vals": {}, "truth": {}}
            add_facts([gi], *rec["own"], acc)
            for c in rec["pool"]:
                add_facts([gi], *c, acc)
            for kind, d in acc.items():
                for k, v in d.items():
                    key = v if v == "overflow" else json.dumps(sorted(v))
                    facts[kind].setdefault(k, {}).setdefault(key, set()).add(gi)

    def pack(kind):
        out = {}
        for k, by in facts[kind].items():
            out[k] = [[v if v == "overflow" else [json.loads(x) for x in json.loads(v)], intern(g)]
                      for v, g in by.items()]
        return out

    out = {
        "format": 3,
        "groups": [[tid] for tid in order],
        "sets": None,
        "lines": {f: {str(k): intern(v) for k, v in per.items()} for f, per in lines.items()},
        "opens": {p: intern(v) for p, v in opens.items()},
        "attrs": {a: intern(v) for a, v in attrs.items()},
        "attr_names": attr_names,
        "failed_baseline": failed_baseline,
        "sites": sites,
        "cmp": pack("cmp"),
        "vals": pack("vals"),
        "truth": pack("truth"),
        "incomplete": sorted(set(incomplete)),
    }
    out["sets"] = sets
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"map: {len(order)} tests in {len(groups)} chunks, {len(lines)} files, "
          f"{len(opens)} data files, {len(out['cmp'])} cmp sites, {len(out['truth'])} bool sites, "
          f"{len(sets)} test sets, {len(attrs)} attrs read ({len(attr_names)} recorded names), incomplete groups {out['incomplete']}, "
          f"baseline failures {failed_baseline}", file=sys.stderr)


if __name__ == "__main__":
    main()
