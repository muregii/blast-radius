"""Build-time: run sqlglot's suite under the rtsmap plugin and write the
dependency map the selector reads.

Usage: python build_map.py <sqlglot_root> <tests.json> <out_map.json> [jobs]

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

HEAVY_SECONDS = 0.3
HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    root, tests_json, out_path = sys.argv[1:4]
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else os.cpu_count() or 2
    root = os.path.realpath(root)
    tests = json.load(open(tests_json))["tests"]

    by_file: dict[str, list[dict]] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t)

    groups: list[dict] = []  # {"tests": [numbers], "args": [...]}
    for file, ts in sorted(by_file.items()):
        light = [t for t in ts if t["seconds"] < HEAVY_SECONDS]
        heavy = [t for t in ts if t["seconds"] >= HEAVY_SECONDS]
        if light:
            # Exact node ids: --deselect matches by prefix and would drop
            # test_foo__bar along with test_foo.
            groups.append({"tests": [t["id"] for t in light], "args": [t["id"] for t in light]})
        for t in heavy:
            groups.append({"tests": [t["id"]], "args": [t["id"]]})

    tmp = tempfile.mkdtemp(prefix="rtsmap")
    env = dict(os.environ)
    env.update(
        SKIP_INTEGRATION="0",
        RTS_ROOT=root,
        PYTHONPATH=os.pathsep.join([root, HERE]),
        PYTHONDONTWRITEBYTECODE="1",
    )

    def run(i: int) -> dict:
        out = os.path.join(tmp, f"{i}.json")
        argfile = os.path.join(tmp, f"{i}.args")
        with open(argfile, "w") as f:
            f.write("\n".join(groups[i]["args"]))
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "rtsmap_plugin",
               "-p", "no:cacheprovider", "-o", "addopts=", f"@{argfile}"]
        proc = subprocess.run(cmd, cwd=root, env={**env, "RTS_OUT": out},
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

    lines: dict[str, dict[int, list[int]]] = {}
    opens: dict[str, list[int]] = {}
    failed_baseline: list[str] = []
    for gi, res in enumerate(results):
        seen: dict[str, set[int]] = {}
        for files in res["lines"].values():
            for f, ls in files.items():
                seen.setdefault(f, set()).update(ls)
        for f, ls in seen.items():
            per = lines.setdefault(f, {})
            for ln in ls:
                per.setdefault(ln, []).append(gi)
        for paths in res["opens"].values():
            for p in paths:
                lst = opens.setdefault(p, [])
                if not lst or lst[-1] != gi:
                    lst.append(gi)
        failed_baseline += [n for n, o in res["outcomes"].items() if o == "failed"]

    out = {
        "groups": [g["tests"] for g in groups],
        "lines": {f: {str(k): v for k, v in per.items()} for f, per in lines.items()},
        "opens": opens,
        "failed_baseline": failed_baseline,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"map: {len(groups)} groups, {len(lines)} files, "
          f"{len(opens)} data files, baseline failures {failed_baseline}", file=sys.stderr)


if __name__ == "__main__":
    main()
