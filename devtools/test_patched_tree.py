"""PatchedTree against a three-file fake repo.

    python -m pytest devtools/test_patched_tree.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector import PatchedTree  # noqa: E402

TEST_SRC = (
    "from sqlglot.sub.b import VALUE\n\n"
    "def test_value():\n    assert VALUE == 1\n\n"
    "def test_other():\n    assert True\n"
)


def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "sqlglot" / "sub").mkdir(parents=True)
    (repo / "sqlglot" / "__init__.py").write_text("")
    (repo / "sqlglot" / "sub" / "__init__.py").write_text("")
    (repo / "sqlglot" / "sub" / "b.py").write_text("VALUE = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text(TEST_SRC)
    return repo


def test_no_source_change_links_whole_package(tmp_path):
    repo = fake_repo(tmp_path)
    root = PatchedTree(tmp_path / "w", repo).apply({})
    assert (root / "sqlglot").is_symlink()
    assert (root / "tests").is_dir() and not (root / "tests").is_symlink()


def test_changed_module_is_real_file_rest_are_links(tmp_path):
    repo = fake_repo(tmp_path)
    root = PatchedTree(tmp_path / "w", repo).apply({"sqlglot/sub/b.py": "VALUE = 2\n"})
    assert not (root / "sqlglot").is_symlink()
    assert not (root / "sqlglot" / "sub").is_symlink()
    assert (root / "sqlglot" / "__init__.py").is_symlink()
    assert (root / "sqlglot" / "sub" / "__init__.py").is_symlink()
    b = root / "sqlglot" / "sub" / "b.py"
    assert not b.is_symlink() and b.read_text() == "VALUE = 2\n"
    assert (repo / "sqlglot" / "sub" / "b.py").read_text() == "VALUE = 1\n"


def test_next_apply_undoes_the_previous_one(tmp_path):
    repo = fake_repo(tmp_path)
    tree = PatchedTree(tmp_path / "w", repo)
    tree.apply({"sqlglot/sub/b.py": "VALUE = 2\n", "tests/test_x.py": "# edited\n"})
    root = tree.apply({})
    assert (root / "sqlglot").is_symlink()
    assert (root / "tests" / "test_x.py").read_text() == TEST_SRC
    assert (repo / "tests" / "test_x.py").read_text() == TEST_SRC


def test_without_tests_ignores_test_edits(tmp_path):
    repo = fake_repo(tmp_path)
    tree = PatchedTree(tmp_path / "st", repo, with_tests=False)
    root = tree.apply({"sqlglot/sub/b.py": "VALUE = 2\n", "tests/test_x.py": "# edited\n"})
    assert not (root / "tests").exists()
    assert (root / "sqlglot" / "sub" / "b.py").read_text() == "VALUE = 2\n"


def test_run_reports_failures_of_the_patched_code(tmp_path):
    repo = fake_repo(tmp_path)
    tree = PatchedTree(tmp_path / "w", repo)
    ids = ["tests/test_x.py::test_value", "tests/test_x.py::test_other"]
    tree.apply({})
    assert tree.run(ids, timeout=60)[0] == set()
    tree.apply({"sqlglot/sub/b.py": "VALUE = 2\n"})
    assert tree.run(ids, timeout=60)[0] == {"tests/test_x.py::test_value"}
    tree.apply({"tests/test_x.py": TEST_SRC + "\ndef test_new():\n    assert False\n"})
    assert tree.run(ids + ["tests/test_x.py::test_new"], timeout=60)[0] == {"tests/test_x.py::test_new"}
    # An id pytest cannot find stops the whole run: every id counts as failed.
    gone = ids + ["tests/test_x.py::test_gone"]
    assert tree.run(gone, timeout=60)[0] == set(gone)
