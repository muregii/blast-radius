"""Cost benchmark: one-line mutants shaped like the sample commits.

Writes <out>/commits/<id>.patch plus <out>/tests.json so `RTS_DATA=<out>
python selector.py` runs on them like on the sample. No answers: this measures
cost (selection size, fallbacks), not misses; soundness stays with stress*.py.

Operators copy the sample: comparison swaps, and/or swaps, dropping `not`,
True/False flips, int literal +-1, added comment lines (code); a token
inserted into or deleted from a SQL line (fixture); 2-3 of those in different
files (multi). Mix follows the sample: 16 code, 8 fixture, 3 multi, 3 comment.

Usage: python devtools/mutants.py <sqlglot repo> <out dir> <n> <seed>
"""

import ast
import difflib
import io
import json
import random
import shutil
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CMP = {"is": ["=="], "==": ["!=", "is"], "!=": ["=="], "<": ["<="], "<=": ["<"],
       ">": [">="], ">=": [">"], "and": ["or"], "or": ["and"]}
COMMENTS = ["# Intentionally not cached", "# Cheap check first",
            "# Keep in sync with the generator", "# Kept for backwards compatibility",
            "# Mirrors the behavior of the reference implementation"]
SQL_INSERTS = ["1", "0", "42", "x", "NULL", "xa"]
KINDS = ["code"] * 16 + ["fixture"] * 8 + ["multi"] * 3 + ["comment"] * 3


def py_sites(text: str):
    """(line index, col, end col, replacement) for every token mutation."""
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return out
    for i, t in enumerate(toks):
        (r, c), (_, ec) = t.start, t.end
        if t.type == tokenize.OP and t.string in CMP:
            out += [(r - 1, c, ec, n) for n in CMP[t.string]]
        elif t.type == tokenize.NAME and t.string in ("is", "and", "or"):
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if t.string == "is" and nxt is not None and nxt.string == "not":
                out.append((r - 1, c, nxt.end[1], "!="))
            else:
                out += [(r - 1, c, ec, n) for n in CMP[t.string]]
        elif t.type == tokenize.NAME and t.string in ("True", "False"):
            out.append((r - 1, c, ec, "False" if t.string == "True" else "True"))
        elif t.type == tokenize.NAME and t.string == "not":
            prev = toks[i - 1].string if i else ""
            if prev not in ("is", "in") and (i + 1 < len(toks) and toks[i + 1].string != "in"):
                out.append((r - 1, c, toks[i + 1].start[1], ""))
        elif t.type == tokenize.NUMBER and t.string.isdigit():
            v = int(t.string)
            out.append((r - 1, c, ec, str(v + 1 if v == 0 or random.random() < 0.5 else v - 1)))
    return out


def mutate_code(rng, text):
    sites = py_sites(text)
    rng.shuffle(sites)
    lines = text.split("\n")
    for ln, c, ec, new in sites[:20]:
        if lines[ln] != text.split("\n")[ln]:
            continue
        cand = lines[:]
        cand[ln] = cand[ln][:c] + new + cand[ln][ec:]
        new_text = "\n".join(cand)
        try:
            if ast.dump(ast.parse(new_text)) != ast.dump(ast.parse(text)):
                return new_text
        except SyntaxError:
            continue
    return None


def mutate_comment(rng, text):
    lines = text.split("\n")
    for _ in range(20):
        i = rng.randrange(1, len(lines))
        prev = lines[i - 1]
        if prev.rstrip().endswith(("(", "[", "{", ",", "\\")) or '"""' in prev:
            continue
        indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())] if lines[i].strip() else ""
        cand = lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:]
        new_text = "\n".join(cand)
        try:
            if ast.dump(ast.parse(new_text)) == ast.dump(ast.parse(text)):
                return new_text
        except SyntaxError:
            continue
    return None


def mutate_fixture(rng, text):
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith(("#", "--"))]
    if not idx:
        return None
    i = rng.choice(idx)
    words = lines[i].split(" ")
    if rng.random() < 0.7 or len(words) < 2:
        words.insert(rng.randrange(len(words) + 1), rng.choice(SQL_INSERTS))
    else:
        words.pop(rng.randrange(len(words)))
    lines[i] = " ".join(words)
    return "\n".join(lines)


def diff(rel, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{rel}", f"b/{rel}", n=3))
    return f"diff --git a/{rel} b/{rel}\n" + body + ("" if body.endswith("\n") else "\n")


def main():
    repo, out, n, seed = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    rng = random.Random(seed)
    random.seed(seed)
    pys = sorted(p.relative_to(repo).as_posix() for p in (repo / "sqlglot").rglob("*.py"))
    sqls = sorted(p.relative_to(repo).as_posix() for p in (repo / "tests/fixtures").rglob("*.sql"))
    if out.exists():
        shutil.rmtree(out)
    (out / "commits").mkdir(parents=True)
    shutil.copy(ROOT / "sample/tests.json", out / "tests.json")
    kinds = {}

    def one(kind):
        for _ in range(50):
            rel = rng.choice(sqls if kind == "fixture" else pys)
            old = (repo / rel).read_text()
            new = {"code": mutate_code, "comment": mutate_comment,
                   "fixture": mutate_fixture}[kind](rng, old)
            if new is not None and new != old:
                return rel, old, new
        raise RuntimeError(kind)

    for k in range(n):
        kind = rng.choice(KINDS)
        if kind == "multi":
            parts, seen = [], set()
            while len(parts) < rng.choice([2, 2, 3]):
                p = one(rng.choice(["code", "code", "fixture", "comment"]))
                if p[0] not in seen:
                    seen.add(p[0])
                    parts.append(p)
        else:
            parts = [one(kind)]
        cid = f"m{seed}_{k:04d}"
        (out / "commits" / f"{cid}.patch").write_text("".join(diff(*p) for p in sorted(parts)))
        kinds[cid] = kind
    (out / "kinds.json").write_text(json.dumps(kinds, indent=0))
    print(f"wrote {n} mutants to {out}")


if __name__ == "__main__":
    main()
