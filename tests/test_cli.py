"""CLI surface: help, exit codes, machine output, safety rails.

The CLI is the product, so these tests exercise it the way a user does - through
Typer's runner, with the project as the working directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _cwd(cli_env: None) -> None:
    """Every test runs inside the generated project."""


def invoke(*args: str, expect: int | None = 0) -> str:
    result = runner.invoke(app, list(args))
    if expect is not None:
        assert result.exit_code == expect, f"`ua {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
    return result.output


def test_help_lists_the_grouped_surface() -> None:
    output = invoke("--help")
    for group in ("Agent", "Project", "Verify", "Inspect", "Browser", "Memory", "Config"):
        assert group.lower() in output.lower()
    assert "run" in output and "doctor" in output


def test_version_and_json_version() -> None:
    assert "unknown-agent" in invoke("--version")
    payload = json.loads(invoke("--json", "version"))
    assert payload["version"]
    assert payload["python"]


def test_init_creates_project_config(tmp_path: Path) -> None:
    invoke("init")
    config = Path(".unknown") / "config.toml"
    assert config.is_file()
    text = config.read_text()
    assert "[project]" in text
    assert "protected_paths" in text
    assert (Path(".unknown") / "guidance.md").is_file()


def test_doctor_reports_checks_as_json() -> None:
    payload = json.loads(invoke("--json", "doctor"))
    assert payload["checks"]
    assert all("status" in check for check in payload["checks"])


def test_inspect_json_describes_the_project() -> None:
    payload = json.loads(invoke("--json", "inspect"))
    assert payload["kind"]
    assert "python" in payload["languages"]
    assert payload["commands"].get("test")


def test_verify_exit_code_signals_failure(failing_project: Path, cli_env: None) -> None:
    output = invoke("verify", "--required-only", expect=7)
    assert "tests" in output


def test_verify_json_is_machine_readable() -> None:
    payload = json.loads(invoke("--json", "verify", "--required-only"))
    assert "results" in payload
    assert payload["passed"] is True


def test_unknown_command_is_a_usage_error() -> None:
    result = runner.invoke(app, ["definitely-not-a-command"])
    assert result.exit_code == 2
    assert "Usage" in result.output or "No such command" in result.output


def test_tools_list_and_call_are_wired() -> None:
    payload = json.loads(invoke("--json", "tools", "list"))
    assert payload["count"] > 20
    output = invoke("tools", "call", "read_file", "--arg", "path=src/demo/calc.py")
    assert "return left + right" in output


def test_read_only_blocks_mutating_tools() -> None:
    output = invoke("--read-only", "tools", "call", "write_file", "--arg", "path=x.py", "--arg", "content=1", expect=9)
    assert "read-only" in output.lower()
    assert "traceback" not in output.lower()


def test_dry_run_reports_instead_of_writing() -> None:
    invoke("--dry-run", "--yes", "tools", "call", "create_file", "--arg", "path=src/demo/dry.py", "--arg", "content=1")
    assert not (Path("src") / "demo" / "dry.py").exists()


def test_memory_cli_round_trip() -> None:
    invoke("memory", "remember", "the project uses pytest", "--kind", "convention")
    output = invoke("memory", "search", "pytest")
    assert "pytest" in output
    stats = json.loads(invoke("--json", "memory", "stats"))
    assert stats["facts"] >= 1


def test_config_show_and_get() -> None:
    assert "agent.max_steps" in invoke("config", "show", "--section", "agent")
    assert invoke("config", "get", "agent.max_steps").strip().isdigit()


def test_context_repo_map_and_procedures() -> None:
    assert "calc.py" in invoke("context", "repo-map")
    invoke("context", "procedures", "--task", "add a function")


def test_plan_is_read_only_and_shows_steps() -> None:
    output = invoke("--provider", "offline", "plan", "add pagination")
    assert "Implementation" in output or "Reconnaissance" in output


def test_runs_and_sessions_are_listed_after_a_run() -> None:
    invoke("--provider", "offline", "run", "describe the project layout")
    output = invoke("runs", "list")
    assert "completed" in output
    assert "sessions" in invoke("sessions", "list")
