"""Onboarding UX: screens, the setup flow, and the promise that keys never leak.

The flow is exercised through the real CLI command with providers mocked at the
discovery boundary, so config persistence, credential storage and rendering are all
the production code paths.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from unknown_agent.cli.app import app
from unknown_agent.config.credentials import CredentialStore
from unknown_agent.config.loader import load_settings
from unknown_agent.errors import ConfigError, ExitCode
from unknown_agent.onboarding import screens
from unknown_agent.onboarding.flow import OnboardingOptions, run_onboarding
from unknown_agent.providers.catalog import CATALOG, resolve_entry
from unknown_agent.providers.discovery import ModelInfo, ValidationResult
from unknown_agent.ui.theme import UNKNOWN_AGENT_THEME

runner = CliRunner()

FAKE_KEY = "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"
FAKE_MODELS = [
    ModelInfo(id="meta-llama/llama-3.3-70b-instruct", name="Llama 3.3 70B", provider="groq", context_window=131072, supports_tools=True, input_cost_per_mtok=0.59, output_cost_per_mtok=0.79),
    ModelInfo(id="vision-only", name="Vision", provider="groq", context_window=8192, supports_tools=False, input_cost_per_mtok=None, output_cost_per_mtok=None),
]


# --------------------------------------------------------------------------- #
# screens
# --------------------------------------------------------------------------- #
def test_every_screen_renders_with_the_shipped_theme() -> None:
    """A missing style name must fail here, not in front of a first-time user."""

    buffer = io.StringIO()
    console = Console(file=buffer, theme=UNKNOWN_AGENT_THEME, width=100, force_terminal=False)
    entry = resolve_entry("openrouter")
    rendered = [
        screens.banner(),
        screens.step_header(1, "Choose a provider", "a subtitle"),
        screens.provider_table(CATALOG, highlight=("openrouter",)),
        screens.model_table(FAKE_MODELS, limit=1),
        screens.key_prompt_panel(entry, existing_hint="OPENROUTER_API_KEY is set"),
        screens.success_panel(entry.label, "openai/gpt-4o-mini", models=446, config_path="/tmp/config.toml", next_steps=("ua run \"do something\"",)),
        screens.failure_panel("the provider rejected that key", "HTTP 401", hint="Check the key", options=("retry", "cancel")),
        screens.info_panel("setup cancelled", "nothing was written"),
        screens.capability_line({"tools": True, "streaming": True, "model_listing": "live", "reasoning": False}),
    ]
    for item in rendered:
        console.print(item)
    assert "OpenRouter" in buffer.getvalue()


def test_screen_text_never_contains_a_key_placeholder_pattern() -> None:
    """Screens only ever show providers and models - no secret-shaped strings."""

    entry = resolve_entry("openai")
    buffer = io.StringIO()
    Console(file=buffer, width=120, theme=UNKNOWN_AGENT_THEME).print(screens.key_prompt_panel(entry, existing_hint=""))
    assert "sk-" not in buffer.getvalue()


# --------------------------------------------------------------------------- #
# flow
# --------------------------------------------------------------------------- #
@pytest.fixture
def mocked_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(entry, *, api_key=None, base_url=None, timeout=20.0):  # noqa: ANN001, ANN202
        return list(FAKE_MODELS)

    async def fake_validate(entry, *, api_key=None, base_url=None, timeout=20.0):  # noqa: ANN001, ANN202
        return ValidationResult(ok=True, message="key accepted", models_available=True, model_count=len(FAKE_MODELS))

    monkeypatch.setattr("unknown_agent.onboarding.flow.fetch_models", fake_fetch)
    monkeypatch.setattr("unknown_agent.onboarding.flow.validate_credential", fake_validate)


def test_setup_flow_persists_config_and_hides_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocked_provider: None, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "setup", "--provider", "groq", "--api-key", FAKE_KEY])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["provider_type"] == "groq"
    assert payload["model"] == "meta-llama/llama-3.3-70b-instruct"  # ranked: tool-capable first
    assert payload["credential_stored"] is True

    settings = load_settings(cwd=tmp_path).settings
    profile = settings.providers[payload["provider"]]
    assert profile.type == "groq"
    assert profile.base_url == "https://api.groq.com/openai/v1"
    assert profile.api_key == f"store:{payload['provider']}", "the config must hold a reference, never the secret"

    store = CredentialStore()
    assert store.get(payload["provider"]) == FAKE_KEY
    assert FAKE_KEY not in Path(payload["config_path"]).read_text()
    assert FAKE_KEY not in result.stdout and FAKE_KEY not in result.stderr


def test_setup_flow_reports_bad_credentials_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    async def rejecting(entry, *, api_key=None, base_url=None, timeout=20.0):  # noqa: ANN001, ANN202
        return ValidationResult(ok=False, message="the provider rejected this key (HTTP 401)", hint="Paste it again or create a new key", status_code=401)

    monkeypatch.setattr("unknown_agent.onboarding.flow.validate_credential", rejecting)
    result = runner.invoke(app, ["setup", "--provider", "openai", "--api-key", FAKE_KEY])

    assert result.exit_code != 0
    assert not (tmp_path / ".unknown").exists()
    assert not load_settings(cwd=tmp_path).paths.user_config_file.exists()


def test_interactive_failure_offers_recovery_without_crashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation fails and the (simulated) user cancels: clean exit, clear message."""

    monkeypatch.chdir(tmp_path)

    async def rejecting(entry, *, api_key=None, base_url=None, timeout=20.0):  # noqa: ANN001, ANN202
        return ValidationResult(ok=False, message="network unreachable", hint="Check your connection or pick another provider", status_code=None)

    monkeypatch.setattr("unknown_agent.onboarding.flow.validate_credential", rejecting)

    # `output.interactive` is derived from `sys.stdout.isatty()`; a test runner is not a
    # terminal, so the interactive branch is enabled explicitly for this test.
    from unknown_agent.ui.console import OutputManager, output

    monkeypatch.setattr(OutputManager, "interactive", property(lambda self: True))

    class ScriptedPrompter:
        interactive = True
        non_interactive = False

        def text(self, message: str, *, secret: bool = False, default: str | None = None, allow_empty: bool = False) -> str:
            if "API key" in message:
                return FAKE_KEY
            if "retry?" in message:
                return "a"  # abort
            return default or ""

        def confirm(self, message: str, *, default: bool = True) -> bool:
            return default

    class FakeRuntime:
        def __init__(self, settings) -> None:  # noqa: ANN001
            self._settings = settings

        def load(self, **_: object) -> object:
            return type("Loaded", (), {"settings": self._settings})()

        def save_settings(self, settings) -> Path:  # noqa: ANN001
            raise AssertionError("nothing should be written when onboarding is abandoned")

    settings = load_settings(cwd=tmp_path).settings
    outcome = asyncio.run(
        run_onboarding(
            FakeRuntime(settings),
            options=OnboardingOptions(provider="openai", store=CredentialStore()),
            prompter=ScriptedPrompter(),
        )
    )
    assert outcome is None


