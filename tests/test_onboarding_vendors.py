"""Vendor-specific onboarding rules: private endpoints and user-named models.

Azure OpenAI is the useful stress test for both: it has no reachable default endpoint
and its "model" is a deployment the account owner invented, so onboarding must ask
instead of guessing (or fail loudly when it cannot ask).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app
from unknown_agent.config.loader import load_settings

runner = CliRunner()
FAKE_KEY = "az8f7e6d5c4b3a29180f7e6d5c4b3a2918"


def test_private_endpoint_requires_the_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["setup", "--provider", "azure-openai", "--api-key", FAKE_KEY, "--model", "my-deployment"])
    assert result.exit_code != 0
    message = f"{result.exception} {getattr(result.exception, 'hint', '')}".lower()
    assert "base-url" in message


def test_private_endpoint_is_persisted_and_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    endpoint = "https://demo.openai.azure.com/openai/v1"
    result = runner.invoke(
        app,
        ["--json", "setup", "--provider", "azure-openai", "--api-key", FAKE_KEY, "--model", "my-deployment", "--base-url", endpoint, "--skip-check"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    profile = load_settings(cwd=tmp_path).settings.providers[payload["provider"]]
    assert profile.base_url == endpoint
    assert profile.model == "my-deployment"
    assert profile.type == "azure-openai"
    assert FAKE_KEY not in (Path(payload["config_path"]).read_text())


def test_vendor_without_model_listing_needs_an_explicit_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["setup", "--provider", "azure-openai", "--api-key", FAKE_KEY, "--base-url", "https://demo.openai.azure.com/openai/v1", "--skip-check"],
    )
    assert result.exit_code != 0
    assert "--model" in f"{result.exception} {getattr(result.exception, 'hint', '')}"


def test_local_provider_needs_no_key_and_no_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LM Studio needs neither a key nor an endpoint - but it does need a model name
    while the local server is unreachable (it cannot be listed), and the error has to
    say exactly that instead of silently inventing one."""

    monkeypatch.chdir(tmp_path)
    without_server = runner.invoke(app, ["setup", "--provider", "lmstudio", "--as", "local", "--skip-check"])
    assert without_server.exit_code != 0
    message = f"{without_server.exception} {getattr(without_server.exception, 'hint', '')}".lower()
    assert "local server" in message and "method" not in message.split("local server")[0][-30:]

    with_model = runner.invoke(
        app,
        ["--json", "setup", "--provider", "lmstudio", "--as", "local", "--model", "qwen2.5-coder-7b-instruct", "--skip-check"],
    )
    assert with_model.exit_code == 0, with_model.output
    payload = json.loads(with_model.stdout)
    assert payload["credential_stored"] is False
    profile = load_settings(cwd=tmp_path).settings.providers["local"]
    assert profile.api_key in (None, "")
    assert profile.base_url.startswith("http://")
    assert profile.model == "qwen2.5-coder-7b-instruct"
