"""Soundness stress test for edits to test modules.

Changes one constant inside a method of a test class (a test method or a
helper such as setUp / validate_identity), asks the selector which tests the
edit reaches, and runs the tests of that file (and of the files importing it)
that the selector dropped. Any failure the clean code does not also show in
the same order is a soundness bug.

Usage: RTS_REPO=<sqlglot> RTS_MAP=<map.json> python stress_tests.py <n> <seed>
"""

import ast
import difflib
import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("RTS_DATA", os.path.join(_ROOT, "sample"))
os.environ.setdefault("RTS_TMP", "/tmp/rts_stress_tests")
os.makedirs(os.environ["RTS_TMP"], exist_ok=True)
os.environ["RTS_TMP"] = os.path.realpath(os.environ["RTS_TMP"])
sys.path.insert(0, _ROOT)
import selector as S  # noqa: E402

BUDGET_SECONDS = float(os.environ.get("STRESS_BUDGET", "60"))


def constants_in_methods(tree):
    out = []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Constant) and n.lineno == n.end_lineno
                            and isinstance(n.value, (int, str)) and not isinstance(n.value, bool)
                            and n.lineno > fn.lineno):
                        out.append(n)
    return out


def main():
    n, seed = int(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(seed)
    sel = S.Selector()
    files = sorted({t["file"] for t in sel.tests})
    ws = S.PatchedTree(S.TMP / "w")
    tested = bugs = 0
    t0 = time.time()
    tries = 0
    while tested < n and tries < n * 50:
        tries += 1
        rel = rng.choice(files)
        src = (S.REPO / rel).read_text()
        consts = constants_in_methods(ast.parse(src))
        if not consts:
            continue
        c = rng.choice(consts)
        new = repr(c.value + 1) if isinstance(c.value, int) else repr(c.value + "_x")
        lines = src.split("\n")
        line = lines[c.lineno - 1]
        lines[c.lineno - 1] = line[:c.col_offset] + new + line[c.end_col_offset:]
        mutated = "\n".join(lines)
        try:
            ast.parse(mutated)
        except SyntaxError:
            continue
        patch = "".join(difflib.unified_diff(
            src.splitlines(True), mutated.splitlines(True), f"a/{rel}", f"b/{rel}"))
        cands, new_texts, why = sel.candidates(patch)
        scope = sel.test_importers(rel) | {rel}
        base = {t["number"] for t in sel.tests if t["file"] in scope}
        removed = base - cands
        if not removed:
            continue
        tested += 1
        order = sorted(removed)
        rng.shuffle(order)
        acc, ids = 0.0, []
        for i in order:
            if acc + sel.tests[i]["seconds"] > BUDGET_SECONDS:
                continue
            acc += sel.tests[i]["seconds"]
            ids.append(sel.tests[i]["id"])
        ws.apply(new_texts)
        failed, info = ws.run(ids, timeout=900)
        if failed:
            ws.apply({})
            clean, _ = ws.run(ids, timeout=900)
            if clean:
                failed = set(failed) - set(clean)
        status = "OK"
        if failed is None:
            status = f"UNUSABLE {info}"
        elif failed:
            status = f"BUG {len(failed)} failed: {sorted(failed)[:3]}"
            bugs += 1
        print(f"[{tested}] {rel}:{c.lineno} kept={len(cands)} removed={len(removed)} ran={len(ids)} "
              f"{status} | {why[:140]}", flush=True)
    print(f"mutants: {tested}, bugs: {bugs}, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
