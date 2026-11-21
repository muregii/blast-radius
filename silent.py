"""Silent-mutation prover.

A group of tests runs in one process at build time. If, in that process, every
evaluation of the edited expression gives the same value under the old and the
new code, the process runs identically, so no test in the group can change
outcome. The build recorded, per group and per expression site:

  comparisons `a OP b`: facts (a is b, a == b, order of a and b), and the
                        operand values when they are small literals;
  `x and y` / `x or y`: the truth value of every evaluated operand.

Facts exist only for types whose comparisons and truth tests run no user code;
anything else is recorded as opaque "X" and blocks a proof.

prove(old_tree, new_tree, ...) returns the set of groups proven silent for an
edit, or an empty set when it cannot prove anything. It never adds tests.
"""

from __future__ import annotations

import ast

# operator index -> outcome from a fact (ident, eq, order)
LT, LE, GT, GE, EQ, NE, IS, ISNOT = range(8)
OPCODES = {ast.Lt: LT, ast.LtE: LE, ast.Gt: GT, ast.GtE: GE, ast.Eq: EQ, ast.NotEq: NE,
           ast.Is: IS, ast.IsNot: ISNOT}
_PY_OPS = {
    LT: lambda a, b: a < b, LE: lambda a, b: a <= b, GT: lambda a, b: a > b,
    GE: lambda a, b: a >= b, EQ: lambda a, b: a == b, NE: lambda a, b: a != b,
    IS: lambda a, b: a is b, ISNOT: lambda a, b: a is not b,
}
UNKNOWN = object()


def outcome(fact, op):
    """Result of `a OP b` given the recorded fact, or UNKNOWN."""
    if fact == "X":
        return UNKNOWN
    ident, eq, order = fact
    if op == IS:
        return ident
    if op == ISNOT:
        return not ident
    if op in (EQ, NE):
        if eq not in (True, False):
            return UNKNOWN
        return eq if op == EQ else not eq
    if order not in (-1, 0, 1):
        return UNKNOWN
    return {LT: order < 0, LE: order <= 0, GT: order > 0, GE: order >= 0}[op]


def _decode(v):
    if isinstance(v, dict) and "t" in v:
        return tuple(_decode(x) for x in v["t"])
    return v


def _literal(node: ast.AST):
    try:
        value = ast.literal_eval(node)
    except Exception:
        return UNKNOWN
    if _plain(value):
        return value
    return UNKNOWN


def _plain(v, depth=0) -> bool:
    t = type(v)
    if t in (int, bool, str, float, type(None)):
        return True
    if t is tuple:
        return depth < 3 and all(_plain(x, depth + 1) for x in v)
    return False


def _dump(node) -> str:
    return ast.dump(node, include_attributes=False)


def diff_paths(a, b, path=()):
    """Paths (tuples of (field, index)) to the smallest AST nodes that differ."""
    if type(a) is not type(b):
        return [path]
    if not isinstance(a, ast.AST):
        return [] if a == b else [path]
    if _dump(a) == _dump(b):
        return []
    out = []
    for field in a._fields:
        va, vb = getattr(a, field, None), getattr(b, field, None)
        if isinstance(va, list) and isinstance(vb, list):
            if len(va) != len(vb):
                return [path]
            for i, (x, y) in enumerate(zip(va, vb)):
                out += _child_diff(x, y, path + ((field, i),), path)
        else:
            out += _child_diff(va, vb, path + ((field, None),), path)
    return out


def _child_diff(x, y, child_path, parent_path):
    # Differences in operator tokens or plain fields belong to the node that
    # holds them (a changed `<` is a change of the Compare).
    if isinstance(x, (ast.expr, ast.stmt, ast.excepthandler, ast.arg, ast.keyword,
                      ast.comprehension, ast.match_case, ast.pattern, ast.withitem)):
        return diff_paths(x, y, child_path)
    if isinstance(x, ast.AST) or isinstance(y, ast.AST):
        return [] if _dump(x) == _dump(y) else [parent_path]
    return [] if x == y else [parent_path]


def node_at(tree, path):
    node = tree
    for field, idx in path:
        node = getattr(node, field)
        if idx is not None:
            node = node[idx]
    return node


def site_key(rel, node, kind):
    return f"{rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}:{kind}"


def in_boolean_context(tree, path) -> bool:
    """True if the value at `path` is only ever used for its truth value."""
    if not path:
        return False
    parent_path = path[:-1]
    field, idx = path[-1]
    parent = node_at(tree, parent_path)
    if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and field == "test":
        return True
    if isinstance(parent, ast.comprehension) and field == "ifs":
        return True
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return True
    if isinstance(parent, ast.BoolOp):
        # An operand's value can become the BoolOp's value, so the BoolOp
        # itself must be used only for its truth value.
        return in_boolean_context(tree, parent_path)
    return False


class _Unpack:
    """site -> {str(group): facts}. Format 3 stores each site as
    [[facts, set id], ...]; expanded on first use."""

    def __init__(self, raw: dict, sets):
        self.raw = raw
        self.sets = sets
        self.cache: dict = {}

    def get(self, site, default=None):
        if site in self.cache:
            return self.cache[site]
        v = self.raw.get(site)
        if v is None:
            return default
        if self.sets is not None:
            d = {}
            for facts, sid in v:
                for g in self.sets[sid]:
                    d[str(g)] = facts
            v = d
        self.cache[site] = v
        return v


