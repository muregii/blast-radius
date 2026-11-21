"""Blast Radius selector.

For each patch:
  1. Static filter. Hunks that leave the Python AST unchanged are dropped.
     Every remaining change is mapped to the statements it touches, and the
     build-time map gives the test groups whose process ran those lines (or
     opened the changed data file). Tests outside those groups cannot see the
     change. Anything we cannot reason about selects every test.
  2. Runtime check. Cheap candidates are run against the patched tree in /tmp
     and only the ones that fail are kept. Expensive candidates are printed
     without running them.
  3. The kept tests are split over the number of workers that minimises the
     busiest worker plus 2 s per worker.

Logs go to stderr; stdout carries only answer lines.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from silent import Prover  # noqa: E402

DATA = Path(os.environ.get("RTS_DATA", "/data"))
REPO = Path(os.environ.get("RTS_REPO", "/repo"))
MAP = Path(os.environ.get("RTS_MAP", "/app/map.json"))
APP = Path(__file__).resolve().parent
TMP = Path(os.environ.get("RTS_TMP", "/tmp"))
STATE = MAP.with_suffix(".state.json")
IMPORTSTATE = APP / "buildtools" / "importstate.py"
# Only try the class-attribute path when the line-based selection costs more
# than this (it spends ~1.5 s of CPU importing all of sqlglot).
CLASS_ATTR_MIN_MS = int(os.environ.get("RTS_CLASS_ATTR_MIN_MS", "5000"))

WORKER_MS = 2000
MAX_WORKERS = 8
# Candidates at or above this many recorded seconds are printed, not run.
VERIFY_MAX_SECONDS = float(os.environ.get("RTS_VERIFY_MAX", "2.0"))
# Wall-clock seconds we allow ourselves per commit on average.
WALL_PER_COMMIT = float(os.environ.get("RTS_WALL_PER_COMMIT", "4.5"))
WALL_RESERVE = 20.0
# Verification CPU ~= ALPHA * recorded seconds + BETA (pytest start, imports).
VERIFY_ALPHA = float(os.environ.get("RTS_VERIFY_ALPHA", "1.4"))
VERIFY_BETA = float(os.environ.get("RTS_VERIFY_BETA", "2.5"))


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------- patches --

class FilePatch:
    def __init__(self, old: str | None, new: str | None):
        self.old = old  # repo-relative path, None for /dev/null
        self.new = new
        self.hunks: list[tuple[int, list[str]]] = []  # (old_start, lines)


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    lines = text.split("\n")
    i = 0

    def strip(p: str) -> str | None:
        p = p.split("\t")[0].strip()
        if p == "/dev/null":
            return None
        return p[2:] if p[:2] in ("a/", "b/") else p

    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = FilePatch(strip(line[4:]), strip(lines[i + 1][4:]))
            files.append(cur)
            i += 2
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m and cur is not None:
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body: list[str] = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                l = lines[i]
                if l.startswith("\\"):
                    body.append(l)
                    i += 1
                    continue
                tag = l[:1] if l else " "
                body.append(l if l else " ")
                if tag in (" ", "-"):
                    o += 1
                if tag in (" ", "+"):
                    n += 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            cur.hunks.append((int(m.group(1)), body))
            continue
        i += 1
    return files


def apply_hunks(old_text: str, hunks: list[tuple[int, list[str]]]) -> str:
    """Apply hunks to old_text. Raises ValueError if context does not match."""
    old = old_text.split("\n")
    out: list[str] = []
    pos = 0  # index into old
    for start, body in hunks:
        idx = max(start - 1, 0) if start > 0 else 0
        if start == 0:
            idx = 0
        out.extend(old[pos:idx])
        pos = idx
        for l in body:
            if l.startswith("\\"):
                continue
            tag, content = l[:1], l[1:]
            if tag == " ":
                if pos >= len(old) or old[pos] != content:
                    raise ValueError("context mismatch")
                out.append(content)
                pos += 1
            elif tag == "-":
                if pos >= len(old) or old[pos] != content:
                    raise ValueError("removal mismatch")
                pos += 1
            elif tag == "+":
                out.append(content)
    out.extend(old[pos:])
    return "\n".join(out)


def hunk_regions(start: int, body: list[str]) -> tuple[set[int], set[int]]:
    """Old line numbers removed, and old line numbers next to an insertion.

    A run of removed lines followed by the same number of added lines with the
    same indentation is an in-place edit: only the removed lines matter.
    Anything else that adds lines is treated as an insertion as well.
    """
    removed: set[int] = set()
    near_insert: set[int] = set()
    items = [l for l in body if not l.startswith("\\")]
    ln = start
    i = 0
    while i < len(items):
        tag = items[i][:1]
        if tag == " ":
            ln += 1
            i += 1
            continue
        minus: list[str] = []
        plus: list[str] = []
        first = ln
        while i < len(items) and items[i][:1] in "-+":
            if items[i][:1] == "-":
                minus.append(items[i][1:])
                removed.add(ln)
                ln += 1
            else:
                plus.append(items[i][1:])
            i += 1

        def indent(s: str) -> int:
            return len(s) - len(s.lstrip())

        in_place = (
            minus
            and len(minus) == len(plus)
            and all(indent(a) == indent(b) for a, b in zip(minus, plus))
        )
        if plus and not in_place:
            near_insert.update({first - 1, ln})
    return removed, near_insert


# -------------------------------------------------------------- analysis --

def ast_equal(a: str, b: str) -> bool:
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return False


class PyIndex:
    """Statement spans of one parsed module."""

    def __init__(self, src: str):
        self.tree = ast.parse(src)
        self.stmts: list[ast.stmt] = []
        self.funcs: list[ast.AST] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.stmt):
                self.stmts.append(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                self.funcs.append(node)

    @staticmethod
    def _header_end(node: ast.stmt) -> int:
        ends = []
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            seq = getattr(node, field, None)
            if seq:
                ends.append(min(getattr(c, "lineno", node.end_lineno) for c in seq) - 1)
        return min(ends) if ends else node.end_lineno

    def stmt_lines(self, line: int) -> set[int] | None:
        """Lines of the innermost statement containing `line` (header only for
        compound statements). None if no statement contains it."""
        best = None
        for s in self.stmts:
            if s.lineno <= line <= s.end_lineno:
                if best is None or (s.end_lineno - s.lineno) <= (best.end_lineno - best.lineno):
                    best = s
        if best is None:
            return None
        lo = best.lineno
        if best.decorator_list if hasattr(best, "decorator_list") else False:
            lo = min(d.lineno for d in best.decorator_list)
        hi = self._header_end(best)
        if line > hi:
            # Line sits between compound clauses (e.g. an `else:`): treat the
            # whole compound statement as touched.
            hi = best.end_lineno
        return set(range(lo, max(hi, lo) + 1))

    def enclosing_function_lines(self, line: int) -> set[int] | None:
        best = None
        for f in self.funcs:
            if f.lineno <= line <= f.end_lineno:
                if best is None or (f.end_lineno - f.lineno) <= (best.end_lineno - best.lineno):
                    best = f
        if best is None:
            return None
        return set(range(best.lineno, best.end_lineno + 1))


class Selector:
    def __init__(self):
        raw = json.loads(MAP.read_text())
        self._gt_cache: dict = {}
        sets = raw.get("sets")
        if raw.get("failed_baseline"):
            log(f"warning: map baseline failures {raw['failed_baseline']}")
        self.groups: list[list[str]] = raw["groups"]
        # Format 3 interns test sets: lines/opens hold an index into "sets".
        sets = raw.get("sets")
        if sets is not None:
            self.lines = {f: {k: sets[v] for k, v in per.items()} for f, per in raw["lines"].items()}
            self.opens = {p: sets[v] for p, v in raw["opens"].items()}
        else:
            self.lines = raw["lines"]
            self.opens = raw["opens"]
        doc = json.loads((DATA / "tests.json").read_text())
        self.tests = doc["tests"]
        self.by_id = {t["id"]: t for t in self.tests}
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.prover = Prover(raw)
        self.attr_names = set(raw.get("attr_names", ()))
        self.attr_tests = {}
        for a, sid in raw.get("attrs", {}).items():
            self.attr_tests[a] = self.group_tests_cached(sets[sid]) if sets is not None else set()
        self._state = None
        self._test_texts: dict[str, str] = {}
        self._helpers = None
        self.silent_log: list[str] = []
        self.tests_by_file: dict[str, set[int]] = {}
        for t in self.tests:
            self.tests_by_file.setdefault(t["file"], set()).add(t["number"])
        mapped = {i for g in self.groups for i in g}
        # Tests the map does not know about are always selected.
        self.unmapped = {t["number"] for t in self.tests if t["id"] not in mapped}
        self.all = {t["number"] for t in self.tests}

    def group_tests_cached(self, gis) -> set[int]:
        key = id(gis)
        hit = self._gt_cache.get(key)
        if hit is None:
            hit = self._gt_cache[key] = self.group_tests(gis)
        return hit

    def group_tests(self, gis) -> set[int]:
        out = set()
        for gi in gis:
            for tid in self.groups[gi]:
                t = self.by_id.get(tid)
                if t:
                    out.add(t["number"])
        return out

    def groups_for_lines(self, path: str, lines: set[int]) -> set[int]:
        per = self.lines.get(path, {})
        gis: set[int] = set()
        for ln in lines:
            gis.update(per.get(str(ln), ()))
        return gis

    def groups_for_file(self, path: str) -> set[int]:
        gis: set[int] = set()
        for v in self.lines.get(path, {}).values():
            gis.update(v)
        return gis

    def candidates(self, patch_text: str) -> tuple[set[int], dict[str, str], str]:
        """Returns (candidate test numbers, {path: new text}, reason)."""
        files = parse_patch(patch_text)
        if not files:
            return set(), {}, "empty patch"
        cands: set[int] = set()
        new_texts: dict[str, str] = {}
        reasons = []
        class_edits: list[tuple[str, set[int], str]] = []
        for fp in files:
            if fp.old is None or fp.new is None or fp.old != fp.new:
                return set(self.all), {}, f"add/delete/rename {fp.old}->{fp.new}"
            path = fp.old
            src = REPO / path
            try:
                old_text = src.read_text(encoding="utf-8")
                new_text = apply_hunks(old_text, fp.hunks)
            except (OSError, ValueError, UnicodeDecodeError) as e:
                return set(self.all), {}, f"cannot apply {path}: {e}"
            new_texts[path] = new_text
            if path.endswith(".py"):
                c, why = self._python_change(path, old_text, fp)
                if (c and path.startswith("sqlglot/") and split_cost(c, self.ms) > CLASS_ATTR_MIN_MS
                        and class_body_data_edit(old_text, new_text)):
                    class_edits.append((path, c, why))
                    continue
            elif path in self.opens or path.startswith("tests/fixtures/"):
                c, why = self.group_tests(self.opens.get(path, [])), "data file"
            else:
                c, why = set(self.all), "unknown file kind"
            reasons.append(f"{path}: {why} -> {len(c)}")
            cands |= c
        if class_edits:
            # Edits that only change data assigned in class bodies: what they
            # can change is import-time state. Compare it with the pinned
            # commit's and select the tests that read a changed attribute.
            alt, why2 = self.class_attr_tests(new_texts)
            for path, c, why in class_edits:
                if alt is not None and split_cost(alt, self.ms) < split_cost(c, self.ms):
                    c, why = alt, why2
                else:
                    why = f"{why}; class attrs: {why2}"
                reasons.append(f"{path}: {why} -> {len(c)}")
                cands |= c
        return cands, new_texts, "; ".join(reasons)

    def class_attr_tests(self, new_texts: dict[str, str]) -> tuple[set[int] | None, str]:
        """Tests that read a class attribute whose import-time value the patch
        changes, or None if that cannot be told."""
        if not self.attr_names or not STATE.exists():
            return None, "no attribute map"
        try:
            base = self._base_state()
            root = mirror_tree(TMP / "st", new_texts)
            out = TMP / "rts_state.json"
            if out.exists():
                out.unlink()
            env = {k: v for k, v in os.environ.items() if not k.startswith("RTS_")}
            env.update(PYTHONHASHSEED="0", PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
            subprocess.run([sys.executable, str(IMPORTSTATE), str(out), str(root)], env=env,
                           cwd=str(TMP), capture_output=True, timeout=60, check=True)
            new = json.loads(out.read_text())
        except Exception as e:  # never let this cost safety
            return None, f"state error {type(e).__name__}"
        if new.get("import_failed") != base.get("import_failed"):
            return None, "import changed"
        a, b = base["state"], new["state"]
        changed = [k for k in set(a) | set(b) if a.get(k) != b.get(k)
                   and not k.endswith(("|__path__", "|__file__", "|__cached__"))]
        if not changed:
            return set(), "import state unchanged"
        tests: set[int] = set()
        for k in changed:
            if not k.startswith("C:") or k not in a or k not in b:
                return None, f"changed {k.split('|')[-1]} ({'module global' if k.startswith('M:') else 'added/removed'})"
            owner, name = k[2:].split("|", 1)
            if name not in self.attr_names:
                return None, f"changed {name} (reads not recorded)"
            if b[k].startswith("enum") or a[k].startswith("enum"):
                return None, f"changed {name} (enum)"
            tests |= self.attr_tests.get(f"{owner}|{name}", set())
            tests |= self.tests_naming(name)
        return tests | self.unmapped, f"{len(changed)} class attrs changed"

    def _base_state(self) -> dict:
        if self._state is None:
            self._state = json.loads(STATE.read_text())
        return self._state

    def tests_naming(self, name: str) -> set[int]:
        """Tests in test files that mention the attribute by name."""
        out = set()
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        for f, nums in self.tests_by_file.items():
            text = self._test_texts.get(f)
            if text is None:
                try:
                    text = (REPO / f).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                # helpers imported by test files can read attributes too
                self._test_texts[f] = text
            if pat.search(text):
                out |= nums
        if pat.search(self._helpers_text()):
            out |= set(self.all)
        return out

    def _helpers_text(self) -> str:
        if self._helpers is None:
            parts = []
            for p in (REPO / "tests").rglob("*.py"):
                if not p.name.startswith("test_"):
                    try:
                        parts.append(p.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
            self._helpers = "\n".join(parts)
        return self._helpers

    def _python_change(self, path: str, old_text: str, fp: FilePatch) -> tuple[set[int], str]:
        under_src = path.startswith("sqlglot/")
        under_tests = path.startswith("tests/")
        if not (under_src or under_tests):
            return set(self.all), "python outside sqlglot/tests"
        try:
            index = PyIndex(old_text)
        except SyntaxError:
            return set(self.all), "old file unparsable"
        new_full = apply_hunks(old_text, fp.hunks)
        if ast_equal(old_text, new_full):
            return set(), "ast unchanged"
        try:
            ast.parse(new_full)
        except SyntaxError:
            # The module no longer imports: everything that loaded it fails.
            if under_src:
                return self.group_tests(self.groups_for_file(path)), "syntax error"
            return set(self.all), "test syntax error"
        if under_tests:
            mod = path
            if path.endswith(("helpers.py", "__init__.py", "conftest.py")) or "/" in path[len("tests/"):] and not os.path.basename(path).startswith("test_"):
                return set(self.all), "shared test helper"
            return {t["number"] for t in self.tests if t["file"] == mod}, "test file"

        all_gis: set[int] = set()
        notes = []
        for start, body in fp.hunks:
            # Ignore hunks that on their own leave the AST unchanged.
            try:
                hunk_text = apply_hunks(old_text, [(start, body)])
            except ValueError:
                return set(self.all), "hunk does not apply alone"
            if ast_equal(old_text, hunk_text):
                continue
            lines: set[int] = set()
            removed, near = hunk_regions(start, body)
            whole_file = False
            for ln in removed:
                span = index.stmt_lines(ln)
                if span is None:
                    whole_file = True
                    break
                lines |= span
            if near and not whole_file:
                # Inserted code runs only if its enclosing function runs; at
                # module or class level it runs at import.
                for ln in near:
                    span = index.enclosing_function_lines(ln) if ln >= 1 else None
                    if span is None:
                        whole_file = True
                        break
                    lines |= span
            gis = self.groups_for_file(path) if whole_file else self.groups_for_lines(path, lines)
            # Silent-mutation proof: drop groups whose recorded executions
            # cannot tell the old expression from the new one.
            if gis and not near:
                try:
                    silent, why = self.prover.prove(path, index.tree, ast.parse(hunk_text), gis)
                except Exception as e:  # never let the prover cost safety
                    silent, why = set(), f"prover error {e!r}"
                if silent:
                    gis = gis - silent
                notes.append(why)
                self.silent_log.append(f"{path}: {why}")
            all_gis |= gis
        return self.group_tests(all_gis), "; ".join(notes) or "lines"


def class_body_data_edit(old_text: str, new_text: str) -> bool:
    """True if the two modules differ only in simple assignments made directly
    in class bodies (`NAME = value`, `NAME: T = value`), at any class nesting,
    never inside a function."""
    try:
        old, new = ast.parse(old_text), ast.parse(new_text)
    except SyntaxError:
        return False
    return _bodies_ok(old.body, new.body, in_class=False)


def _plain_assign(s: ast.stmt) -> bool:
    if isinstance(s, ast.Assign):
        targets = s.targets
    elif isinstance(s, ast.AnnAssign) and s.value is not None and s.simple:
        targets = [s.target]
    else:
        return False
    return all(isinstance(t, ast.Name) and not t.id.startswith("__") for t in targets)


def _bodies_ok(old: list, new: list, in_class: bool) -> bool:
    import difflib

    a = [ast.dump(x) for x in old]
    b = [ast.dump(x) for x in new]
    if a == b:
        return True
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        os_, ns = old[i1:i2], new[j1:j2]
        if (tag == "replace" and len(os_) == len(ns)
                and all(isinstance(x, ast.ClassDef) and isinstance(y, ast.ClassDef) for x, y in zip(os_, ns))):
            for x, y in zip(os_, ns):
                same_head = (x.name == y.name
                             and [ast.dump(d) for d in x.decorator_list] == [ast.dump(d) for d in y.decorator_list]
                             and [ast.dump(d) for d in x.bases] == [ast.dump(d) for d in y.bases]
                             and [ast.dump(d) for d in x.keywords] == [ast.dump(d) for d in y.keywords])
                if not same_head or not _bodies_ok(x.body, y.body, in_class=True):
                    return False
            continue
        if not in_class or not all(_plain_assign(x) for x in os_ + ns):
            return False
    return True


def mirror_tree(root: Path, new_texts: dict[str, str]) -> Path:
    """root/sqlglot: symlinks to the pinned sqlglot with the patched modules as
    real files."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    changed = {Path(p): t for p, t in new_texts.items() if p.startswith("sqlglot/")}
    on_path = {Path(*p.parts[:i]) for p in changed for i in range(1, len(p.parts))}

    def fill(rel: Path):
        (root / rel).mkdir()
        for entry in os.scandir(REPO / rel):
            r = rel / entry.name
            if r in changed:
                continue
            if r in on_path:
                fill(r)
            else:
                (root / r).symlink_to(REPO / r)

    fill(Path("sqlglot"))
    for r, text in changed.items():
        (root / r).write_text(text, encoding="utf-8")
    return root


