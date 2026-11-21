"""Fingerprint sqlglot's import-time state: every class attribute and module
global of every sqlglot module, after importing all of them.

Usage: PYTHONHASHSEED=0 PYTHONPATH=<sqlglot root> python importstate.py <out.json>

Writes {"state": {key: fingerprint}, "opaque": n}. Keys are
"C:<module>.<qualname>|<attr>" for class attributes and "M:<module>|<name>"
for module globals. Classes that share a module and qualname (classes made
with type() in a metaclass) share one key: their fingerprints are merged, and
the recorder at build time keys attribute reads the same way, so a change in
any of them selects every test that read any of them.

Comparing the state before and after a patch tells which class attributes
an edit changed, including values derived from it while the classes were
created (metaclasses, __init_subclass__, other class bodies). The fingerprint
is a description of the value, never an id or address, so two imports of the
same code give the same fingerprint.
"""

from __future__ import annotations

import enum
import hashlib
import importlib
import json
import os
import pkgutil
import sys
import types

MAX_DEPTH = 40


def class_key(c: type) -> str:
    return f"{c.__module__}.{c.__qualname__}"


class Fingerprinter:
    def __init__(self):
        self.opaque = 0
        self.memo: dict[int, str] = {}
        self.stack: set[int] = set()

    def fp(self, v, depth: int = 0) -> str:
        t = type(v)
        if v is None or t in (bool, int, float, complex, str, bytes):
            return f"{t.__name__}:{v!r}"
        if isinstance(v, type):
            return "class:" + class_key(v)
        if isinstance(v, enum.Enum):
            return f"enum:{class_key(t)}.{v.name}"
        if isinstance(v, types.ModuleType):
            return "module:" + v.__name__
        i = id(v)
        if i in self.memo:
            return self.memo[i]
        if i in self.stack or depth > MAX_DEPTH:
            return "cycle"
        self.stack.add(i)
        try:
            out = self._fp(v, t, depth + 1)
        finally:
            self.stack.discard(i)
        if len(out) > 256:
            out = out.split(":", 1)[0][:24] + "#" + hashlib.sha1(out.encode()).hexdigest()
        self.memo[i] = out
        return out

    def _fp(self, v, t, d) -> str:
        f = self.fp
        if t in (tuple, list) or isinstance(v, (tuple, list)):
            items = [f(x, d) for x in v]
            if v and all(isinstance(x, type) for x in v):
                # Tuples of classes are built from sets (isinstance targets):
                # their order differs between runs.
                items.sort()
            return f"{class_key(t)}[" + ",".join(items) + "]"
        if isinstance(v, dict):
            items = [f"{f(k, d)}:{f(x, d)}" for k, x in v.items()]
            if not all(_stable_key(k) for k in v):
                # Keys hashed by identity (classes, objects) come out of sets in
                # a different order every run: compare those dicts unordered.
                items.sort()
            return f"{class_key(t)}{{" + ",".join(items) + "}"
        if isinstance(v, (set, frozenset)):
            return f"{class_key(t)}(" + ",".join(sorted(f(x, d) for x in v)) + ")"
        if isinstance(v, (types.FunctionType, types.LambdaType)):
            return "fn:" + self._code(v.__code__, d) + "|" + ",".join(
                [f(v.__defaults__, d), f(v.__kwdefaults__, d),
                 ",".join(f(c.cell_contents, d) if _cell_ok(c) else "empty"
                          for c in (v.__closure__ or ()))])
        if isinstance(v, (classmethod, staticmethod)):
            return f"{t.__name__}:" + f(v.__func__, d)
        if isinstance(v, property):
            return "property:" + ",".join(f(x, d) for x in (v.fget, v.fset, v.fdel))
        if isinstance(v, (types.MethodType,)):
            return "method:" + f(v.__func__, d) + "@" + f(v.__self__, d)
        if isinstance(v, (types.BuiltinFunctionType, types.MethodWrapperType,
                          types.WrapperDescriptorType, types.MethodDescriptorType,
                          types.GetSetDescriptorType, types.MemberDescriptorType)):
            return f"builtin:{getattr(v, '__qualname__', getattr(v, '__name__', '?'))}"
        if isinstance(v, types.CodeType):
            return self._code(v, d)
        import functools
        import re

        if isinstance(v, functools.partial):
            return "partial:" + ",".join([f(v.func, d), f(v.args, d), f(v.keywords, d)])
        if isinstance(v, re.Pattern):
            return f"re:{v.pattern!r}:{v.flags}"
        parts = []
        dct = getattr(v, "__dict__", None)
        if isinstance(dct, dict):
            parts.append(f(dict(dct), d))
        for cls in t.__mro__:
            for s in cls.__dict__.get("__slots__", ()) if isinstance(cls.__dict__.get("__slots__", ()), (tuple, list)) else ():
                if s in ("__dict__", "__weakref__"):
                    continue
                parts.append(f"{s}=" + (f(getattr(v, s), d) if hasattr(v, s) else "unset"))
        if parts:
            return f"obj:{class_key(t)}(" + ";".join(parts) + ")"
        r = repr(v)
        if " at 0x" in r or "object at" in r:
            self.opaque += 1
            return f"opaque:{class_key(t)}"
        return f"repr:{class_key(t)}:{r}"

    def _code(self, c: types.CodeType, d) -> str:
        consts = ",".join(self._code(x, d) if isinstance(x, types.CodeType) else self.fp(x, d)
                          for x in c.co_consts)
        h = hashlib.sha1(c.co_code + repr((c.co_names, c.co_varnames, c.co_freevars,
                                           c.co_cellvars, c.co_qualname)).encode()).hexdigest()
        return f"code:{c.co_qualname}:{h}:{hashlib.sha1(consts.encode()).hexdigest()}"


