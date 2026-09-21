"""Repository intelligence tools: inspect, git state, git diff, git log, commit, package info.

The agent must understand a repository before it edits it.  These tools give it
deterministic, cheap facts (no model call involved): detected stack, package
manager, commands, entry points, git status and diffs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.host.process import run_process
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext


class InspectProjectTool(Tool):
    name = "inspect_project"
    description = (
        "Return the project intelligence report: detected languages, frameworks, package manager, "
        "test/build/lint commands, entry points, directory layout and risk notes. Call this first "
        "when you need to understand an unfamiliar repository."
    )
    category = "repo"
    risk = RiskLevel.READ
    tags = ("inspect", "primary")
    parameters = (
        ParameterSpec("refresh", "boolean", "Recompute instead of using the cached report.", default=False),
    )
    examples = ("ua tools call inspect_project",)

    async def run(self, ctx: ToolContext, refresh: bool = False) -> ToolResult:
        from unknown_agent.context.project import load_project_context

        project = ctx.project
        if project is None or refresh:
            project = load_project_context(ctx.project_root, ctx.settings)
            ctx.project = project
        report = project.render()
        return ToolResult.success(report, kind=getattr(project, "kind", None), languages=getattr(project, "languages", []), commands=getattr(project, "commands", {}))


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show the working tree status, current branch, upstream divergence and recent commits."
    category = "repo"
    risk = RiskLevel.READ
    tags = ("git", "inspect")
    parameters = (ParameterSpec("include_recent", "boolean", "Include the last five commits.", default=True),)

    async def run(self, ctx: ToolContext, include_recent: bool = True) -> ToolResult:
        if not (ctx.project_root / ".git").exists():
            return ToolResult.success("Not a git repository.", is_git=False)
        status = await run_process(["git", "status", "--short", "--branch"], cwd=ctx.project_root, timeout=30)
        log = await run_process(["git", "log", "--oneline", "-5"], cwd=ctx.project_root, timeout=30) if include_recent else None
        body = status.combined()
        if log is not None:
            body += "\n\nrecent commits:\n" + log.combined()
        changed = [line[3:] for line in status.stdout.splitlines() if line[:2].strip() and not line.startswith("##")]
        return ToolResult.success(body.strip() or "(clean)", changed_files=changed, is_git=True)


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show the diff of unstaged changes, staged changes, or against a revision. Use before finalising work."
    category = "repo"
    risk = RiskLevel.READ
    tags = ("git",)
    parameters = (
        ParameterSpec("staged", "boolean", "Diff staged changes instead of the working tree.", default=False),
        ParameterSpec("path", "string", "Limit the diff to a path.", default=""),
        ParameterSpec("base", "string", "Diff against this revision (e.g. HEAD~1, main).", default=""),
        ParameterSpec("stat_only", "boolean", "Return only the diffstat.", default=False),
    )

    async def run(self, ctx: ToolContext, staged: bool = False, path: str = "", base: str = "", stat_only: bool = False) -> ToolResult:
        argv = ["git", "diff", "--no-color"]
        if staged:
            argv.append("--cached")
        if stat_only:
            argv.append("--stat")
        if base:
            argv.append(base)
        if path:
            argv.extend(["--", path])
        result = await run_process(argv, cwd=ctx.project_root, timeout=60)
        if result.exit_code != 0:
            return ToolResult.failure(result.combined()[:2000])
        text = result.combined() or "(no changes)"
        files = [line.split(" b/")[-1] for line in text.splitlines() if line.startswith("diff --git")]
        return ToolResult.success(text[:40_000], files=files, empty=text == "(no changes)")


class GitLogTool(Tool):
    name = "git_log"
    description = "Show recent commit history with optional path filtering and a search pattern."
    category = "repo"
    risk = RiskLevel.READ
    parameters = (
        ParameterSpec("limit", "integer", "Number of commits.", default=20, minimum=1, maximum=500),
        ParameterSpec("path", "string", "Limit history to a path.", default=""),
        ParameterSpec("grep", "string", "Filter commits whose message matches this text.", default=""),
    )

    async def run(self, ctx: ToolContext, limit: int = 20, path: str = "", grep: str = "") -> ToolResult:
        argv = ["git", "log", f"-{limit}", "--oneline", "--no-decorate"]
        if grep:
            argv.extend(["--grep", grep, "-i"])
        if path:
            argv.extend(["--", path])
        result = await run_process(argv, cwd=ctx.project_root, timeout=60)
        if result.exit_code != 0:
            return ToolResult.failure(result.combined()[:1000])
        return ToolResult.success(result.combined() or "(no commits)")


class GitCommitTool(Tool):
    name = "git_commit"
    description = (
        "Stage the given paths and create a commit using the project's conventions. "
        "Never amends or force-pushes. Requires an explicit message."
    )
    category = "repo"
    risk = RiskLevel.WRITE
    parameters = (
        ParameterSpec("message", "string", "Commit message.", required=True),
        ParameterSpec("paths", "array", "Paths to stage (default: everything already tracked plus new files under the project).", default=None, items="string"),
    )

    async def run(self, ctx: ToolContext, message: str, paths: list[str] | None = None) -> ToolResult:
        if not message.strip():
            return ToolResult.failure("A commit message is required.")
        if not (ctx.project_root / ".git").exists():
            return ToolResult.failure("Not a git repository.")
        if paths:
            add = await run_process(["git", "add", "--", *paths], cwd=ctx.project_root, timeout=60)
        else:
            add = await run_process(["git", "add", "-A"], cwd=ctx.project_root, timeout=60)
        if add.exit_code != 0:
            return ToolResult.failure(f"git add failed: {add.combined()[:500]}")
        commit = await run_process(["git", "commit", "-m", message], cwd=ctx.project_root, timeout=120)
        if commit.exit_code != 0 and "nothing to commit" in commit.combined().lower():
            return ToolResult.success("Nothing to commit: the working tree is clean.")
        if commit.exit_code != 0:
            return ToolResult.failure(f"git commit failed: {commit.combined()[:800]}")
        ctx.emit("observation", f"committed: {message.splitlines()[0][:80]}")
        return ToolResult.success(commit.combined()[:2000], message=message)


class PackageInfoTool(Tool):
    name = "package_info"
    description = "Summarise dependency manifests (pyproject/requirements/package.json/go.mod/Cargo.toml/composer.json) and lockfile state."
    category = "repo"
    risk = RiskLevel.READ
    parameters = (ParameterSpec("which", "string", "Manifest to read; empty means all detected.", default=""),)

    async def run(self, ctx: ToolContext, which: str = "") -> ToolResult:
        manifests = [
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "package.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "package-lock.json",
            "go.mod",
            "Cargo.toml",
            "composer.json",
            "Gemfile",
            "pom.xml",
            "build.gradle",
            "Dockerfile",
            "docker-compose.yml",
        ]
        chunks: list[str] = []
        for name in manifests:
            if which and Path(which).name != name:
                continue
            path = ctx.project_root / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.name == "package.json":
                try:
                    payload = json.loads(text)
                    merged = {
                        "name": payload.get("name"),
                        "scripts": payload.get("scripts"),
                        "dependencies": sorted((payload.get("dependencies") or {}).keys()),
                        "devDependencies": sorted((payload.get("devDependencies") or {}).keys()),
                        "packageManager": payload.get("packageManager"),
                        "engines": payload.get("engines"),
                        "workspaces": payload.get("workspaces"),
                    }
                    text = json.dumps(merged, indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            chunks.append(f"--- {name} ---\n{text[:4000]}")
        if not chunks:
            return ToolResult.success("No dependency manifests found.")
        return ToolResult.success("\n\n".join(chunks), manifests=len(chunks))


class ProjectNoteTool(Tool):
    name = "project_note"
    description = (
        "Persist a project fact or convention to long-term memory (architecture notes, invariants, "
        "learned gotchas). Use sparingly: only durable, reusable knowledge - not step-by-step logs."
    )
    category = "memory"
    risk = RiskLevel.WRITE
    tags = ("memory",)
    parameters = (
        ParameterSpec("text", "string", "The fact to remember.", required=True),
        ParameterSpec("kind", "string", "convention | architecture | gotcha | decision | command | fact", default="fact", enum=("convention", "architecture", "gotcha", "decision", "command", "fact")),
        ParameterSpec("tags", "array", "Search tags.", default=None, items="string"),
        ParameterSpec("confidence", "number", "0-1 confidence in the fact.", default=0.8, minimum=0.0, maximum=1.0),
    )

    async def run(self, ctx: ToolContext, text: str, kind: str = "fact", tags: list[str] | None = None, confidence: float = 0.8) -> ToolResult:
        memory = ctx.memory
        if memory is None:
            return ToolResult.failure("Memory store is not available in this session.")
        fact_id = memory.remember(text, kind=kind, tags=tags or [], source=f"run:{ctx.run_id}", confidence=confidence)
        ctx.emit("memory.write", f"remembered ({kind}): {text[:80]}", fact_id=fact_id)
        return ToolResult.success(f"stored fact #{fact_id} ({kind})", fact_id=fact_id)


def build_tools() -> list[Tool]:
    return [
        InspectProjectTool(),
        GitStatusTool(),
        GitDiffTool(),
        GitLogTool(),
        GitCommitTool(),
        PackageInfoTool(),
        ProjectNoteTool(),
    ]


__all__ = [
    "InspectProjectTool",
    "GitStatusTool",
    "GitDiffTool",
    "GitLogTool",
    "GitCommitTool",
    "PackageInfoTool",
    "ProjectNoteTool",
    "build_tools",
]
