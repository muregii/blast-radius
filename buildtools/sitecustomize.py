"""Build time only: record sqlglot lines executed in child processes.

The map's pytest plugin sets RTS_CHILD_DIR and RTS_PARENT_PID. Any other
Python process that starts with this directory on PYTHONPATH (spawned pool
workers, subprocesses) records the sqlglot lines it runs and writes them to
RTS_CHILD_DIR/<pid>.json when it exits.
"""

import os

_dir = os.environ.get("RTS_CHILD_DIR")
if _dir and os.environ.get("RTS_PARENT_PID") != str(os.getpid()):
    import atexit
    import json
    import sys

    _root = os.path.realpath(os.environ["RTS_ROOT"])
    _src = os.path.join(_root, "sqlglot") + os.sep
    _seen: dict[str, set[int]] = {}
    _cache: dict[str, object] = {}
    _mon = sys.monitoring
    _tool = _mon.COVERAGE_ID

    def _on_line(code, line):
        fn = code.co_filename
        rel = _cache.get(fn, 0)
        if rel == 0:
            real = os.path.realpath(fn)
            rel = real[len(_root) + 1 :] if real.startswith(_src) else None
            _cache[fn] = rel
        if rel is not None:
            _seen.setdefault(rel, set()).add(line)
        return _mon.DISABLE

    def _dump():
        try:
            _mon.set_events(_tool, 0)
            path = os.path.join(_dir, f"{os.getpid()}.json")
            with open(path, "w") as f:
                json.dump({k: sorted(v) for k, v in _seen.items()}, f)
        except Exception:
            pass

    try:
        _mon.use_tool_id(_tool, "rtsmap-child")
        _mon.register_callback(_tool, _mon.events.LINE, _on_line)
        _mon.set_events(_tool, _mon.events.LINE)
        atexit.register(_dump)
        # multiprocessing workers can leave through os._exit; flush there too.
        try:
            import multiprocessing.util as _mpu

            _mpu.Finalize(None, _dump, exitpriority=-100)
        except Exception:
            pass
    except Exception:
        pass