def _stable_key(k) -> bool:
    if k is None or type(k) in (bool, int, float, str, bytes) or isinstance(k, enum.Enum):
        return True
    if type(k) is tuple:
        return all(_stable_key(x) for x in k)
    return False


def _cell_ok(c) -> bool:
    try:
        c.cell_contents
        return True
    except ValueError:
        return False


def import_all(root: str) -> list[str]:
    import sqlglot  # noqa: F401

    failed = []
    pkg_dir = os.path.join(root, "sqlglot")
    for info in pkgutil.walk_packages([pkg_dir], "sqlglot."):
        name = info.name
        if ".__main__" in name or name.endswith("__main__"):
            continue
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{name}: {type(e).__name__}")
    return failed


def collect(root: str) -> dict:
    failed = import_all(root)
    fpr = Fingerprinter()
    by_key: dict[str, list[str]] = {}
    seen_classes: set[int] = set()
    todo: list[type] = []

    def add(k, v):
        by_key.setdefault(k, []).append(fpr.fp(v))

    mods = sorted((n, m) for n, m in sys.modules.items()
                  if (n == "sqlglot" or n.startswith("sqlglot.")) and m is not None)
    for name, mod in mods:
        for k, v in list(vars(mod).items()):
            if k in ("__builtins__", "__loader__", "__spec__", "__cached__", "__file__"):
                continue
            add(f"M:{name}|{k}", v)
            if isinstance(v, type):
                todo.append(v)
    # Every class reachable from a module, a class attribute, or a subclass
    # link that belongs to sqlglot.
    while todo:
        c = todo.pop()
        if id(c) in seen_classes:
            continue
        seen_classes.add(id(c))
        if not (c.__module__ == "sqlglot" or str(c.__module__).startswith("sqlglot.")):
            continue
        ck = class_key(c)
        for k, v in list(c.__dict__.items()):
            if k in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__"):
                continue
            add(f"C:{ck}|{k}", v)
            if isinstance(v, type):
                todo.append(v)
        try:
            todo.extend(type.__subclasses__(c))
        except TypeError:
            pass
        mc = type(c)
        if mc is not type:
            todo.append(mc)
    state = {k: (v[0] if len(v) == 1 else "multi:" + "|".join(sorted(v))) for k, v in by_key.items()}
    # Keep the kind (fn, class, dict, ...) readable in front of the hash.
    state = {k: (v if len(v) <= 64 else v.split(":", 1)[0][:24] + "#" + hashlib.sha1(v.encode()).hexdigest())
             for k, v in state.items()}
    return {"state": state, "opaque": fpr.opaque, "import_failed": failed}


def main() -> None:
    out = sys.argv[1]
    root = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("RTS_STATE_ROOT", "")
    if root:
        sys.path.insert(0, root)
    data = collect(root or os.path.dirname(os.path.dirname(importlib.util.find_spec("sqlglot").origin)))
    with open(out, "w") as f:
        json.dump(data, f, separators=(",", ":"))


if __name__ == "__main__":
    main()
