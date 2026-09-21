"""Validation tools: run the quality gates from inside the agent loop."""

from __future__ import annotations

from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext


class RunGatesTool(Tool):
    name = "run_gates"
    description = (
        "Run the project's quality gates (syntax, lint, format, typecheck, tests, build, browser, diff) "
        "and return a pass/fail report with evidence. This is how the agent proves work is done: "
        "call it before claiming a task is complete."
    )
    category = "validation"
    risk = RiskLevel.EXECUTE
    tags = ("verify", "primary")
    timeout_s = 2400.0
    parameters = (
        ParameterSpec("gates", "array", "Only run these gates (default: all enabled).", default=None, items="string"),
        ParameterSpec("required_only", "boolean", "Skip advisory gates.", default=False),
        ParameterSpec("url", "string", "Base URL for browser validation.", default=""),
    )
    examples = ("ua tools call run_gates", 'ua tools call run_gates --arg gates=tests,lint')

    async def run(self, ctx: ToolContext, gates: list[str] | None = None, required_only: bool = False, url: str = "") -> ToolResult:
        from unknown_agent.validation.engine import ValidationEngine

        engine = ValidationEngine(ctx.settings, project_root=ctx.project_root, events=ctx.events, run_id=ctx.run_id, browser=ctx.browser, project=ctx.project)
        report = await engine.run(gates=gates, required_only=required_only, base_url=url or None)
        ok = report.passed
        lines = []
        for result in report.results:
            icon = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "error": "ERROR"}.get(result.status.value, "?")
            lines.append(f"[{icon}] {result.name}{' (required)' if result.required else ' (advisory)'} - {result.summary or 'no summary'} [{result.duration_s:.1f}s]")
        failing = report.failures
        output = "\n".join(lines)
        if failing:
            output += "\n\nFailure details:\n" + "\n\n".join(f"### {item.name}\n{item.output[-2500:]}" for item in failing[:3])
        return ToolResult(
            ok=ok,
            output=output,
            error=None if ok else f"{len(failing)} gate(s) failing: {', '.join(item.name for item in failing)}",
            data={"passed": ok, "report": report.to_dict()},
        )


class SyntaxCheckTool(Tool):
    name = "syntax_check"
    description = "Fast syntax-only validation for a file or the whole project (no tests, no imports executed)."
    category = "validation"
    risk = RiskLevel.READ
    tags = ("verify",)
    parameters = (ParameterSpec("path", "string", "File or directory to check.", default="."),)

    async def run(self, ctx: ToolContext, path: str = ".") -> ToolResult:
        import ast

        from unknown_agent.tools.builtin.fs_tools import iter_files, resolve, relative

        target = resolve(ctx, path)
        files = [target] if target.is_file() else iter_files(ctx, target)
        failures: list[str] = []
        checked = 0
        for file_path in files:
            if file_path.suffix == ".py":
                checked += 1
                try:
                    ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError as exc:
                    failures.append(f"{relative(ctx, file_path)}:{exc.lineno}: {exc.msg}")
            elif file_path.suffix in {".json"}:
                import json

                checked += 1
                try:
                    json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    failures.append(f"{relative(ctx, file_path)}: {exc}")
        ok = not failures
        output = f"checked {checked} file(s); {len(failures)} problem(s)"
        if failures:
            output += "\n" + "\n".join(failures[:50])
        return ToolResult(ok=ok, output=output, error=None if ok else f"{len(failures)} syntax problem(s)", data={"failures": failures, "checked": checked})


def build_tools() -> list[Tool]:
    return [RunGatesTool(), SyntaxCheckTool()]


__all__ = ["RunGatesTool", "SyntaxCheckTool", "build_tools"]
