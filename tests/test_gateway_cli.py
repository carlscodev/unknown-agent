from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from unknown_agent.cli.app import app

runner = CliRunner()


def invoke(project: Path, *args: str) -> dict[str, object]:
    result = runner.invoke(app, ["--project", str(project), *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_gateway_cli_receive_and_list_are_machine_safe(tmp_path: Path) -> None:
    pairing = invoke(tmp_path, "gateway", "pair-issue", "--channel", "test", "--account", "bot")
    identity = invoke(
        tmp_path,
        "gateway",
        "pair-claim",
        "--channel",
        "test",
        "--account",
        "bot",
        "--sender",
        "user",
        "--code",
        str(pairing["code"]),
    )
    assert identity["role"] == "operator"
    accepted = invoke(
        tmp_path,
        "gateway",
        "receive",
        "--channel",
        "test",
        "--account",
        "bot",
        "--conversation",
        "room",
        "--sender",
        "user",
        "--external-id",
        "delivery-1",
        "--text",
        "hello",
    )
    assert accepted["accepted"] is True
    listing = invoke(tmp_path, "gateway", "list")
    assert listing["records"][0]["id"] == accepted["id"]  # type: ignore[index]


def test_gateway_help_exposes_worker_and_drain(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project", str(tmp_path), "gateway", "--help"])
    assert result.exit_code == 0
    assert "receive" in result.output
    assert "drain" in result.output
    assert "worker" in result.output
