from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from unknown_agent.config.models import Settings
from unknown_agent.host.process import run_process
from unknown_agent.sandbox import SandboxError, SandboxRequest, build_sandbox_command


def lookup(mapping: dict[str, str]):
    return lambda name: mapping.get(name)


def request(tmp_path: Path, **changes: object) -> SandboxRequest:
    values: dict[str, object] = {
        "workspace": tmp_path,
        "cwd": tmp_path,
        "mode": "required",
        "profile": "workspace",
    }
    values.update(changes)
    return SandboxRequest.model_validate(values)


def test_linux_bubblewrap_profile_is_bounded(tmp_path: Path) -> None:
    decision = build_sandbox_command(
        ["python", "-V"],
        request(tmp_path),
        system="Linux",
        which=lookup({"bwrap": "/usr/bin/bwrap"}),
    )
    assert decision.enforced
    assert decision.backend == "bubblewrap"
    assert "--unshare-net" in decision.argv
    bind = decision.argv.index("--bind")
    assert decision.argv[bind : bind + 3] == ["--bind", str(tmp_path), str(tmp_path)]
    assert decision.argv[-3:] == ["--", "python", "-V"]


def test_network_profile_and_isolated_override(tmp_path: Path) -> None:
    workspace = build_sandbox_command(
        ["cmd"],
        request(tmp_path, allow_network=True),
        system="Linux",
        which=lookup({"bwrap": "/bwrap"}),
    )
    isolated = build_sandbox_command(
        ["cmd"],
        request(tmp_path, allow_network=True, profile="isolated"),
        system="Linux",
        which=lookup({"bwrap": "/bwrap"}),
    )
    assert "--unshare-net" not in workspace.argv
    assert "--unshare-net" in isolated.argv
    assert not isolated.network_allowed


def test_required_fails_closed_and_auto_reports_degradation(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="required sandbox unavailable"):
        build_sandbox_command(["cmd"], request(tmp_path), system="Linux", which=lookup({}))
    decision = build_sandbox_command(
        ["cmd"],
        request(tmp_path, mode="auto"),
        system="Linux",
        which=lookup({}),
    )
    assert not decision.enforced
    assert "unavailable" in decision.detail


def test_macos_profile_allows_only_workspace_writes(tmp_path: Path) -> None:
    decision = build_sandbox_command(
        ["python"],
        request(tmp_path, allow_network=False),
        system="Darwin",
        which=lookup({"sandbox-exec": "/usr/bin/sandbox-exec"}),
    )
    policy = decision.argv[2]
    assert "deny default" in policy
    assert str(tmp_path) in policy
    assert "deny network" in policy


def test_windows_requires_native_restricted_token_helper(tmp_path: Path) -> None:
    decision = build_sandbox_command(
        ["cmd.exe"],
        request(tmp_path),
        system="Windows",
        which=lookup({"unknown-agent-sandbox.exe": "C:/ua/sandbox.exe"}),
    )
    assert decision.backend == "restricted-token"
    assert decision.enforced


def test_paths_outside_workspace_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="inside the workspace"):
        request(tmp_path, cwd=tmp_path.parent)
    with pytest.raises(ValidationError, match="writable paths"):
        request(tmp_path, writable_paths=[tmp_path.parent])


def test_timeout_is_hard_capped_at_five_minutes(tmp_path: Path) -> None:
    result = asyncio.run(
        run_process(
            ["python", "-c", "print('ok')"],
            cwd=tmp_path,
            timeout=9999,
        )
    )
    assert result.ok
    assert result.stdout.strip() == "ok"
    with pytest.raises(ValidationError, match="at most 300"):
        Settings.model_validate({"tools": {"timeouts": {"shell_run": 301}}})
