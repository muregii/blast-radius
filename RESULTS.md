# Results log

One row per practice submission. Sample = local simulated score on the 30
sample commits (`score.py`); Board = practice leaderboard (middle of 3 runs).

| Date (EST) | Commit | Idea | Sample | Board | Misses | CPU s | Learned |
|---|---|---|---|---|---|---|---|
| 2026-11-20 | e60dec6 | starter kit | — | 1614.1 | — | — | baseline |
| 2026-11-20 | 08354ee | v1 line coverage per test file | 268.2 | 327.4 | 0 | — | coverage alone cuts ~80% |
| 2026-11-21 | 4fe3624 | v2 silent-mutation proofs | 212.5 | 317.0 | 0 | — | proofs help on the sample, much less on the board |
| 2026-11-21 | 0f7a851 | v3 per-test coverage (forked children) | 168.8 | not submitted | — | — | superseded by c2 before its gate finished |
| 2026-11-21 | 55118fa | c2 class-attribute reads + and/or short-circuit fix | 127.7 | 252.5 | 0 | 7.3 | board gap persists (sample −40%, board −20%); importstate subprocess raised CPU 2.2 → 7.3 s |