# ----------------------------------------------------------- verification --

class Workspace:
    """/tmp/w holds a real copy of tests/ (pytest must see real paths to match
    node ids) and, per commit, a symlink mirror of sqlglot/ with the changed
    modules written as real files."""

    def __init__(self, root: Path):
        self.root = root
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        shutil.copytree(REPO / "tests", root / "tests", symlinks=False)
        self.restore: dict[Path, bytes] = {}

    def prepare(self, new_texts: dict[str, str]) -> None:
        for path, data in self.restore.items():
            path.write_bytes(data)
        self.restore.clear()
        pkg = self.root / "sqlglot"
        if pkg.exists() or pkg.is_symlink():
            if pkg.is_symlink():
                pkg.unlink()
            else:
                shutil.rmtree(pkg)
        src_changes = {Path(p): t for p, t in new_texts.items() if p.startswith("sqlglot/")}
        if not src_changes:
            pkg.symlink_to(REPO / "sqlglot")
        else:
            changed = set(src_changes)
            on_path = {Path(*p.parts[:i]) for p in changed for i in range(1, len(p.parts))}

            def fill(rel: Path):
                (self.root / rel).mkdir()
                for entry in os.scandir(REPO / rel):
                    r = rel / entry.name
                    if r in changed:
                        continue
                    if r in on_path:
                        fill(r)
                    else:
                        (self.root / r).symlink_to(REPO / r)

            fill(Path("sqlglot"))
            for r, text in src_changes.items():
                (self.root / r).write_text(text, encoding="utf-8")
        for p, text in new_texts.items():
            if p.startswith("tests/"):
                target = self.root / p
                self.restore[target] = target.read_bytes()
                target.write_text(text, encoding="utf-8")


