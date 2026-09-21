"""Quality gates: detection, exit semantics, evidence and the diff gate."""

from __future__ import annotations

import asyncio
from pathlib import Path

from unknown_agent.config.loader import load_settings
from unknown_agent.context.project import load_project_context
from unknown_agent.observability.events import EventBus
from unknown_agent.validation.engine import ValidationEngine
from unknown_agent.validation.gates import default_gates


def build_engine(project: Path, **kwargs: object) -> ValidationEngine:
    loaded = load_settings(cwd=project)
    context = load_project_context(project, loaded.settings)
    return ValidationEngine(loaded.settings, project_root=project, project=context, events=EventBus(), **kwargs)  # type: ignore[arg-type]


def test_default_gate_order_is_cheapest_first() -> None:
    from unknown_agent.config.models import Settings

    names = [gate.name for gate in default_gates(Settings())]
    assert names[0] == "syntax"
    assert names[-1] == "diff"
    assert "tests" in names and "build" in names


def test_python_project_never_runs_javascript_gates(project: Path) -> None:
    engine = build_engine(project)
    report = asyncio.run(engine.run(required_only=False))
    assert report.passed
    lint = next(item for item in report.results if item.name == "lint")
    if lint.command:
        assert "eslint" not in lint.command
        assert "tsc" not in lint.command
    typecheck = next(item for item in report.results if item.name == "typecheck")
    assert typecheck.passed or typecheck.status.value in {"skipped", "failed"}


def test_failing_tests_gate_fails_and_keeps_evidence(failing_project: Path) -> None:
    engine = build_engine(failing_project)
    report = asyncio.run(engine.run(gates=["tests"], required_only=True))
    tests = next(item for item in report.results if item.name == "tests")
    assert not tests.passed
    assert tests.status.value == "failed"
    assert "assert" in (tests.output or "").lower()
    assert tests.fix_hint
    assert not report.passed
    assert report.blocking_failures


def test_syntax_gate_reports_file_and_line(project: Path) -> None:
    (project / "src" / "demo" / "broken.py").write_text("def oops(:\n    pass\n", encoding="utf-8")
    engine = build_engine(project)
    report = asyncio.run(engine.run(gates=["syntax"], required_only=True))
    syntax = next(item for item in report.results if item.name == "syntax")
    assert not syntax.passed
    assert "broken.py" in syntax.output


def test_report_serialises_to_json(project: Path) -> None:
    engine = build_engine(project)
    report = asyncio.run(engine.run(gates=["syntax"], required_only=True))
    payload = report.to_dict()
    assert payload["passed"] is True
    assert payload["results"][0]["name"] == "syntax"


def test_browser_gate_skips_without_playwright(project: Path) -> None:
    engine = build_engine(project, browser=None)
    report = asyncio.run(engine.run(gates=["browser"]))
    browser = next(item for item in report.results if item.name == "browser")
    assert browser.status.value == "skipped"
    assert "playwright" in browser.summary.lower() or "url" in browser.summary.lower()


def test_browser_gate_explains_a_missing_url(project: Path) -> None:
    from unknown_agent.browser.manager import BrowserSessionManager

    loaded = load_settings(cwd=project)
    manager = BrowserSessionManager(loaded.settings.browser, artifacts_dir=project / ".unknown" / "artifacts")
    engine = build_engine(project, browser=manager)
    report = asyncio.run(engine.run(gates=["browser"]))
    browser = next(item for item in report.results if item.name == "browser")
    assert browser.status.value in {"skipped", "error"}
    assert "url" in (browser.summary + (browser.fix_hint or "")).lower()


def test_custom_gate_command_from_config(project: Path) -> None:
    from unknown_agent.config.models import Settings

    settings = Settings()
    settings.validation.custom_gate_commands = {"docs": "python3 -c \"print('docs ok')\""}
    gates = {gate.name: gate for gate in default_gates(settings)}
    assert "docs" in gates
