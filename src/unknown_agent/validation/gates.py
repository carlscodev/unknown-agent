"""Quality gates: the concrete checks that define "done".

Each gate is a small object with a name, a required/advisory policy, a way to
decide whether it applies to this project, and a run method that returns a
:class:`~unknown_agent.core.models.GateResult` with evidence (exit code, output,
duration, artifacts, fix hint).

Gates are auto-detected from the project's own tooling - the agent never invents
a command.  When detection finds nothing, the gate reports ``skipped`` with the
reason, which keeps reports honest.
"""

from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.core.models import GateResult, GateStatus
from unknown_agent.host.process import ProcessResult, run_process
from unknown_agent.host.shell import normalize_package_manager_command
from unknown_agent.observability.logging import get_logger

log = get_logger("validation.gates")

MAX_GATE_OUTPUT = 12_000


@dataclass
class GateContext:
    """Everything a gate needs to run."""

    settings: Settings
    project_root: Path
    project: Any = None
    browser: Any = None
    base_url: str | None = None
    changed_files: list[str] | None = None
    events: Any = None
    run_id: str = ""

    def command(self, *names: str) -> str | None:
        commands = dict(getattr(self.project, "commands", {}) or {})
        commands.update(self.settings.project.commands or {})
        for name in names:
            if commands.get(name):
                return str(commands[name])
        return None

    def emit(self, event_type: str, message: str, **data: Any) -> None:
        if self.events is None:
            return
        from unknown_agent.observability.events import Event

        self.events.emit(Event(type=event_type, run_id=self.run_id, message=message, data=data))  # type: ignore[arg-type]


class Gate:
    """Base class for a validation gate."""

    name: str = "gate"
    description: str = ""
    #: Tools/binaries that must exist for the gate to be applicable.
    requires: tuple[str, ...] = ()

    def applicable(self, ctx: GateContext) -> tuple[bool, str]:
        """Return ``(applies, reason_if_not)``."""

        return True, ""

    async def run(self, ctx: GateContext) -> GateResult:
        raise NotImplementedError


def _from_process(name: str, command: str, result: ProcessResult, *, hint: str | None = None) -> GateResult:
    output = result.combined()
    truncated = output[-MAX_GATE_OUTPUT:]
    status = GateStatus.PASSED if result.ok else GateStatus.FAILED
    summary = _summarise(name, result)
    return GateResult(
        name=name,
        status=status,
        command=command,
        exit_code=result.exit_code,
        duration_s=result.duration_s,
        summary=summary,
        output=truncated,
        fix_hint=hint,
    )


#: Lines that actually mean something in a passing run (pytest/vitest/ruff/tsc...).
_MEANINGFUL_OK = re.compile(
    r"(\d+\s+passed|\d+\s+files?\s+(?:already\s+)?formatted|All checks passed|Success|OK\b|"
    r"tests?\s+passed|no issues|clean)",
    re.IGNORECASE,
)


def _summarise(name: str, result: ProcessResult) -> str:
    """Pick the one line a human would read out of a tool's output."""

    if result.timed_out:
        return f"{name} timed out"
    if result.ok:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        meaningful = next((line for line in reversed(lines) if _MEANINGFUL_OK.search(line)), "")
        if meaningful:
            return meaningful[:160]
        first = lines[0] if lines else ""
        return first[:160] if first else f"{name} passed"
    text = result.combined()
    failures = [line.strip() for line in text.splitlines() if re.search(r"(FAILED|ERROR|error|✕|✗|×)", line)]
    return (failures[0] if failures else (text.splitlines()[-1] if text.splitlines() else f"{name} failed"))[:200]


