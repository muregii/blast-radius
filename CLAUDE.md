# Blast Radius — handoff notes

Entry for the RWX "Blast Radius" track (Builders Buildathon). For each patch to
sqlglot (pinned commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), print which
tests to run and how to split them over ≤8 workers. Rules: README.md and SCORING.md.

**Deadline: Sunday 11:00 AM EST.** One practice submission per hour; the board
keeps the best score. Track page: joinbuilders.org/buildathon/tracks/blast-radius

## Scoring in one line
Per commit: busiest worker's recorded seconds + 2 s per worker; +300 s if any
failing test is left out; plus total container CPU seconds; middle of 3 runs.
A single miss costs more than the whole leader's total, so **zero misses comes first**.

## Status
| Version | Commit | Sample (simulated) | Practice board |
|---|---|---|---|
| starter | e60dec6 | — | 1614.1 s |
| v1 coverage | 08354ee | 268.2 s, 0 misses | 327.4 s, 0 misses |
| v2 silent proofs | 4fe3624 | 212.5 s, 0 misses | **317.0 s, 0 misses** (CPU ≈ 2.2 s) |
| v3 per-test coverage | 0f7a851 | 168.8 s, 0 misses | not submitted (c2 superseded it) |
| c2 class-attribute reads | 55118fa | 127.7 s, 0 misses | **252.5 s, 0 misses** (CPU 7.3 s) |
| c3 test edits, inert files | see RESULTS.md | 127.7 s, 0 misses | see RESULTS.md |

Every submission and what it taught is logged in RESULTS.md.

Leaders were ~169–180 s.

## Architecture
Build time (Dockerfile → `buildtools/build_map.py`):
1. `fetch_sqlglot.py` downloads sqlglot at the pinned commit to /repo.
2. `instrument.py` makes a recording copy: every single-op comparison becomes
   `_rts_c(site, a, b, op)`, every and/or operand `_rts_v(site, i, v)`. The
   source is copied unchanged; the instrumented code goes in an *unchecked
   hash-based .pyc*, so tracebacks/line numbers are unchanged.
3. The suite runs against that copy in ~68 chunks (slices of one test file,
   ≤6 recorded seconds each; exact node ids, never `--deselect`). With
   `RTS_FORK=1`, `rtsmap_plugin.py` collects the chunk once, then forks a
   fresh child per test, so every test is recorded from the same
   post-collection state (no caches warmed by earlier tests). Lines run
   during collection count for every test of the chunk. Lines via
   `sys.monitoring`, fixture-file opens via an audit hook; `sitecustomize.py`
   installs the recorder in every process, including spawned pool workers
   (each forked test gets its own child dir, so pool data is per test).
4. Output `/app/map.json` (format 3): one selection unit per test
   (`groups` = singletons), line→test-set id, opened file→test-set id,
   interned `sets`, and per site comparison facts `(a is b, a == b, order)` +
   small literal values + and/or truth values as `[[facts, set id], ...]`.
   ~10 MB. Build CPU ≈ 1.6× the v2 build (forks, repeated setUpClass).

Decision time (`selector.py`, `silent.py`), per patch:
- AST unchanged (comments, etc.) → `[]`.
- Data file (fixtures) → groups that opened it.
- Code edit → groups that ran the edited statements; in-place edits then go
  through the **silent prover** (`silent.py`): if every recorded evaluation of
  the edited comparison / and-or gives the same value old vs new, the group's
  process ran identically → drop the group. Handles operator swaps, literal
  operand changes, and/or swaps in boolean context. Inconclusive → keep.
- Test module edits: importers first (33 dialect test files import
  `Validator` from `tests/dialects/test_dialect.py`): an edit selects the
  file plus every test file importing it, transitively. If nothing imports
  it, `test_edit_targets` narrows to the edited `test_*` methods (in the
  class and its in-file subclasses), or the classes whose helpers /
  class-level code changed; module-level edits → whole file.
- Files no test can reach (`inert_file`): outside sqlglot/, tests/,
  sqlglotc/; not a pytest/packaging startup file; never opened during the
  build; never named in tests/ → nothing. (Docs, posts, .github, benchmarks,
  pdoc.) Checked by blanking all 36 such files: full suite still passes.
- A changed PEP 263 `coding:` line → everything that imports the file.
- Anything unanalysable → all tests.
- Split: LPT over k=1..8 workers, choose min(busiest + 2 s·k).
- Running candidates to check them exists (`PatchedTree.run`) but the
  cost rule nearly always prefers printing. `PatchedTree` (see CONTEXT.md)
  also builds the class-attribute path's import tree; the stress scripts use
  the same `apply` / `run` pair.

## Where the remaining cost is
Per-test coverage (v3) fixed fixture edits (151 → 1 test). What remains:
- Class-level constant edits: handled by c2 (below) when the edit only
  changes class-body data assignments. ktz3 went 1617 → 36 tests.
- Heavy tests dominate: `test_max_nodes` 28.8 s, tpcds/tpch executor tests.
  Any edit on a line they run costs ≥ 30 s unless proven silent.

