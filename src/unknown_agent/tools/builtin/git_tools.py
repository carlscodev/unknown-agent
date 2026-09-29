"""Mission-aware Git branch and worktree isolation.

Parallel agents editing one checkout are not parallel; they are a race condition.
This module gives every mutating mission node a dedicated branch and worktree,
records ownership in a WAL registry, and refuses destructive cleanup when the
worktree is dirty.

The manager never invokes a shell. Branch names are validated, every managed
path is constrained to a private sibling directory outside the primary worktree,
and merge conflicts leave the destination repository in its original state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

__all__ = [
    "GitCommandError",
    "WorktreeRecord",
    "WorktreeManager",
    "GitWorktreeCreateTool",
    "GitWorktreeListTool",
    "GitWorktreeStatusTool",
    "GitWorktreeCommitTool",
    "GitWorktreeMergePreviewTool",
    "GitWorktreeMergeTool",
    "GitWorktreeRemoveTool",
    "build_tools",
]

_BRANCH_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$")
_SEMANTIC_COMMIT = re.compile(
    r"^(feat|fix|refactor|test|docs|chore|perf|build|ci|style|revert)(\([^)]+\))?!?: .+"
)


class GitCommandError(RuntimeError):
    """A Git command failed with its safe diagnostic attached."""

    def __init__(self, args: list[str], code: int, output: str) -> None:
        self.args_list = args
        self.code = code
        self.output = output
        super().__init__(f"git {' '.join(args)} failed ({code}): {output[-1200:]}")


@dataclass(slots=True)
class WorktreeRecord:
    id: str
    branch: str
    path: str
    base: str
    node_key: str
    created_at: float
    status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "branch": self.branch,
            "path": self.path,
            "base": self.base,
            "node_key": self.node_key,
            "created_at": self.created_at,
            "status": self.status,
        }


class WorktreeManager:
    """Owns isolated Git worktrees for one repository."""

    def __init__(self, repo: Path | str, *, managed_root: Path | str | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.git_root = Path(self._git(["rev-parse", "--show-toplevel"]).strip()).resolve()
        digest = hashlib.sha256(str(self.git_root).encode()).hexdigest()[:12]
        default_root = self.git_root.parent / ".unknown-agent-worktrees" / digest
        self.managed_root = Path(managed_root).resolve() if managed_root else default_root
        self.managed_root.mkdir(parents=True, exist_ok=True)
        db = self.git_root / ".unknown" / "worktrees.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS worktrees (
                id TEXT PRIMARY KEY,
                branch TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL UNIQUE,
                base TEXT NOT NULL,
                node_key TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "WorktreeManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float = 120.0,
        check: bool = True,
    ) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        if check and completed.returncode != 0:
            raise GitCommandError(args, completed.returncode, output)
        return output

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.").lower()
        return slug[:72] or "task"

    def _validate_branch(self, branch: str) -> str:
        if not _BRANCH_SAFE.fullmatch(branch) or ".." in branch or branch.endswith(("/", ".lock")):
            raise ValueError(f"unsafe Git branch name {branch!r}")
        # Let Git enforce its additional ref-format rules.
        self._git(["check-ref-format", "--branch", branch])
        return branch

    def _managed_path(self, worktree_id: str) -> Path:
        candidate = (self.managed_root / worktree_id).resolve()
        if candidate.parent != self.managed_root:
            raise ValueError("managed worktree path escaped its root")
        return candidate

    def _record(self, row: sqlite3.Row) -> WorktreeRecord:
        return WorktreeRecord(
            id=row["id"],
            branch=row["branch"],
            path=row["path"],
            base=row["base"],
            node_key=row["node_key"],
            created_at=row["created_at"],
            status=row["status"],
        )

    def get(self, identifier: str) -> WorktreeRecord | None:
        row = self._conn.execute(
            "SELECT * FROM worktrees WHERE id=? OR branch=?", (identifier, identifier)
        ).fetchone()
        return self._record(row) if row else None

    def list(self, *, include_removed: bool = False) -> list[WorktreeRecord]:
        sql = "SELECT * FROM worktrees"
        if not include_removed:
            sql += " WHERE status != 'removed'"
        sql += " ORDER BY created_at DESC"
        return [self._record(row) for row in self._conn.execute(sql).fetchall()]

    def create(
        self,
        node_key: str,
        *,
        branch: str = "",
        base: str = "HEAD",
    ) -> WorktreeRecord:
        """Create a branch and checkout isolated from every other worker."""

        node_slug = self._slug(node_key)
        branch_name = self._validate_branch(branch or f"feature/subtask-{node_slug}")
        if self.get(branch_name) is not None:
            raise ValueError(f"branch {branch_name!r} already has a managed worktree")
        if self._git(["branch", "--list", branch_name]):
            raise ValueError(f"branch {branch_name!r} already exists")

        base_sha = self._git(["rev-parse", "--verify", base]).strip()
        short = hashlib.sha256(f"{branch_name}:{time.time_ns()}".encode()).hexdigest()[:8]
        worktree_id = f"wt-{node_slug[:32]}-{short}"
        path = self._managed_path(worktree_id)

        self._git(["worktree", "add", "-b", branch_name, str(path), base_sha], timeout=300.0)
        record = WorktreeRecord(
            id=worktree_id,
            branch=branch_name,
            path=str(path),
            base=base_sha,
            node_key=node_key,
            created_at=time.time(),
        )
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO worktrees(id,branch,path,base,node_key,created_at,status) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        record.id,
                        record.branch,
                        record.path,
                        record.base,
                        record.node_key,
                        record.created_at,
                        record.status,
                    ),
                )
        except Exception:
            self._git(["worktree", "remove", "--force", str(path)], check=False)
            self._git(["branch", "-D", branch_name], check=False)
            raise
        return record

    def status(self, identifier: str) -> dict[str, Any]:
        record = self.get(identifier)
        if record is None:
            raise KeyError(identifier)
        path = Path(record.path)
        exists = path.is_dir()
        porcelain = self._git(["status", "--porcelain=v1", "--branch"], cwd=path) if exists else ""
        lines = porcelain.splitlines()
        changes = lines[1:] if lines and lines[0].startswith("##") else lines
        return {
            **record.to_dict(),
            "exists": exists,
            "clean": exists and not changes,
            "head": self._git(["rev-parse", "HEAD"], cwd=path).strip() if exists else "",
            "changes": changes,
        }

    def commit(self, identifier: str, message: str) -> str:
        record = self.get(identifier)
        if record is None:
            raise KeyError(identifier)
        if not _SEMANTIC_COMMIT.fullmatch(message.strip()):
            raise ValueError(
                "commit message must be semantic, e.g. 'feat(engine): add resumable checkpoints'"
            )
        path = Path(record.path)
        self._git(["add", "--all"], cwd=path)
        staged = self._git(["diff", "--cached", "--name-only"], cwd=path)
        if not staged:
            raise ValueError("worktree has no changes to commit")
        self._git(["commit", "-m", message.strip()], cwd=path, timeout=300.0)
        return self._git(["rev-parse", "HEAD"], cwd=path).strip()

    def preview_merge(self, identifier: str) -> dict[str, Any]:
        """Compute the three-way merge without changing either checkout."""

        record = self.get(identifier)
        if record is None:
            raise KeyError(identifier)
        destination = self._git(["rev-parse", "HEAD"], cwd=self.git_root).strip()
        branch_sha = self._git(["rev-parse", record.branch]).strip()
        base = self._git(["merge-base", destination, branch_sha]).strip()
        output = self._git(["merge-tree", base, destination, branch_sha], timeout=300.0)
        conflicts = sorted(
            {
                match.group(1)
                for match in re.finditer(r"(?:changed in both|CONFLICT.*? in)\s+([^\n]+)", output)
            }
        )
        commits = self._git(["log", "--oneline", f"{destination}..{branch_sha}"]).splitlines()
        changed = self._git(["diff", "--name-status", f"{destination}...{branch_sha}"]).splitlines()
        return {
            "worktree": record.to_dict(),
            "destination": destination,
            "source": branch_sha,
            "merge_base": base,
            "clean": not conflicts,
            "conflicts": conflicts,
            "commits": commits,
            "changed_files": changed,
            "preview": output[-12000:],
        }

    def merge(self, identifier: str, *, message: str = "") -> str:
        """Merge a worker branch into the primary checkout with rollback on conflict."""

        record = self.get(identifier)
        if record is None:
            raise KeyError(identifier)
        if self._git(["status", "--porcelain"], cwd=self.git_root):
            raise ValueError("primary worktree is dirty; refusing to merge")
        preview = self.preview_merge(identifier)
        if not preview["clean"]:
            raise ValueError("merge preview contains conflicts: " + ", ".join(preview["conflicts"]))
        commit_message = message.strip() or f"merge: consolidate {record.branch}"
        try:
            self._git(
                ["merge", "--no-ff", record.branch, "-m", commit_message],
                cwd=self.git_root,
                timeout=300.0,
            )
        except GitCommandError:
            self._git(["merge", "--abort"], cwd=self.git_root, check=False)
            raise
        sha = self._git(["rev-parse", "HEAD"], cwd=self.git_root).strip()
        with self._conn:
            self._conn.execute("UPDATE worktrees SET status='merged' WHERE id=?", (record.id,))
        return sha

    def remove(
        self,
        identifier: str,
        *,
        delete_branch: bool = False,
        force: bool = False,
    ) -> None:
        record = self.get(identifier)
        if record is None:
            raise KeyError(identifier)
        path = Path(record.path)
        if path.exists():
            dirty = bool(self._git(["status", "--porcelain"], cwd=path))
            if dirty and not force:
                raise ValueError("worktree is dirty; commit it or pass force=true explicitly")
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(path))
            self._git(args, timeout=300.0)
        if delete_branch:
            # -d only deletes a branch Git considers merged. Force deletion is
            # intentionally not exposed here; preserving work is the default.
            self._git(["branch", "-d", record.branch])
        with self._conn:
            self._conn.execute("UPDATE worktrees SET status='removed' WHERE id=?", (record.id,))


def _manager(ctx: ToolContext) -> WorktreeManager:
    return WorktreeManager(ctx.project_root)


def _error(exc: Exception) -> ToolResult:
    if isinstance(exc, GitCommandError):
        return ToolResult.failure(str(exc), output=exc.output)
    return ToolResult.failure(str(exc))


class GitWorktreeCreateTool(Tool):
    name = "git_worktree_create"
    description = "Create an isolated branch and worktree for one mission node or parallel worker."
    category = "repo"
    risk = RiskLevel.WRITE
    tags = ("git", "worktree", "parallel")
    parameters = (
        ParameterSpec("node_key", "string", "Mission node or subtask id.", required=True),
        ParameterSpec("branch", "string", "Branch name; defaults to feature/subtask-<node>.", default=""),
        ParameterSpec("base", "string", "Commit/ref to branch from.", default="HEAD"),
    )

    async def run(
        self, ctx: ToolContext, node_key: str = "", branch: str = "", base: str = "HEAD"
    ) -> ToolResult:
        try:
            with _manager(ctx) as manager:
                record = manager.create(node_key, branch=branch, base=base)
            return ToolResult.success(
                f"created {record.branch} in {record.path}", worktree=record.to_dict()
            )
        except (GitCommandError, ValueError, sqlite3.Error) as exc:
            return _error(exc)


class GitWorktreeListTool(Tool):
    name = "git_worktree_list"
    description = "List managed agent worktrees and their owning mission nodes."
    category = "repo"
    risk = RiskLevel.READ
    tags = ("git", "worktree")
    parameters = (
        ParameterSpec("include_removed", "boolean", "Include historical removed entries.", default=False),
    )

    async def run(self, ctx: ToolContext, include_removed: bool = False) -> ToolResult:
        try:
            with _manager(ctx) as manager:
                records = manager.list(include_removed=include_removed)
            lines = [
                f"{item.id}  {item.status:<8}  {item.branch}  node={item.node_key}  {item.path}"
                for item in records
            ]
            return ToolResult.success(
                "\n".join(lines) if lines else "no managed worktrees",
                worktrees=[item.to_dict() for item in records],
            )
        except (GitCommandError, sqlite3.Error) as exc:
            return _error(exc)


class GitWorktreeStatusTool(Tool):
    name = "git_worktree_status"
    description = "Inspect one isolated worker checkout, including uncommitted files and HEAD."
    category = "repo"
    risk = RiskLevel.READ
    tags = ("git", "worktree")
    parameters = (ParameterSpec("id", "string", "Worktree id or branch.", required=True),)

    async def run(self, ctx: ToolContext, id: str = "") -> ToolResult:
        try:
            with _manager(ctx) as manager:
                status = manager.status(id)
            return ToolResult.success(json.dumps(status, indent=2), status=status)
        except (GitCommandError, KeyError, sqlite3.Error) as exc:
            return _error(exc)


class GitWorktreeCommitTool(Tool):
    name = "git_worktree_commit"
    description = "Stage and commit one worker's changes using a required semantic commit message."
    category = "repo"
    risk = RiskLevel.WRITE
    tags = ("git", "worktree")
    parameters = (
        ParameterSpec("id", "string", "Worktree id or branch.", required=True),
        ParameterSpec("message", "string", "Semantic commit message.", required=True),
    )

    async def run(self, ctx: ToolContext, id: str = "", message: str = "") -> ToolResult:
        try:
            with _manager(ctx) as manager:
                sha = manager.commit(id, message)
            return ToolResult.success(f"committed {sha[:12]}: {message}", commit=sha)
        except (GitCommandError, KeyError, ValueError, sqlite3.Error) as exc:
            return _error(exc)


class GitWorktreeMergePreviewTool(Tool):
    name = "git_worktree_merge_preview"
    description = "Preview a three-way merge without modifying either checkout; report conflicts and changed files."
    category = "repo"
    risk = RiskLevel.READ
    tags = ("git", "worktree", "merge")
    parameters = (ParameterSpec("id", "string", "Worktree id or branch.", required=True),)

    async def run(self, ctx: ToolContext, id: str = "") -> ToolResult:
        try:
            with _manager(ctx) as manager:
                preview = manager.preview_merge(id)
            state = "clean" if preview["clean"] else "CONFLICTING"
            lines = [
                f"merge preview: {state}",
                f"commits: {len(preview['commits'])}",
                f"changed files: {len(preview['changed_files'])}",
            ]
            if preview["conflicts"]:
                lines.append("conflicts: " + ", ".join(preview["conflicts"]))
            lines.extend(str(item) for item in preview["changed_files"])
            return ToolResult.success("\n".join(lines), preview=preview)
        except (GitCommandError, KeyError, sqlite3.Error) as exc:
            return _error(exc)


class GitWorktreeMergeTool(Tool):
    name = "git_worktree_merge"
    description = "Merge a clean worker branch into the primary checkout after a conflict-free preview."
    category = "repo"
    risk = RiskLevel.WRITE
    tags = ("git", "worktree", "merge")
    parameters = (
        ParameterSpec("id", "string", "Worktree id or branch.", required=True),
        ParameterSpec("message", "string", "Optional merge commit message.", default=""),
    )

    async def run(self, ctx: ToolContext, id: str = "", message: str = "") -> ToolResult:
        try:
            with _manager(ctx) as manager:
                sha = manager.merge(id, message=message)
            return ToolResult.success(f"merged {id} as {sha[:12]}", commit=sha)
        except (GitCommandError, KeyError, ValueError, sqlite3.Error) as exc:
            return _error(exc)


class GitWorktreeRemoveTool(Tool):
    name = "git_worktree_remove"
    description = "Remove a managed worktree; refuses dirty trees and unmerged branch deletion by default."
    category = "repo"
    risk = RiskLevel.WRITE
    tags = ("git", "worktree")
    parameters = (
        ParameterSpec("id", "string", "Worktree id or branch.", required=True),
        ParameterSpec("delete_branch", "boolean", "Also delete the branch if Git considers it merged.", default=False),
        ParameterSpec("force", "boolean", "Discard dirty worktree changes explicitly.", default=False),
    )

    async def run(
        self,
        ctx: ToolContext,
        id: str = "",
        delete_branch: bool = False,
        force: bool = False,
    ) -> ToolResult:
        try:
            with _manager(ctx) as manager:
                manager.remove(id, delete_branch=delete_branch, force=force)
            return ToolResult.success(f"removed managed worktree {id}")
        except (GitCommandError, KeyError, ValueError, sqlite3.Error) as exc:
            return _error(exc)


def build_tools() -> list[Tool]:
    return [
        GitWorktreeCreateTool(),
        GitWorktreeListTool(),
        GitWorktreeStatusTool(),
        GitWorktreeCommitTool(),
        GitWorktreeMergePreviewTool(),
        GitWorktreeMergeTool(),
        GitWorktreeRemoveTool(),
    ]