class SyntaxGate(Gate):
    name = "syntax"
    description = "Parse Python/JSON/YAML files for syntax errors before running anything slower."

    def applicable(self, ctx: GateContext) -> tuple[bool, str]:
        return True, ""

    async def run(self, ctx: GateContext) -> GateResult:
        started = time.perf_counter()
        problems: list[str] = []
        checked = 0
        for path in _iter_files(ctx.project_root, ctx.changed_files):
            suffix = path.suffix.lower()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if suffix == ".py":
                checked += 1
                try:
                    ast.parse(text, filename=str(path))
                except SyntaxError as exc:
                    problems.append(f"{path.relative_to(ctx.project_root)}:{exc.lineno}: {exc.msg}")
            elif suffix == ".json":
                checked += 1
                try:
                    json.loads(text)
                except json.JSONDecodeError as exc:
                    problems.append(f"{path.relative_to(ctx.project_root)}: invalid JSON ({exc.msg})")
        duration = time.perf_counter() - started
        if problems:
            return GateResult(
                name=self.name,
                status=GateStatus.FAILED,
                duration_s=duration,
                summary=f"{len(problems)} syntax problem(s)",
                output="\n".join(problems[:40]),
                fix_hint="Fix the syntax error at the reported file:line before running slower gates.",
            )
        return GateResult(name=self.name, status=GateStatus.PASSED, duration_s=duration, summary=f"{checked} file(s) parsed")


class CommandGate(Gate):
    """Gate backed by a project command (lint, format, typecheck, tests, build).

    Resolution order:

    1. the project's own declared command (``[project.commands]``) - always preferred;
    2. an *environment-appropriate* fallback: a fallback only applies when its
       language or marker files match the project, so a Python repository never
       runs ``npx tsc`` and a JS repository never runs ``pytest``.

    That ordering is what keeps reports trustworthy: a gate either runs the
    project's real command or it reports itself as skipped, with a reason.
    """

    def __init__(
        self,
        name: str,
        *,
        description: str,
        commands: tuple[str, ...],
        fallbacks: tuple[dict[str, Any], ...] = (),
        hint: str | None = None,
        timeout_s: float = 900.0,
    ) -> None:
        self.name = name
        self.description = description
        self.commands = commands
        self.fallbacks = fallbacks
        self.hint = hint
        self.timeout_s = timeout_s

    def _fallback_matches(self, ctx: GateContext, fallback: dict[str, Any]) -> bool:
        from shutil import which

        command = str(fallback.get("command", ""))
        if not command or which(command.split()[0]) is None:
            return False
        language = fallback.get("language")
        if language:
            languages = {item.lower() for item in getattr(ctx.project, "languages", []) or []}
            if language not in languages:
                return False
        files = fallback.get("files") or ()
        if files and not any((ctx.project_root / name).exists() for name in files):
            return False
        return True

    def resolve_command(self, ctx: GateContext) -> str | None:
        command = ctx.command(*self.commands)
        if command:
            return normalize_package_manager_command(command, ctx.project.package_manager if ctx.project else None)
        for fallback in self.fallbacks:
            if self._fallback_matches(ctx, fallback):
                return str(fallback["command"])
        return None

    def applicable(self, ctx: GateContext) -> tuple[bool, str]:
        command = self.resolve_command(ctx)
        if not command:
            return False, (
                f"no {self.name} command detected (declare it under "
                r"\[project.commands] in .unknown/config.toml to enable this gate)"
            )
        return True, command

    async def run(self, ctx: GateContext) -> GateResult:
        command = self.resolve_command(ctx)
        if not command:
            return GateResult(name=self.name, status=GateStatus.SKIPPED, summary=f"no {self.name} command detected")
        result = await run_process(["/bin/sh", "-c", command] if _posix() else ["cmd", "/c", command], cwd=ctx.project_root, timeout=self.timeout_s, label=command)
        gate = _from_process(self.name, command, result, hint=self.hint)
        gate.fix_hint = gate.fix_hint or _failure_hint(self.name, gate.output)
        return gate


class DiffGate(Gate):
    name = "diff"
    description = "Check the working tree for obvious defects: conflict markers, huge accidental rewrites, leftover debug code."

    async def run(self, ctx: GateContext) -> GateResult:
        started = time.perf_counter()
        problems: list[str] = []
        markers = re.compile(r"^(<{7}|={7}|>{7})", re.MULTILINE)
        debug_markers = re.compile(r"\b(pdb\.set_trace|breakpoint\(\)|console\.log\(['\"]debug|TODO: implement|FIXME: implement|raise NotImplementedError)\b")
        for path in _iter_files(ctx.project_root, ctx.changed_files):
            if path.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".php", ".java", ".md", ".json", ".yaml", ".yml"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = path.relative_to(ctx.project_root)
            if markers.search(text):
                problems.append(f"{relative}: conflict markers present")
            for match in debug_markers.finditer(text):
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{relative}:{line}: leftover '{match.group(0)[:40]}'")
        duration = time.perf_counter() - started
        if problems:
            return GateResult(
                name=self.name,
                status=GateStatus.FAILED,
                duration_s=duration,
                summary=f"{len(problems)} suspicious pattern(s)",
                output="\n".join(problems[:40]),
                fix_hint="Resolve conflict markers and remove debug leftovers before shipping.",
            )
        return GateResult(name=self.name, status=GateStatus.PASSED, duration_s=duration, summary="no defects detected in changed files")