def run_tests(tree: Path, ids: list[str], timeout: float) -> tuple[set[str] | None, str]:
    """Run ids in tree. Returns the failed ids, or None if the run was unusable."""
    report = TMP / "rts_report.json"
    argfile = TMP / "rts_args.txt"
    if report.exists():
        report.unlink()
    argfile.write_text("\n".join(ids))
    env = dict(os.environ)
    env.update(
        SKIP_INTEGRATION="0",
        PYTHONPATH=os.pathsep.join([str(tree), str(APP)]),
        PYTHONDONTWRITEBYTECODE="1",
        RTS_REPORT=str(report),
    )
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "rts_report",
           "-p", "no:cacheprovider", "-o", "addopts=", "--rootdir", str(tree),
           "-W", "ignore", f"@{argfile}"]
    try:
        proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if not report.exists():
        return None, f"no report (rc={proc.returncode}) {proc.stderr[-500:]}"
    res = json.loads(report.read_text())
    outcomes = res["outcomes"]
    failed = {i for i in ids if outcomes.get(i) == "failed"}
    broken = set(res["broken_files"])
    failed |= {i for i in ids if i.split("::")[0] in broken}
    missing = [i for i in ids if i not in outcomes and i.split("::")[0] not in broken]
    if missing:
        # A test we asked for never reported: treat it as failing.
        failed |= set(missing)
    return failed, f"rc={proc.returncode}"


