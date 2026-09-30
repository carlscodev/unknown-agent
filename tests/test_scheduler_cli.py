from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from unknown_agent.cli.app import app

runner = CliRunner()


def invoke(project: Path, *arguments: str) -> dict[str, object]:
    result = runner.invoke(app, ["--project", str(project), *arguments])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_scheduler_cli_lifecycle_is_json_safe(tmp_path: Path) -> None:
    created = invoke(
        tmp_path,
        "scheduler",
        "create",
        "--name",
        "nightly review",
        "--trigger",
        "once",
        "--run-at",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "--payload",
        '{"task":"review this repository","read_only":true}',
    )
    job_id = str(created["id"])
    assert created["action"] == "agent.run"

    listing = invoke(tmp_path, "scheduler", "list")
    assert listing["jobs"][0]["id"] == job_id  # type: ignore[index]

    paused = invoke(tmp_path, "scheduler", "pause", job_id)
    assert paused["enabled"] is False
    resumed = invoke(tmp_path, "scheduler", "resume", job_id)
    assert resumed["enabled"] is True

    execution = invoke(
        tmp_path,
        "scheduler",
        "trigger",
        job_id,
        "--idempotency-key",
        "cli-delivery-1",
    )
    inspected = invoke(tmp_path, "scheduler", "inspect", job_id)
    assert inspected["executions"][0]["id"] == execution["id"]  # type: ignore[index]

    cancelled = invoke(tmp_path, "scheduler", "cancel", str(execution["id"]))
    assert cancelled["status"] == "cancelled"
    replayed = invoke(tmp_path, "scheduler", "replay", str(execution["id"]))
    assert replayed["id"] != execution["id"]


def test_scheduler_help_exposes_operational_commands(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project", str(tmp_path), "scheduler", "--help"])
    assert result.exit_code == 0
    for command in ("create", "pause", "resume", "trigger", "cancel", "replay", "worker"):
        assert command in result.output