class BrowserGate(Gate):
    name = "browser"
    description = "Load the running app in Chromium, assert it renders, and capture console/network errors."

    def applicable(self, ctx: GateContext) -> tuple[bool, str]:
        if not ctx.settings.browser.enabled:
            return False, "browser validation disabled in config"
        if ctx.browser is None:
            return False, "playwright is not installed (pip install 'unknown-agent[browser]' && ua doctor --install-browsers)"
        url = ctx.base_url or ctx.settings.browser.base_url or (getattr(ctx.project, "dev_url", None))
        if not url:
            return False, "no app URL configured (set [browser] base_url or [project] dev_url, or pass --url)"
        return True, url

    async def run(self, ctx: GateContext) -> GateResult:
        applicable, reason = self.applicable(ctx)
        if not applicable:
            return GateResult(name=self.name, status=GateStatus.SKIPPED, summary=reason)
        url = ctx.base_url or ctx.settings.browser.base_url or getattr(ctx.project, "dev_url", None)
        started = time.perf_counter()
        try:
            session = await ctx.browser.session(base_url=url, start_app=bool(ctx.settings.browser.start_command))
            steps: list[dict[str, Any]] = [{"action": "goto", "url": url}]
            for route in ctx.settings.validation.browser_routes:
                steps.append({"action": "goto", "url": route})
                steps.append({"action": "assert_no_console_errors"})
            flow_file = ctx.settings.browser.flow_file
            if flow_file:
                # A project flow is the difference between "the page loads" and
                # "the feature works": its steps are appended to the render check.
                from unknown_agent.browser.flows import load_flow

                flow_path = Path(flow_file)
                if not flow_path.is_absolute():
                    flow_path = Path(ctx.project_root) / flow_path
                try:
                    steps.extend(load_flow(flow_path).steps)
                except Exception as exc:  # noqa: BLE001 - a broken flow file is a finding, not a crash
                    return GateResult(
                        name=self.name,
                        status=GateStatus.ERROR,
                        duration_s=time.perf_counter() - started,
                        summary=f"browser flow {flow_path} could not be loaded: {exc}",
                        fix_hint="Fix the flow file or unset [browser] flow_file.",
                    )
            report = await session.run_flow(url=url or "", steps=steps)
        except Exception as exc:  # noqa: BLE001 - browser availability issues are environment problems
            return GateResult(
                name=self.name,
                status=GateStatus.ERROR,
                duration_s=time.perf_counter() - started,
                summary=f"browser validation could not run: {type(exc).__name__}: {exc}",
                output=str(exc),
                fix_hint="Run `ua doctor --install-browsers` and check that the app is reachable, or disable the browser gate.",
            )
        duration = time.perf_counter() - started
        problems = [*report.failures, *[f"console error: {item}" for item in report.console_errors], *[f"failed request: {item}" for item in report.failed_requests]]
        status = GateStatus.PASSED if report.passed and not problems else GateStatus.FAILED
        return GateResult(
            name=self.name,
            status=status,
            duration_s=duration,
            summary=(
                f"{len(report.steps)} browser step(s), {len(report.console_errors)} console error(s), "
                f"{len(report.failed_requests)} failed request(s), {len(report.artifacts)} screenshot(s)"
            ),
            output="\n".join(problems[:40]) or report.render(),
            artifacts=list(report.artifacts),
            fix_hint="Open the screenshot artifact, or re-run `ua browser open <url>` to inspect interactively.",
        )


