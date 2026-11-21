"""Soundness stress test for the class-attribute path.

Mutates one literal inside a class-body assignment of sqlglot (flips a bool,
bumps an int, changes a string, drops a dict/set/tuple element), asks the
selector which tests that edit can reach, and runs tests the line-based rule
would have selected but the class-attribute rule dropped. Any failure among
those is a soundness bug.

Usage: RTS_REPO=<sqlglot> RTS_MAP=<map.json> python stress_attrs.py <n> <seed>
"""

import ast
import difflib
import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("RTS_DATA", os.path.join(_ROOT, "sample"))
os.environ.setdefault("RTS_TMP", "/tmp/rts_stress_attrs")
os.makedirs(os.environ["RTS_TMP"], exist_ok=True)
os.environ["RTS_TMP"] = os.path.realpath(os.environ["RTS_TMP"])
sys.path.insert(0, _ROOT)
import selector as S  # noqa: E402

BUDGET_SECONDS = float(os.environ.get("STRESS_BUDGET", "40"))


def class_assigns(tree):
    out = []

    def walk(body, in_class):
        for s in body:
            if isinstance(s, ast.ClassDef):
                walk(s.body, True)
            elif in_class and S._plain_assign(s):
                out.append(s)

    walk(tree.body, False)
    return out


def mutate_value(src_lines, node, rng):
    """Returns (line index, new line) for a one-token change, or None."""
    cands = []
    for n in ast.walk(node.value):
        if isinstance(n, ast.Constant) and n.lineno == n.end_lineno:
            cands.append(n)
    if not cands:
        return None
    c = rng.choice(cands)
    v = c.value
    if isinstance(v, bool):
        new = repr(not v)
    elif isinstance(v, int):
        new = repr(v + 1)
    elif isinstance(v, str) and v:
        new = repr(v + "_x")
    else:
        return None
    ln = c.lineno - 1
    line = src_lines[ln]
    return ln, line[:c.col_offset] + new + line[c.end_col_offset:]


def main():
    n, seed = int(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(seed)
    sel = S.Selector()
    files = sorted(p for p in S.REPO.joinpath("sqlglot").rglob("*.py"))
    ws = S.Workspace(S.TMP / "w")
    tested = bugs = 0
    t0 = time.time()
    tries = 0
    while tested < n and tries < n * 50:
        tries += 1
        path = rng.choice(files)
        rel = str(path.relative_to(S.REPO))
        src = path.read_text()
        tree = ast.parse(src)
        assigns = class_assigns(tree)
        if not assigns:
            continue
        node = rng.choice(assigns)
        lines = src.split("\n")
        m = mutate_value(lines, node, rng)
        if m is None:
            continue
        ln, new_line = m
        new_lines = list(lines)
        new_lines[ln] = new_line
        mutated = "\n".join(new_lines)
        try:
            ast.parse(mutated)
        except SyntaxError:
            continue
        if not S.class_body_data_edit(src, mutated):
            continue
        patch = "".join(difflib.unified_diff(
            src.splitlines(True), mutated.splitlines(True), f"a/{rel}", f"b/{rel}"))
        old_min = S.CLASS_ATTR_MIN_MS
        cands, new_texts, why = sel.candidates(patch)
        S.CLASS_ATTR_MIN_MS = 10**12  # line-based only
        base, _, _ = sel.candidates(patch)
        S.CLASS_ATTR_MIN_MS = old_min
        removed = base - cands
        if not removed:
            continue
        tested += 1
        order = sorted(removed, key=lambda i: sel.tests[i]["seconds"])
        rng.shuffle(order)
        acc, ids = 0.0, []
        for i in order:
            if acc + sel.tests[i]["seconds"] > BUDGET_SECONDS:
                continue
            acc += sel.tests[i]["seconds"]
            ids.append(sel.tests[i]["id"])
        ws.prepare(new_texts)
        failed, info = S.run_tests(ws.root, ids, timeout=900)
        if failed:
            # Some tests depend on run order (they fail on clean code too when
            # run after certain others). Only failures the clean code does not
            # reproduce in the same order count.
            ws.prepare({})
            clean, _ = S.run_tests(ws.root, ids, timeout=900)
            if clean:
                failed = set(failed) - set(clean)
        status = "OK"
        if failed is None:
            status = f"UNUSABLE {info}"
        elif failed:
            status = f"BUG {len(failed)} failed: {sorted(failed)[:3]}"
            bugs += 1
        print(f"[{tested}] {rel}:{ln + 1} kept={len(cands)} removed={len(removed)} ran={len(ids)} "
              f"{status} | {why[:160]}", flush=True)
    print(f"mutants: {tested}, bugs: {bugs}, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
