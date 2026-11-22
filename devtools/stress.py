"""Soundness stress test for the silent-mutation prover.

Generates single-token mutants (comparison operator swaps, and/or swaps) at
sites the build recorded, asks the selector which test groups it proves
silent, then runs those groups' tests against the mutated code. Any failure in
a proven-silent group is a soundness bug.

Usage: python stress.py <n_mutants> <seed>
"""

import ast
import difflib
import json
import os
import random
import sys
import time

# Needs RTS_REPO (sqlglot checkout at the pinned commit) and RTS_MAP (a map
# built by buildtools/build_map.py) in the environment.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("RTS_DATA", os.path.join(_ROOT, "sample"))
os.environ.setdefault("RTS_TMP", "/tmp/rts_stress")
# pytest resolves symlinks (macOS /tmp and /var are links): a workspace behind
# one gets node ids that never match, and every test reads as failed.
os.makedirs(os.environ["RTS_TMP"], exist_ok=True)
os.environ["RTS_TMP"] = os.path.realpath(os.environ["RTS_TMP"])
sys.path.insert(0, _ROOT)
import selector as S  # noqa: E402

SWAPS = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "is": "==", "is not": "!=",
         "==": "is", "!=": "is not"}
OPTEXT = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==",
          ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not"}
BUDGET_SECONDS = float(os.environ.get("STRESS_BUDGET", "40"))
# STRESS_KIND=literal changes a literal operand of a comparison instead of
# swapping the operator (the prover's literal-change rule).
LITERAL = os.environ.get("STRESS_KIND") == "literal"


def make_literal_mutant(src: str, node, rng) -> str | None:
    if len(node.comparators) != 1:
        return None
    lits = [x for x in (node.left, node.comparators[0])
            if isinstance(x, ast.Constant) and type(x.value) in (int, str)
            and x.lineno == x.end_lineno]
    if not lits:
        return None
    x = rng.choice(lits)
    v = x.value
    if isinstance(v, int):
        new = repr(rng.choice([v + 1, v - 1, 0 if v else 1]))
    else:
        new = repr(rng.choice([v + "x", v.upper() if v != v.upper() else v.lower(), ""]))
    lines = src.split("\n")
    ln = x.lineno - 1
    lines[ln] = lines[ln][:x.col_offset] + new + lines[ln][x.end_col_offset:]
    return "\n".join(lines)


def make_mutant(src: str, kind: str, node) -> str | None:
    lines = src.split("\n")
    if kind == "c":
        a, b = node.left, node.comparators[0]
        if a.end_lineno != b.lineno:
            return None
        ln = a.end_lineno - 1
        seg = lines[ln][a.end_col_offset:b.col_offset]
        old = OPTEXT[type(node.ops[0])]
        if seg.strip() != old:
            return None
        new = SWAPS[old]
        lines[ln] = lines[ln][:a.end_col_offset] + seg.replace(old, new) + lines[ln][b.col_offset:]
    else:
        x, y = node.values[0], node.values[1]
        if x.end_lineno != y.lineno:
            return None
        ln = x.end_lineno - 1
        seg = lines[ln][x.end_col_offset:y.col_offset]
        old = "and" if isinstance(node.op, ast.And) else "or"
        if seg.strip() != old:
            return None
        lines[ln] = lines[ln][:x.end_col_offset] + seg.replace(old, "or" if old == "and" else "and") + lines[ln][y.col_offset:]
    return "\n".join(lines)


def main():
    n, seed = int(sys.argv[1]), int(sys.argv[2])
    rng = random.Random(seed)
    sel = S.Selector()
    raw = json.loads(S.MAP.read_text())
    sites = raw["sites"]
    recorded = [i for i in range(len(sites))
                if str(i) in raw["cmp"] or str(i) in raw["truth"]]
    rng.shuffle(recorded)
    ws = S.PatchedTree(S.TMP / "w")
    tested = claims = bugs = 0
    t0 = time.time()
    for idx in recorded:
        if tested >= n:
            break
        rel, line, col, eline, ecol, kind = sites[idx].rsplit(":", 5)
        src = (S.REPO / rel).read_text()
        tree = ast.parse(src)
        node = next((x for x in ast.walk(tree)
                     if isinstance(x, ast.Compare if kind == "c" else ast.BoolOp)
                     and (x.lineno, x.col_offset, x.end_lineno, x.end_col_offset)
                     == (int(line), int(col), int(eline), int(ecol))), None)
        if node is None or (kind == "c" and type(node.ops[0]) not in OPTEXT):
            continue
        if LITERAL and kind != "c":
            continue
        mutated = make_literal_mutant(src, node, rng) if LITERAL else make_mutant(src, kind, node)
        if mutated is None or mutated == src:
            continue
        try:
            ast.parse(mutated)
        except SyntaxError:
            continue
        patch = "".join(difflib.unified_diff(
            src.splitlines(True), mutated.splitlines(True), f"a/{rel}", f"b/{rel}"))
        sel.silent_log.clear()
        # Candidates with and without the prover.
        cands, new_texts, why = sel.candidates(patch)
        prover = sel.prover
        sel.prover = type("Off", (), {"prove": lambda *a, **k: (set(), "off")})()
        base, _, _ = sel.candidates(patch)
        sel.prover = prover
        removed = base - cands
        if not removed:
            continue
        tested += 1
        claims += len(removed)
        ids = [sel.tests[i]["id"] for i in sorted(removed)]
        secs = sum(sel.tests[i]["seconds"] for i in removed)
        if secs > BUDGET_SECONDS:
            # Keep the run affordable: test the cheapest claims only.
            ids = [sel.tests[i]["id"] for i in sorted(removed, key=lambda i: sel.tests[i]["seconds"])]
            acc, keep = 0.0, []
            for i in ids:
                acc += sel.by_id[i]["seconds"]
                if acc > BUDGET_SECONDS:
                    break
                keep.append(i)
            ids = keep
        ws.apply(new_texts)
        failed, info = ws.run(ids, timeout=600)
        if failed:
            # Some tests depend on run order (they fail on clean code too when
            # run after certain others). Only failures the clean code does not
            # reproduce in the same order count.
            ws.apply({})
            clean, _ = ws.run(ids, timeout=600)
            if clean:
                failed = set(failed) - set(clean)
        status = "OK"
        if failed is None:
            status = f"UNUSABLE {info}"
        elif failed:
            status = f"BUG {len(failed)} failed: {sorted(failed)[:3]}"
            bugs += 1
        print(f"[{tested}] {sites[idx]} removed={len(removed)} ran={len(ids)} {status} | {why}",
              flush=True)
    print(f"mutants with claims: {tested}, tests claimed silent: {claims}, bugs: {bugs}, "
          f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
