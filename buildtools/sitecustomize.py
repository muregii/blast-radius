"""Build time only. Loaded by every Python process the map build starts
(PYTHONPATH includes this directory).

1. Installs _rts_c / _rts_v into builtins. The instrumented sqlglot copy calls
   them for every simple comparison and every and/or operand. They compute
   exactly what the original expression computed and record safe facts:
     comparisons: (a is b, a == b, ordering of a and b) plus the operand
                  values when they are small JSON-able literals;
     and/or:      the truth value of each evaluated operand.
   Facts are only recorded for "safe" types, whose comparisons and truth tests
   cannot run user code; anything else is recorded as opaque ("X").
2. In child processes (spawned pools), also records sqlglot line coverage.
   Every child writes <pid>.start at startup and <pid>.json at exit, so the
   parent can tell if a child's data went missing.
"""

import builtins
import os
import sys

_dir = os.environ.get("RTS_CHILD_DIR")
_is_child = bool(_dir) and os.environ.get("RTS_PARENT_PID") != str(os.getpid())

if os.environ.get("RTS_RECORD") == "1":
    import enum
    import math
    import operator

    _OPS = (operator.lt, operator.le, operator.gt, operator.ge, operator.eq,
            operator.ne, operator.is_, operator.is_not)
    _CMP_DUNDERS = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
    _TRUTH_DUNDERS = ("__bool__", "__len__")
    _SCALARS = (int, bool, str, bytes, type(None))
    _CONTAINERS = (tuple, list, dict, set, frozenset)
    _SAFE_MODULES = ("builtins", "enum")
    _MAX_VALUES = 32

    cmp_facts: dict[int, set] = {}
    cmp_values: dict[int, object] = {}  # set of value pairs, or "overflow"
    truth_facts: dict[int, set] = {}
    builtins._rts_data = (cmp_facts, cmp_values, truth_facts)

    _type_cmp_safe: dict[type, bool] = {}
    _type_truth_safe: dict[type, bool] = {}

    def _class_defines(t: type, names) -> bool:
        for cls in t.__mro__:
            if cls is object or cls.__module__ in _SAFE_MODULES:
                continue
            if any(n in cls.__dict__ for n in names):
                return True
        return False

    def _cmp_safe(v, depth=0) -> bool:
        t = type(v)
        if t in _SCALARS:
            return True
        if t is float:
            return True
        if t in (tuple, frozenset):
            return depth < 3 and len(v) <= 16 and all(_cmp_safe(x, depth + 1) for x in v)
        ok = _type_cmp_safe.get(t)
        if ok is None:
            ok = t not in _CONTAINERS and not _class_defines(t, _CMP_DUNDERS)
            _type_cmp_safe[t] = ok
        return ok

    def _orderable(v, depth=0) -> bool:
        t = type(v)
        if t in (int, bool, str, bytes):
            return True
        if t is float:
            return not math.isnan(v)
        if t is tuple:
            return depth < 3 and len(v) <= 16 and all(_orderable(x, depth + 1) for x in v)
        return False

    def _jsonable(v, depth=0) -> bool:
        t = type(v)
        if t in (int, bool, str, type(None)):
            return t is not str or len(v) <= 64
        if t is float:
            return math.isfinite(v)
        if t is tuple:
            return depth < 3 and len(v) <= 8 and all(_jsonable(x, depth + 1) for x in v)
        return False

    def _note_cmp(site, a, b):
        if not (_cmp_safe(a) and _cmp_safe(b)):
            fact = "X"
        else:
            try:
                eq = bool(a == b)
            except Exception:
                eq = "E"
            order = None
            if _orderable(a) and _orderable(b):
                try:
                    order = -1 if a < b else (0 if eq is True else 1)
                except TypeError:
                    order = "T"
            fact = (a is b, eq, order)
        s = cmp_facts.get(site)
        if s is None:
            s = cmp_facts[site] = set()
        s.add(fact)
        vals = cmp_values.get(site)
        if vals == "overflow":
            return
        if fact != "X" and _jsonable(a) and _jsonable(b):
            if vals is None:
                vals = cmp_values[site] = set()
            if len(vals) < _MAX_VALUES:
                vals.add((a, b))
                return
        cmp_values[site] = "overflow"

    _FAST = (int, bool, str, bytes, type(None), float)
    _seen_pairs: set = set()

    def _rts_c(site, a, b, op):
        try:
            result = _OPS[op](a, b)
        except BaseException:
            _note_cmp(site, a, b)
            raise
        ta = type(a)
        tb = type(b)
        if ta in _FAST and tb in _FAST:
            # Facts for a scalar pair never change: record each pair once.
            # Identity is part of the facts: two equal strings or ints may or
            # may not be the same object, so it must be part of the key.
            key = (site, ta, a, tb, b, a is b)
            if key in _seen_pairs:
                return result
            _seen_pairs.add(key)
        _note_cmp(site, a, b)
        return result

    def _truth_safe(v) -> bool:
        t = type(v)
        ok = _type_truth_safe.get(t)
        if ok is None:
            ok = t in _SCALARS or t in _CONTAINERS or t is float or (
                not _class_defines(t, _TRUTH_DUNDERS))
            _type_truth_safe[t] = ok
        return ok

    def _rts_v(site, i, v):
        s = truth_facts.get(site)
        if s is None:
            s = truth_facts[site] = set()
        t = type(v)
        ok = _type_truth_safe.get(t)
        if ok is None:
            ok = _truth_safe(v)
        s.add((i, bool(v) if ok else "X"))
        return v

    builtins._rts_c = _rts_c
    builtins._rts_v = _rts_v


