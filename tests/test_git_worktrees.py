"""Safety and lifecycle tests for mission-owned Git worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from unknown_agent.tools.builtin.git_tools import WorktreeManager


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test Agent")
    git(root, "config", "user.email", "agent@example.test")
    (root / ".gitignore").write_text(".unknown/\n", encoding="utf-8")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "chore: initial state")
    return root


def test_create_isolates_a_worker_outside_the_primary_checkout(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        record = manager.create("api-17")
        path = Path(record.path)

        assert record.branch == "feature/subtask-api-17"
        assert path.is_dir()
        assert not path.is_relative_to(repo)
        assert (path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert manager.status(record.id)["clean"]


def test_duplicate_branch_is_refused(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        manager.create("same")
        with pytest.raises(ValueError, match="already"):
            manager.create("same")


def test_unsafe_branch_is_refused(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        with pytest.raises(ValueError, match="unsafe"):
            manager.create("escape", branch="../outside")


def test_dirty_worktree_cannot_be_removed_accidentally(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        record = manager.create("dirty")
        (Path(record.path) / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="dirty"):
            manager.remove(record.id)
        assert Path(record.path).exists()


def test_commit_requires_a_semantic_message(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        record = manager.create("commit")
        (Path(record.path) / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="semantic"):
            manager.commit(record.id, "changed stuff")

        sha = manager.commit(record.id, "feat(core): change value")
        assert len(sha) == 40
        assert manager.status(record.id)["clean"]


def test_preview_is_non_mutating(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        record = manager.create("preview")
        (Path(record.path) / "new.py").write_text("NEW = True\n", encoding="utf-8")
        manager.commit(record.id, "feat: add isolated file")
        before = git(repo, "rev-parse", "HEAD")

        preview = manager.preview_merge(record.id)

        assert preview["clean"]
        assert any("new.py" in line for line in preview["changed_files"])
        assert git(repo, "rev-parse", "HEAD") == before
        assert not (repo / "new.py").exists()


def test_merge_consolidates_then_allows_safe_cleanup(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        record = manager.create("merge")
        (Path(record.path) / "new.py").write_text("NEW = True\n", encoding="utf-8")
        manager.commit(record.id, "feat: add isolated file")

        merged_sha = manager.merge(record.id)
        assert git(repo, "rev-parse", "HEAD") == merged_sha
        assert (repo / "new.py").exists()
        assert manager.get(record.id).status == "merged"  # type: ignore[union-attr]

        manager.remove(record.id, delete_branch=True)
        assert not Path(record.path).exists()
        assert manager.get(record.id).status == "removed"  # type: ignore[union-attr]
        assert not git(repo, "branch", "--list", record.branch)


def test_merge_refuses_a_dirty_primary_checkout(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        record = manager.create("blocked")
        (Path(record.path) / "new.py").write_text("NEW = True\n", encoding="utf-8")
        manager.commit(record.id, "feat: add isolated file")
        (repo / "app.py").write_text("LOCAL = 'dirty'\n", encoding="utf-8")

        with pytest.raises(ValueError, match="primary worktree is dirty"):
            manager.merge(record.id)


def test_registry_uses_wal(repo: Path) -> None:
    with WorktreeManager(repo) as manager:
        mode = manager._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