# ------------------------------------------------------------------ split --

def split_cost(numbers: set[int], ms: dict[int, int]) -> int:
    if not numbers:
        return 0
    bins = split(numbers, ms)
    return max(sum(ms[n] for n in b) for b in bins) + WORKER_MS * len(bins)


def split(numbers: set[int], ms: dict[int, int]) -> list[list[int]]:
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for k in range(1, MAX_WORKERS + 1):
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            bins[j].append(n)
            loads[j] += ms[n]
        bins = [b for b in bins if b]
        cost = max(loads) + WORKER_MS * len(bins)
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


# ------------------------------------------------------------------- main --

def main() -> None:
    t0 = time.monotonic()
    sel = Selector()
    patches = sorted((DATA / "commits").glob("*.patch"))
    budget = WALL_PER_COMMIT * len(patches)
    verify = os.environ.get("RTS_NO_VERIFY") != "1"
    ws = None
    for idx, patch in enumerate(patches):
        cid = patch.stem
        try:
            cands, new_texts, why = sel.candidates(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # never die: run everything instead
            cands, new_texts, why = set(sel.all), {}, f"error {e!r}"
        cands |= sel.unmapped if cands else set()
        keep = set(cands)
        note = ""
        cheap = sorted(n for n in cands if sel.ms[n] < VERIFY_MAX_SECONDS * 1000)
        heavy = cands - set(cheap)
        cost_print = split_cost(cands, sel.ms) / 1000
        cheap_s = sum(sel.ms[n] for n in cheap) / 1000
        est_cpu = VERIFY_ALPHA * cheap_s + VERIFY_BETA
        cost_verify = est_cpu + split_cost(heavy, sel.ms) / 1000
        elapsed = time.monotonic() - t0
        remaining = len(patches) - idx
        allowance = budget - elapsed - WALL_RESERVE - (remaining - 1) * 0.5
        if (verify and cheap and new_texts and cost_verify < cost_print
                and est_cpu * 1.5 < allowance):
            try:
                if ws is None:
                    ws = Workspace(TMP / "w")
                ws.prepare(new_texts)
                ids = [sel.tests[n]["id"] for n in cheap]
                timeout = min(allowance, 3 * est_cpu + 5)
                failed, info = run_tests(ws.root, ids, timeout=timeout)
            except Exception as e:
                failed, info = None, f"error {e!r}"
            if failed is not None:
                failed_nums = {sel.by_id[i]["number"] for i in failed}
                keep = heavy | failed_nums
                note = f"verified {len(cheap)} -> {len(failed_nums)} fail ({info})"
            else:
                note = f"verification unusable: {info}"
        else:
            note = f"printed (print {cost_print:.1f}s vs verify {cost_verify:.1f}s)"
        workers = split(keep, sel.ms)
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
        log(f"{cid}: {why} | cands={len(cands)} keep={len(keep)} {note} "
            f"[{time.monotonic() - t0:.1f}s]")


if __name__ == "__main__":
    main()