class CustomGate(Gate):
    """User-declared gate: ``[validation.custom_gate_commands]``."""

    def __init__(self, name: str, command: str, *, timeout_s: float = 600.0, hint: str | None = None) -> None:
        self.name = name
        self.command_text = command
        self.timeout_s = timeout_s
        self.hint = hint

    def applicable(self, ctx: GateContext) -> tuple[bool, str]:
        return True, self.command_text

    async def run(self, ctx: GateContext) -> GateResult:
        command = self.command_text
        if ctx.project is not None:
            command = command.replace("{root}", str(ctx.project_root)).replace("{python}", "python3")
        result = await run_process(["/bin/sh", "-c", command] if _posix() else ["cmd", "/c", command], cwd=ctx.project_root, timeout=self.timeout_s, label=command)
        return _from_process(self.name, command, result, hint=self.hint)


def default_gates(settings: Settings) -> list[Gate]:
    """Gate set for a project, ordered cheapest-to-most-expensive.

    Fallbacks are language- and marker-gated so a gate never invents a command
    that does not belong to the project.
    """

    pytest_fallback = {"command": "pytest -q", "language": "python"}
    gates: list[Gate] = [SyntaxGate()]
    detected: list[Gate] = [
        CommandGate(
            "lint",
            description="Static analysis / lint",
            commands=("lint",),
            fallbacks=(
                {"command": "ruff check .", "language": "python", "files": ("ruff.toml", ".ruff.toml", "pyproject.toml")},
                {"command": "npx --no-install eslint .", "files": ("eslint.config.js", "eslint.config.mjs", ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json")},
            ),
        ),
        CommandGate(
            "format",
            description="Formatting check",
            commands=("format_check", "fmt-check", "format"),
            fallbacks=({"command": "ruff format --check .", "language": "python", "files": ("ruff.toml", ".ruff.toml", "pyproject.toml")},),
        ),
        CommandGate(
            "typecheck",
            description="Type checking",
            commands=("typecheck", "types", "type-check"),
            fallbacks=(
                {"command": "npx --no-install tsc --noEmit", "files": ("tsconfig.json",)},
                {"command": "mypy .", "language": "python", "files": ("mypy.ini", ".mypy.ini")},
            ),
        ),
        CommandGate(
            "tests",
            description="Test suite",
            commands=("test", "tests"),
            fallbacks=(
                pytest_fallback,
                {"command": "npm test --silent", "files": ("package.json",)},
                {"command": "go test ./...", "language": "go"},
                {"command": "cargo test", "language": "rust"},
            ),
            hint="Fix the failing assertions; do not weaken or delete tests to make them pass.",
        ),
        CommandGate(
            "build",
            description="Build / compile",
            commands=("build", "compile"),
            fallbacks=(
                {"command": "go build ./...", "language": "go"},
                {"command": "cargo build", "language": "rust"},
            ),
            hint="Read the first compiler/bundler error; later errors are often its consequences.",
        ),
    ]
    for gate in detected:
        if settings.validation.gate(gate.name).enabled:
            gates.append(gate)
    from unknown_agent.validation.custom import build_custom_gates

    gates.extend(build_custom_gates(settings))
    if settings.validation.gate("browser").enabled:
        gates.append(BrowserGate())
    gates.append(DiffGate())
    return gates


def _iter_files(root: Path, changed: list[str] | None) -> list[Path]:
    if changed:
        candidates = []
        for item in changed:
            path = (root / item).resolve()
            if path.is_file():
                candidates.append(path)
        if candidates:
            return candidates
    from unknown_agent.context.repo_map import _iter_source_files

    return _iter_source_files(root, max_files=3000)


def _posix() -> bool:
    import os

    return os.name != "nt"


def _failure_hint(name: str, output: str) -> str | None:
    if name == "tests" and re.search(r"ImportError|ModuleNotFoundError", output):
        return "A dependency is missing: install it (or add it to the manifest) rather than editing the test."
    if name == "build" and re.search(r"cannot find (module|package)|not found", output, re.IGNORECASE):
        return "A module or package is missing; check the import path and the manifest."
    if name == "lint":
        return "Run the project's formatter or fix the reported violations."
    return None


__all__ = [
    "Gate",
    "GateContext",
    "SyntaxGate",
    "CommandGate",
    "DiffGate",
    "BrowserGate",
    "CustomGate",
    "default_gates",
]