## Class-attribute path (c2)
- Build: `buildtools/importstate.py` imports every `sqlglot.*` module and
  fingerprints every module global (`M:`) and reachable class attribute
  (`C:module.qualname|attr`) → `/app/map.state.json`. Its data attribute
  names go to `instrument.py`, which rewrites loads of those names to
  `_rts_a(obj, "NAME")` (and constant-name `getattr`/`hasattr`). The
  recorder keys each read by the defining class in the MRO; reads made while
  a sqlglot module is being imported are skipped (the state diff covers
  them). Map key `attrs`: `owner|name` → test set.
- Decide: only for edits whose changed statements are all class-body plain
  assignments (`class_body_data_edit`) and whose line-based cost is above
  5 s. Runs importstate on a patched tree (no tests/), diffs
  against the build state. Falls back to the line rule on: import failure,
  any `M:` change, an added/removed key, an unrecorded name, an enum value.
  Otherwise selects readers of each changed key ∪ tests whose files name the
  attribute ∪ unmapped tests.
- Soundness: `devtools/stress_attrs.py <n> <seed>` mutates class-body literals.

## Validating changes (do this before every submission)
1. Dev env (no Docker needed): Python 3.13, `pip install -r requirements.lock`,
   sqlglot at the pinned commit (no `pip install`; put it on PYTHONPATH).
2. Build a map: `python buildtools/build_map.py <sqlglot> tests.json map.json 4`
   — must report `incomplete groups []` and `baseline failures []`.
3. `RTS_REPO=<sqlglot> RTS_MAP=map.json python devtools/analyze.py sample/answers.json`
   — every line must say `MISS=0`.
4. Full run + score: `RTS_DATA=sample RTS_REPO=<sqlglot> RTS_MAP=map.json python selector.py > output.jsonl`
   then `python3 score.py sample sample/answers.json output.jsonl`.
5. Soundness of the prover: `devtools/stress.py <n> <seed>` generates operator
   mutants, runs every test the prover claims silent; must print `bugs: 0`.
   Likewise `devtools/stress_attrs.py` (class-body data edits) and
   `devtools/stress_tests.py` (test module edits).
   Build time and the real image: push to a `gate/*` branch; the GitHub
   Actions `gate` workflow builds on 4-core amd64 (c2: 327 s; v2 339 s, which
   passed the scorer) and runs the sample under the scorer's run flags.
6. Docker on this Mac: `make sample` runs under amd64 emulation and is very
   slow. For a correctness check build natively:
   `sed 's/@sha256:[a-f0-9]*//' Dockerfile > Dockerfile.native` and use the
   docker run flags from the Makefile (don't commit Dockerfile.native).

## Pitfalls already hit
- An and/or swap with equal truth values is NOT silent if the swapped
  operator short-circuits past operands with effects: `not fb and (atom :=
  self._parse_atom()) is not None` → `or` skips the call and the binding
  (stress seed 23, parser.py:6774, 44 tests). The prover now requires the
  skipped operands to be effect-free (names, constants, attributes, `not`,
  `is`). Present since v2.
- Some tests depend on run order: `test_dialect::test_enum`,
  `test_starrocks::test_time_slice_origin_roundtrip` and three
  `test_sqlglotc` tests fail on clean code when ~1600 tests run in one
  process sorted by duration. The stress scripts now rerun the same ids on
  clean code and only count failures the clean run doesn't reproduce.
- Recorder dedup of scalar pairs must include `a is b`: equal strings/ints
  are not always the same object. Without it, `==`→`is` was "proven" silent
  and a test failed (stress seed 7, spark2.py:307). Present in v1/v2 too.
- macOS `mktemp`/`/tmp` paths are symlinks (`/private/...`): pytest resolves
  them and node ids stop matching, so every test reads as "failed". Always
  `realpath` RTS_TMP locally (stress.py now does).
- This Mac has 8 GB and swaps hard: local map builds take ~60 min wall
  (both v2 and v3). Judge build time from CPU totals or Docker, not wall.
- `pytest --deselect X` matches by prefix: deselecting `test_foo` drops `test_foo__bar`.
- `test_optimizer` uses a spawn ProcessPoolExecutor; coverage must include
  children. Pool size follows `os.cpu_count()` → capped at 2 in the map build
  (huge slowdown otherwise).
- multiprocessing's resource tracker never runs sqlglot; children register
  for completeness only on their first sqlglot line.
- pytest resolves symlinked test dirs and then can't match node ids: the
  patched tree keeps a real copy of `tests/` in /tmp (64 MB limit).
- The recording copy must include the whole repo (`sqlglotc/` etc.), not just
  `sqlglot/` and `tests/`.
- Rules forbid tables from patch/kind-of-edit → failing tests. Facts about the
  unmodified program (coverage, recorded values) are allowed.

## Conventions
- No AI attribution lines in commit messages or PR descriptions.
- Commit only the files you mean to (don't add `Claude outputs/`, `output.jsonl`, `Dockerfile.native`).
