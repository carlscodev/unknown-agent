"""Execution tools: shell, tests, builds, lint, typecheck, logs, HTTP.

These are the tools that turn "the model wrote code" into "the project actually
runs".  They share one contract:

* the command comes from the *project's own* configuration when available
  (``.unknown/config.toml`` ``[project.commands]``) or from detection, never
  from a guess;
* exit codes are captured, never raised - a failing test run is data the agent
  must reason about, not a crash;
* output is bounded (head+tail) and the full log is written to
  ``.unknown/artifacts`` when it is long, so nothing is silently lost;
* timeouts kill the whole process group so no orphan dev server survives.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.host.process import run_process, split_command
from unknown_agent.host.shell import plan_command
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

TEST_HINTS = ("test",)
BUILD_HINTS = ("build",)
LINT_HINTS = ("lint",)
TYPECHECK_HINTS = ("typecheck", "type-check", "tsc", "mypy")


def project_command(ctx: ToolContext, *names: str) -> str | None:
    """Look up a named command from project config, then from detection."""

    commands = ctx.settings.project.commands or {}
    for name in names:
        if commands.get(name):
            return commands[name]
    project = ctx.project
    detected = getattr(project, "commands", None) or {}
    for name in names:
        if detected.get(name):
            return detected[name]  # type: ignore[union-attr]
    return None


class ShellRunTool(Tool):
    name = "shell_run"
    description = (
        "Run a shell command in the project. The command is policy-checked (denylist for "
        "catastrophic patterns), executed without a shell when possible, and bounded by a "
        "timeout. Use for git, package managers, scripts and diagnostics - not for editing files."
    )
    category = "execution"
    risk = RiskLevel.EXECUTE
    tags = ("run", "primary")
    timeout_s = 900.0
    parameters = (
        ParameterSpec("command", "string", "Command line to execute.", required=True, examples=("npm run lint",)),
        ParameterSpec("cwd", "string", "Working directory relative to the project root.", default="."),
        ParameterSpec("timeout_s", "number", "Kill the command after this many seconds.", default=300, minimum=1, maximum=3600),
        ParameterSpec("env", "object", "Extra environment variables.", default=None),
        ParameterSpec("allow_shell", "boolean", "Allow shell operators (|, &&, >) via /bin/sh. Default: false.", default=False),
    )
    examples = ('ua tools call shell_run --arg command="git status --short"',)

    async def run(self, ctx: ToolContext, command: str, cwd: str = ".", timeout_s: float = 300, env: dict[str, str] | None = None, allow_shell: bool = False) -> ToolResult:
        workdir = (ctx.project_root / cwd).resolve()
        if not workdir.is_dir():
            return ToolResult.failure(f"Working directory does not exist: {workdir}")
        plan = plan_command(command, allow_shell=allow_shell)
        if plan.use_shell and not allow_shell and "shell operators" in plan.reason:
            return ToolResult.failure(
                "Command uses shell operators. Re-run with allow_shell=true if the pipeline is intentional, "
                "or split it into separate commands.",
                output=f"command: {command}",
            )
        result = await run_process(plan.argv, cwd=workdir, timeout=timeout_s, env=env, label=command)
        ctx.emit("observation", f"$ {command} -> exit {result.exit_code} ({result.duration_s:.1f}s)", exit_code=result.exit_code)
        summary = _summarise_process(command, result)
        artifact = None
        if len(result.combined()) > 20_000:
            artifact = str(ctx.artifact_path(f"shell-{int(time.time())}.log"))
            Path(artifact).write_text(result.combined(), encoding="utf-8")
        return ToolResult(
            ok=result.ok,
            output=summary,
            error=None if result.ok else _error_for(result, timeout_s),
            duration_ms=result.duration_s * 1000,
            truncated=result.truncated,
            artifacts=[artifact] if artifact else [],
            data={"exit_code": result.exit_code, "duration_s": round(result.duration_s, 2), "timed_out": result.timed_out, "command": command},
        )


class _ProjectCommandTool(Tool):
    """Shared implementation for test/build/lint/typecheck tools."""

    risk = RiskLevel.EXECUTE
    category = "execution"
    timeout_s = 1800.0
    command_names: tuple[str, ...] = ()
    fallback_commands: tuple[str, ...] = ()
    parameters = (
        ParameterSpec("target", "string", "Optional file/directory/pattern argument passed to the command.", default=""),
        ParameterSpec("cwd", "string", "Working directory relative to the project root.", default="."),
        ParameterSpec("timeout_s", "number", "Kill after this many seconds.", default=600, minimum=1, maximum=3600),
        ParameterSpec("command", "string", "Override the detected command.", default=""),
    )

    def resolve_command(self, ctx: ToolContext, override: str, target: str) -> str | None:
        if override:
            return override
        base = project_command(ctx, *self.command_names)
        if base is None:
            for candidate in self.fallback_commands:
                parts = split_command(candidate)
                if parts and shutil.which(parts[0]):
                    base = candidate
                    break
        if base is None:
            return None
        return f"{base} {target}".strip() if target else base

    async def run(self, ctx: ToolContext, target: str = "", cwd: str = ".", timeout_s: float = 600, command: str = "") -> ToolResult:
        resolved = self.resolve_command(ctx, command, target)
        if resolved is None:
            return ToolResult.failure(
                f"No {self.name.replace('_run', '')} command was detected for this project.",
                output=_no_command_help(ctx, self.name),
            )
        workdir = (ctx.project_root / cwd).resolve()
        plan = plan_command(resolved, allow_shell=True)
        result = await run_process(plan.argv, cwd=workdir, timeout=timeout_s, label=resolved)
        ctx.emit("observation", f"{self.name}: {resolved} -> exit {result.exit_code}", exit_code=result.exit_code, duration_s=round(result.duration_s, 2))
        artifacts: list[str] = []
        if len(result.combined()) > 20_000:
            artifact = str(ctx.artifact_path(f"{self.name}-{int(time.time())}.log"))
            Path(artifact).write_text(result.combined(), encoding="utf-8")
            artifacts.append(artifact)
        return ToolResult(
            ok=result.ok,
            output=_summarise_process(resolved, result),
            error=None if result.ok else _error_for(result, self.timeout_s, label=self.name),
            duration_ms=result.duration_s * 1000,
            truncated=result.truncated,
            artifacts=artifacts,
            data={
                "command": resolved,
                "exit_code": result.exit_code,
                "duration_s": round(result.duration_s, 2),
                "timed_out": result.timed_out,
                "failures": _extract_failures(result.combined()),
            },
        )


class TestRunTool(_ProjectCommandTool):
    name = "test_run"
    description = "Run the project's test suite and return failures with the exact assertion output."
    command_names = ("test",)
    fallback_commands = ("pytest -q", "npm test --silent", "pnpm test --silent", "go test ./...", "cargo test")
    tags = ("verify", "primary")


class BuildRunTool(_ProjectCommandTool):
    name = "build_run"
    description = "Build the project and report the compiler/bundler errors verbatim."
    command_names = ("build",)
    fallback_commands = ("npm run build", "pnpm build", "go build ./...", "cargo build")
    tags = ("verify",)


class LintRunTool(_ProjectCommandTool):
    name = "lint_run"
    description = "Run the configured linter and return violations grouped by file."
    command_names = ("lint",)
    fallback_commands = ("ruff check .", "npx eslint .", "npm run lint")
    tags = ("verify",)


class TypecheckRunTool(_ProjectCommandTool):
    name = "typecheck_run"
    description = "Run the configured type checker (mypy, tsc, pyright) and return type errors."
    command_names = ("typecheck", "type-check", "types")
    fallback_commands = ("mypy .", "npx tsc --noEmit")
    tags = ("verify",)


class ReadLogsTool(Tool):
    name = "read_logs"
    description = "Read recent log files from the project (or the agent's own logs) with tail/head/grep filtering."
    category = "execution"
    risk = RiskLevel.READ
    parameters = (
        ParameterSpec("path", "string", "Log file or directory. Defaults to the project's log directories.", default=""),
        ParameterSpec("lines", "integer", "Number of trailing lines.", default=100, minimum=1, maximum=5000),
        ParameterSpec("grep", "string", "Only show lines matching this regex.", default=""),
        ParameterSpec("agent_log", "boolean", "Read Unknown Agent's own log instead of project logs.", default=False),
    )

    async def run(self, ctx: ToolContext, path: str = "", lines: int = 100, grep: str = "", agent_log: bool = False) -> ToolResult:
        import re

        if agent_log:
            candidates = [ctx.paths.log_file]
        elif path:
            target = (ctx.project_root / path).resolve()
            candidates = [target] if target.is_file() else sorted(target.glob("**/*.log"))[:20]
        else:
            candidates = sorted(
                set(
                    list(ctx.project_root.glob("**/*.log"))[:40]
                    + list((ctx.project_root / "logs").glob("**/*.log"))[:20]
                    + list(ctx.paths.project_logs_dir.glob("**/*.log"))[:20]
                )
            )
        if not candidates:
            return ToolResult.success("No log files found.", files=0)
        chunks: list[str] = []
        pattern = re.compile(grep, re.IGNORECASE) if grep else None
        for candidate in candidates[:5]:
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            selected = text.splitlines()
            if pattern:
                selected = [line for line in selected if pattern.search(line)]
            tail = selected[-lines:]
            chunks.append(f"--- {candidate} ({len(tail)} lines) ---")
            chunks.extend(tail)
        body = "\n".join(chunks) if chunks else "No readable log content."
        return ToolResult.success(body, files=len(candidates))


class HttpFetchTool(Tool):
    name = "http_fetch"
    description = (
        "Fetch a URL (JSON, HTML, text) for API debugging and dependency checks. "
        "Respects policy.allow_network. Returns status, headers and a bounded body."
    )
    category = "network"
    risk = RiskLevel.NETWORK
    timeout_s = 60.0
    parameters = (
        ParameterSpec("url", "string", "Absolute URL.", required=True),
        ParameterSpec("method", "string", "HTTP method.", default="GET", enum=("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")),
        ParameterSpec("headers", "object", "Request headers.", default=None),
        ParameterSpec("json_body", "object", "JSON request body.", default=None),
        ParameterSpec("max_chars", "integer", "Maximum response characters to return.", default=8000, minimum=100, maximum=200_000),
    )

    async def run(self, ctx: ToolContext, url: str, method: str = "GET", headers: dict[str, str] | None = None, json_body: dict[str, Any] | None = None, max_chars: int = 8000) -> ToolResult:
        import httpx

        if not url.startswith(("http://", "https://")):
            return ToolResult.failure("URL must start with http:// or https://")
        client = ctx.http_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            response = await client.request(method, url, headers=headers or None, json=json_body or None)
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Request failed: {exc}")
        finally:
            if owns_client:
                await client.aclose()
        body = response.text[:max_chars]
        ctx.emit("observation", f"{method} {url} -> {response.status_code}")
        return ToolResult(
            ok=response.status_code < 400,
            output=f"HTTP {response.status_code}\n\n{body}",
            error=None if response.status_code < 400 else f"HTTP {response.status_code}",
            data={"status": response.status_code, "content_type": response.headers.get("content-type", ""), "length": len(response.content)},
        )


def _error_for(result: Any, timeout_s: float, *, label: str = "command") -> str:
    """One-line reason a command failed, with timeouts called out as such."""

    if result.timed_out:
        return f"{label} timed out after {timeout_s:g}s (killed the process group); raise timeout_s or speed the command up"
    if result.exit_code is not None and result.exit_code < 0:
        return f"{label} was killed by signal {-result.exit_code}"
    return f"{label} failed with exit code {result.exit_code}"


def _summarise_process(command: str, result: Any) -> str:
    head = [f"$ {command}", f"exit code: {result.exit_code}  duration: {result.duration_s:.1f}s" + ("  [TIMED OUT]" if result.timed_out else "")]
    body = result.combined()
    if not body:
        return "\n".join(head) + "\n(no output)"
    lines = body.splitlines()
    if len(lines) > 400:
        body = "\n".join(lines[:200] + [f"... [{len(lines) - 400} lines omitted] ..."] + lines[-200:])
    return "\n".join(head) + "\n" + body


def _extract_failures(output: str) -> list[str]:
    import re

    patterns = [
        re.compile(r"^(FAILED|ERROR)\s+(.+)$", re.MULTILINE),
        re.compile(r"^\s*(?:✕|×|✗)\s*(.+)$", re.MULTILINE),
        re.compile(r"^(.+?):\s*error\s+(.+)$", re.MULTILINE | re.IGNORECASE),
    ]
    found: list[str] = []
    for pattern in patterns:
        for match in pattern.findall(output):
            line = " ".join(match) if isinstance(match, tuple) else match
            found.append(line.strip()[:240])
            if len(found) >= 20:
                return found
    return found


def _no_command_help(ctx: ToolContext, tool: str) -> str:
    return (
        f"Unknown Agent did not find a command for {tool}.\n"
        "Fix it by declaring the command explicitly in .unknown/config.toml:\n\n"
        "  [project.commands]\n"
        '  test = "pytest -q"\n'
        '  build = "npm run build"\n\n'
        "Run `ua inspect` to see what was detected, or pass command=... to this tool."
    )


def build_tools() -> list[Tool]:
    return [
        ShellRunTool(),
        TestRunTool(),
        BuildRunTool(),
        LintRunTool(),
        TypecheckRunTool(),
        ReadLogsTool(),
        HttpFetchTool(),
    ]


__all__ = [
    "ShellRunTool",
    "TestRunTool",
    "BuildRunTool",
    "LintRunTool",
    "TypecheckRunTool",
    "ReadLogsTool",
    "HttpFetchTool",
    "project_command",
    "build_tools",
]
