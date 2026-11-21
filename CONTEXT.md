# Blast Radius — domain terms

**Patched tree**: a directory laid out like the pinned sqlglot with one
commit's changed files in place: `sqlglot/` as symlinks with the changed
modules as real files, and optionally a real copy of `tests/` with the changed
test files written in. One tree is reused across commits; applying a commit
undoes the previous one. The class-attribute path imports from a tree without
`tests/`; verification and the stress scripts run pytest in one with
`tests/`. Code: `PatchedTree` in `selector.py`.
_Avoid_: workspace, mirror.