def test_cancel_paths_return_tuples_not_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an empty key prompt used to crash the flow with a TypeError."""

    from unknown_agent.onboarding.flow import _collect_credential

    entry = resolve_entry("openai")

    class EmptyPrompter:
        interactive = True
        non_interactive = False

        def text(self, message: str, *, secret: bool = False, default: str | None = None, allow_empty: bool = False) -> str:
            return ""

        def confirm(self, message: str, *, default: bool = True) -> bool:
            return False

    collected = asyncio.run(
        _collect_credential(entry, OnboardingOptions(), EmptyPrompter(), CredentialStore(tmp_path / "credentials.toml"), interactive=True)
    )
    assert collected == (None, False)


def test_flag_based_setup_needs_no_prompts_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocked_provider: None) -> None:
    """CI path: provider + key from the environment, model chosen automatically."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    result = runner.invoke(app, ["--json", "setup", "--provider", "groq", "--api-key-env", "GROQ_API_KEY"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    settings = load_settings(cwd=tmp_path).settings
    assert settings.providers[payload["provider"]].api_key == "env:GROQ_API_KEY"


def test_setup_short_circuits_when_already_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["setup", "--provider", "offline", "--as", "offline", "--skip-check"])
    again = runner.invoke(app, ["--json", "setup"])
    assert again.exit_code == 0
    assert json.loads(again.stdout)["status"] == "already_configured"


def test_rerun_with_force_reconfigures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocked_provider: None) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "setup", "--provider", "groq", "--api-key", FAKE_KEY, "--force"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["provider_type"] == "groq"


def test_wrapper_sync_helper_matches_async_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocked_provider: None) -> None:
    from unknown_agent.onboarding.flow import run_onboarding_sync

    monkeypatch.chdir(tmp_path)
    result = run_onboarding_sync(None, options=OnboardingOptions(provider="groq", api_key=FAKE_KEY))
    assert result is not None and result.provider_type == "groq"
    assert result.config_path


def test_endpoint_requiring_providers_are_asked_or_told(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Azure has no usable default endpoint: scripted runs must be told, not guessed at."""

    monkeypatch.chdir(tmp_path)
    without = runner.invoke(app, ["setup", "--provider", "azure-openai", "--api-key", FAKE_KEY, "--model", "my-deployment"])
    assert without.exit_code != 0
    exc = without.exception
    assert "base-url" in f"{exc} {getattr(exc, 'hint', '')}".lower()

    with_endpoint = runner.invoke(
        app,
        ["--json", "setup", "--provider", "azure-openai", "--api-key", FAKE_KEY, "--model", "my-deployment", "--base-url", "https://demo.openai.azure.com/openai/v1", "--skip-check"],
    )
    assert with_endpoint.exit_code == 0, with_endpoint.output
    settings = load_settings(cwd=tmp_path).settings
    profile = settings.providers[json.loads(with_endpoint.stdout)["provider"]]
    assert profile.base_url == "https://demo.openai.azure.com/openai/v1"
    assert profile.model == "my-deployment"


def test_provider_that_cannot_list_models_needs_an_explicit_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["setup", "--provider", "azure-openai", "--api-key", FAKE_KEY, "--base-url", "https://demo.openai.azure.com/openai/v1", "--skip-check"],
    )
    assert result.exit_code != 0
    assert "pass --model" in f"{result.exception} {getattr(result.exception, 'hint', '')}"