def _encode_values(vals):
    if vals == "overflow":
        return "overflow"
    return [[_enc(a), _enc(b)] for a, b in vals]


def _enc(v):
    if type(v) is tuple:
        return {"t": [_enc(x) for x in v]}
    return v


if _is_child:
    import atexit
    import json

    _root = os.path.realpath(os.environ["RTS_ROOT"])
    _src = os.path.join(_root, "sqlglot") + os.sep
    _seen: dict[str, set[int]] = {}
    _cache: dict[str, object] = {}
    _mon = sys.monitoring
    _tool = _mon.COVERAGE_ID
    _done = False

    def _on_line(code, line):
        fn = code.co_filename
        rel = _cache.get(fn, 0)
        if rel == 0:
            real = os.path.realpath(fn)
            rel = real[len(_root) + 1 :] if real.startswith(_src) else None
            _cache[fn] = rel
        if rel is not None:
            if not _seen:
                # First sqlglot line in this process: from now on its data
                # must arrive. Helpers that never run sqlglot (the
                # multiprocessing resource tracker) never register.
                try:
                    open(os.path.join(_dir, f"{os.getpid()}.start"), "w").close()
                except Exception:
                    pass
            _seen.setdefault(rel, set()).add(line)
        return _mon.DISABLE

    def _dump():
        global _done
        if _done:
            return
        _done = True
        try:
            _mon.set_events(_tool, 0)
            data = {"lines": {k: sorted(v) for k, v in _seen.items()}}
            rec = getattr(builtins, "_rts_data", None)
            if rec is not None:
                cf, cv, tf = rec
                data["cmp"] = {str(k): [list(f) if f != "X" else "X" for f in v] for k, v in cf.items()}
                data["vals"] = {str(k): _encode_values(v) for k, v in cv.items()}
                data["truth"] = {str(k): sorted(v, key=repr) for k, v in tf.items()}
            path = os.path.join(_dir, f"{os.getpid()}.json")
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
        except Exception:
            pass

    try:
        _mon.use_tool_id(_tool, "rtsmap-child")
        _mon.register_callback(_tool, _mon.events.LINE, _on_line)
        _mon.set_events(_tool, _mon.events.LINE)
        atexit.register(_dump)
        try:
            import multiprocessing.util as _mpu

            _mpu.Finalize(None, _dump, exitpriority=-100)
        except Exception:
            pass
    except Exception:
        pass