class Prover:
    def __init__(self, raw_map: dict):
        self.site_index = {k: i for i, k in enumerate(raw_map.get("sites", []))}
        self.cmp = _Unpack(raw_map.get("cmp", {}), raw_map.get("sets"))
        self.vals = _Unpack(raw_map.get("vals", {}), raw_map.get("sets"))
        self.truth = _Unpack(raw_map.get("truth", {}), raw_map.get("sets"))
        self.incomplete = {str(g) for g in raw_map.get("incomplete", [])}

    def prove(self, rel, old_tree, new_tree, groups: set[int]) -> tuple[set[int], str]:
        paths = diff_paths(old_tree, new_tree)
        if not paths:
            return set(groups), "no ast change"
        silent = set(groups) - {int(g) for g in self.incomplete}
        reasons = []
        for path in paths:
            ok, why = self._prove_path(rel, old_tree, new_tree, path, silent)
            reasons.append(why)
            silent &= ok
            if not silent:
                break
        return silent, "; ".join(reasons)

    def _prove_path(self, rel, old_tree, new_tree, path, groups):
        # Walk outwards from the changed node to the nearest provable form.
        for cut in range(len(path), -1, -1):
            prefix = path[:cut]
            o = node_at(old_tree, prefix)
            n = node_at(new_tree, prefix)
            if isinstance(o, ast.stmt):
                break
            if isinstance(o, ast.Compare) and isinstance(n, ast.Compare):
                return self._compare(rel, o, n, groups)
            if isinstance(o, ast.BoolOp) and isinstance(n, ast.BoolOp):
                return self._boolop(rel, old_tree, prefix, o, n, groups)
        return set(), "unsupported edit"

    def _compare(self, rel, o, n, groups):
        if len(o.ops) != 1 or len(n.ops) != 1:
            return set(), "chained comparison"
        op_o, op_n = OPCODES.get(type(o.ops[0])), OPCODES.get(type(n.ops[0]))
        if op_o is None or op_n is None:
            return set(), "unsupported operator"
        idx = self.site_index.get(site_key(rel, o, "c"))
        if idx is None:
            return set(), "site not recorded"
        idx = str(idx)
        same_left = _dump(o.left) == _dump(n.left)
        same_right = _dump(o.comparators[0]) == _dump(n.comparators[0])
        facts_by_group = self.cmp.get(idx, {})
        proven = set()
        if same_left and same_right:
            for g in groups:
                facts = facts_by_group.get(str(g))
                if not facts:
                    proven.add(g)  # never evaluated in this group's process
                    continue
                if all(_same(outcome(_fact(f), op_o), outcome(_fact(f), op_n)) for f in facts):
                    proven.add(g)
            return proven, f"operator change, {len(proven)}/{len(groups)} groups silent"
        # Literal operand change: replay recorded operand values.
        if op_o in (IS, ISNOT) or op_n in (IS, ISNOT):
            return set(), "identity test with a changed operand"
        new_left = UNKNOWN if same_left else _literal(n.left)
        new_right = UNKNOWN if same_right else _literal(n.comparators[0])
        if (not same_left and new_left is UNKNOWN) or (not same_right and new_right is UNKNOWN):
            return set(), "operand change is not a literal"
        vals_by_group = self.vals.get(idx, {})
        for g in groups:
            facts = facts_by_group.get(str(g))
            if not facts:
                proven.add(g)
                continue
            pairs = vals_by_group.get(str(g))
            if pairs is None or pairs == "overflow" or any(_fact(f) == "X" for f in facts):
                continue
            ok = True
            for a, b in pairs:
                a, b = _decode(a), _decode(b)
                a2 = a if same_left else new_left
                b2 = b if same_right else new_right
                try:
                    before = _PY_OPS[op_o](a, b)
                    after = _PY_OPS[op_n](a2, b2)
                except Exception:
                    ok = False
                    break
                if type(before) is not bool or type(after) is not bool or before != after:
                    ok = False
                    break
            if ok:
                proven.add(g)
        return proven, f"literal change, {len(proven)}/{len(groups)} groups silent"

    def _boolop(self, rel, old_tree, prefix, o, n, groups):
        if type(o.op) is type(n.op):
            return set(), "and/or operands changed"
        if len(o.values) != len(n.values) or any(
            _dump(x) != _dump(y) for x, y in zip(o.values, n.values)
        ):
            return set(), "and/or operands changed"
        if not in_boolean_context(old_tree, prefix):
            return set(), "and/or value used beyond its truth"
        idx = self.site_index.get(site_key(rel, o, "b"))
        if idx is None:
            return set(), "site not recorded"
        # and -> or is silent iff every operand was always true (so all were
        # evaluated); or -> and iff every operand was always false.
        need = isinstance(o.op, ast.And)
        facts_by_group = self.truth.get(str(idx), {})
        proven = set()
        for g in groups:
            facts = facts_by_group.get(str(g))
            if not facts:
                proven.add(g)
                continue
            seen = {}
            for i, b in facts:
                seen.setdefault(i, set()).add(b)
            if all(seen.get(i) == {need} for i in range(len(o.values))):
                proven.add(g)
        return proven, f"and/or swap, {len(proven)}/{len(groups)} groups silent"


def _fact(f):
    return f if f == "X" else tuple(f)


def _same(x, y) -> bool:
    if x is UNKNOWN or y is UNKNOWN:
        return False
    return x == y
