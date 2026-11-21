"""Build time only: make a recording copy of sqlglot.

Usage: python instrument.py <repo> <dest>

Every single-operator comparison `a OP b` (OP in < <= > >= == != is, is not)
becomes `_rts_c(SITE, a, b, OPCODE)`, and every operand of `x and y` /
`x or y` becomes `_rts_v(SITE, i, operand)`. Both helpers (installed into
builtins by sitecustomize.py) evaluate exactly what the original code did, in
the same order, and record what they saw. Line and column positions are kept,
so coverage line numbers are unchanged.

Writes <dest>/rts_sites.json: site index -> "path:line:col:end_line:end_col:kind".
"""

import ast
import importlib.util
import json
import marshal
import os
import shutil
import sys

OPCODES = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3, ast.Eq: 4, ast.NotEq: 5,
           ast.Is: 6, ast.IsNot: 7}


def site_key(rel: str, node: ast.AST, kind: str) -> str:
    return f"{rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}:{kind}"


class Instrument(ast.NodeTransformer):
    def __init__(self, rel: str, sites: list[str], attr_names: frozenset = frozenset()):
        self.rel = rel
        self.sites = sites
        # Class attributes whose reads are recorded: `x.NAME` becomes
        # `_rts_a(x, "NAME")`, and getattr/hasattr with a constant NAME go
        # through _rts_getattr/_rts_hasattr.
        self.attr_names = attr_names

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in self.attr_names:
            call = ast.Call(
                func=ast.Name(id="_rts_a", ctx=ast.Load()),
                args=[node.value, ast.Constant(node.attr)],
                keywords=[],
            )
            return ast.copy_location(call, node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        f = node.func
        if (isinstance(f, ast.Name) and f.id in ("getattr", "hasattr") and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in self.attr_names
                and not node.keywords):
            node.func = ast.copy_location(ast.Name(id=f"_rts_{f.id}", ctx=ast.Load()), f)
        return node

    def _site(self, node: ast.AST, kind: str) -> int:
        self.sites.append(site_key(self.rel, node, kind))
        return len(self.sites) - 1

    # Annotations are never evaluated at run time in a way we care about and
    # rewriting them could change typing behaviour: leave them alone.
    def visit_arg(self, node):
        return node

    def visit_AnnAssign(self, node):
        if node.value is not None:
            node.value = self.visit(node.value)
        return node

    def visit_FunctionDef(self, node):
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.args = self.visit_arguments_defaults(node.args)
        node.body = [self.visit(s) for s in node.body]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arguments_defaults(self, args: ast.arguments) -> ast.arguments:
        args.defaults = [self.visit(d) for d in args.defaults]
        args.kw_defaults = [self.visit(d) if d is not None else None for d in args.kw_defaults]
        return args

    def visit_Lambda(self, node):
        node.args = self.visit_arguments_defaults(node.args)
        node.body = self.visit(node.body)
        return node

    def visit_Compare(self, node):
        site = self._site(node, "c") if len(node.ops) == 1 and type(node.ops[0]) in OPCODES else None
        self.generic_visit(node)
        if site is None:
            return node
        call = ast.Call(
            func=ast.Name(id="_rts_c", ctx=ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0],
                  ast.Constant(OPCODES[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)

    def visit_BoolOp(self, node):
        site = self._site(node, "b")
        self.generic_visit(node)
        new_values = []
        for i, v in enumerate(node.values):
            call = ast.Call(
                func=ast.Name(id="_rts_v", ctx=ast.Load()),
                args=[ast.Constant(site), ast.Constant(i), v],
                keywords=[],
            )
            new_values.append(ast.copy_location(call, v))
        node.values = new_values
        return node


def main() -> None:
    repo, dest = sys.argv[1], sys.argv[2]
    attr_names: frozenset = frozenset()
    if len(sys.argv) > 3:
        with open(sys.argv[3]) as f:
            attr_names = frozenset(json.load(f))
    if os.path.exists(dest):
        shutil.rmtree(dest)
    # Everything except sqlglot/ is copied as is (tests, sqlglotc/, setup
    # files): some tests exercise files outside the package.
    shutil.copytree(repo, dest, ignore=lambda d, names: [
        n for n in names
        if (os.path.realpath(d) == os.path.realpath(repo) and n in ("sqlglot", ".git"))
        or n == "__pycache__"
    ])
    sites: list[str] = []
    src_root = os.path.join(repo, "sqlglot")
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        rel_dir = os.path.relpath(dirpath, repo)
        os.makedirs(os.path.join(dest, rel_dir), exist_ok=True)
        for name in filenames:
            src = os.path.join(dirpath, name)
            rel = os.path.join(rel_dir, name)
            out = os.path.join(dest, rel)
            if not name.endswith(".py"):
                shutil.copy2(src, out)
                continue
            text = open(src, encoding="utf-8").read()
            tree = ast.parse(text, filename=rel)
            tree = Instrument(rel, sites, attr_names).visit(tree)
            ast.fix_missing_locations(tree)
            # The source file is copied unchanged and the instrumented code
            # goes into an unchecked hash-based .pyc next to it. Python loads
            # that .pyc without comparing it to the source, so the recording
            # code runs while tracebacks and inspect.getsource still show the
            # original text.
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
            codeobj = compile(tree, out, "exec", dont_inherit=True)
            cache = importlib.util.cache_from_source(out)
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            # Unchecked hash-based pyc: Python loads it without comparing it
            # to the source, so the instrumented code runs while tracebacks,
            # line numbers and inspect.getsource still see the original text.
            data = bytearray(importlib.util.MAGIC_NUMBER)
            data += (0b01).to_bytes(4, "little")  # flags: hash-based, unchecked
            data += importlib.util.source_hash(text.encode("utf-8"))
            data += marshal.dumps(codeobj)
            with open(cache, "wb") as f:
                f.write(data)
    with open(os.path.join(dest, "rts_sites.json"), "w") as f:
        json.dump(sites, f)
    print(f"instrumented {len(sites)} sites -> {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
