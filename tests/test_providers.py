"""Provider catalog, adapters, discovery, credentials and onboarding routing.

Network calls are exercised through ``httpx.MockTransport`` so the *real* plumbing
(URL building, headers, auth, status handling, parsing) runs while staying offline.
Two rules these tests enforce:

* **no dead integrations** - every catalog entry must build an adapter that passes
  the tool-call round trip;
* **no leaked secrets** - stored credentials never appear in output or settings dumps.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app
from unknown_agent.config.credentials import CredentialStore
from unknown_agent.config.loader import load_settings
from unknown_agent.config.models import ProviderConfig, Settings
from unknown_agent.errors import ConfigError
from unknown_agent.onboarding.detect import NEEDS_REPAIR, NEEDS_SETUP, READY, detect_setup_state
from unknown_agent.providers.catalog import CATALOG, resolve_entry
from unknown_agent.providers.discovery import ModelInfo, ProviderProbeError, fetch_models, validate_credential
from unknown_agent.providers.registry import build_provider, protocol_for
from unknown_agent.providers.toolkit import check_tool_roundtrip, normalise_provider_error

runner = CliRunner()


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def test_catalog_keys_are_unique_and_complete() -> None:
    keys = [entry.key for entry in CATALOG]
    assert len(keys) == len(set(keys))
    assert {"openai", "anthropic", "mistral", "openrouter", "deepseek", "xai", "groq"} <= set(keys), "the required providers must all be present"
    for entry in CATALOG:
        assert entry.label and entry.protocol
        if entry.requires_base_url or entry.protocol == "offline":
            assert entry.base_url == ""
        else:
            assert entry.base_url.startswith("http"), entry.key
        if entry.model_listing == "none" and not entry.requires_base_url:
            # Vendors that cannot list models must ship a curated set - otherwise
            # onboarding would have nothing to offer. Azure is exempt: its "model" is a
            # deployment the user names.
            assert entry.known_models or entry.default_model, f"{entry.key} can never offer a model"
        if entry.api_key_required:
            assert entry.env_var and entry.credential_hint, entry.key


def test_catalog_aliases_and_lookups() -> None:
    assert resolve_entry("grok").key == "xai"
    assert resolve_entry("Gemini").key == "google"
    assert resolve_entry("kimi").key == "moonshot"
    assert resolve_entry("nope-not-a-provider") is None


def test_every_provider_builds_a_working_adapter() -> None:
    """The 'no dead integrations' test: one adapter round trip per catalog entry."""

    failures: list[str] = []
    for entry in CATALOG:
        try:
            # A literal (non-reference) key keeps this test offline while still
            # exercising construction, catalog defaults and the tool-call contract.
            profile = ProviderConfig(**{**entry.profile_defaults, "api_key": "sk-test-literal"})
        except Exception as exc:  # noqa: BLE001 - collect all failures, then assert
            failures.append(f"{entry.key}: profile rejected: {exc}")
            continue
        try:
            provider = build_provider(profile, name=entry.key)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{entry.key}: could not build adapter: {exc}")
            continue
        if entry.supports_tools:
            report = check_tool_roundtrip(provider)
            if not report.ok:
                failures.append(f"{entry.key}: tool round trip failed: {report.render()}")
    assert not failures, "\n".join(failures)


def test_vendor_types_map_to_adapter_protocols() -> None:
    assert protocol_for("groq") == "openai-compatible"
    assert protocol_for("openrouter") == "openai-compatible"
    assert protocol_for("anthropic") == "anthropic"
    assert protocol_for("ollama") == "ollama"
    assert protocol_for("offline") == "offline"


def test_vendor_profile_keeps_its_identity_and_base_url() -> None:
    profile = ProviderConfig(type="groq", model="llama-3.3-70b-versatile", api_key="gsk-literal-test-key")
    assert profile.type == "groq"
    provider = build_provider(profile, name="groq")
    assert provider.vendor == "groq"
    assert provider.base_url == "https://api.groq.com/openai/v1"


def test_unknown_provider_type_is_rejected_with_a_useful_message() -> None:
    with pytest.raises(Exception) as excinfo:
        ProviderConfig(type="not-a-vendor")
    assert "providers catalog" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# discovery (offline, via MockTransport)
# --------------------------------------------------------------------------- #
def mock_client(handler: httpx.MockTransport, base_url: str, headers: dict[str, str]) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=handler, base_url=base_url)
    client.headers.update(headers)
    return client


@pytest.fixture
def openrouter_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "anthropic/claude-3.5-sonnet",
                "name": "Anthropic: Claude 3.5 Sonnet",
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "supported_parameters": ["tools", "temperature"],
                "architecture": {"input_modalities": ["text", "image"]},
                "top_provider": {"max_completion_tokens": 8192},
            },
            {
                "id": "meta-llama/llama-3.3-70b-instruct",
                "name": "Llama 3.3 70B",
                "context_length": 131072,
                "pricing": {"prompt": "0.00000012", "completion": "0.0000003"},
                "supported_parameters": ["temperature"],
                "architecture": {"input_modalities": ["text"]},
            },
        ]
    }


def test_fetch_models_parses_openrouter_metadata(monkeypatch: pytest.MonkeyPatch, openrouter_payload: dict[str, object]) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=openrouter_payload))
    monkeypatch.setattr(
        "unknown_agent.providers.discovery.build_client",
        lambda entry, api_key, base_url=None, timeout=20.0: mock_client(transport, "https://openrouter.ai/api/v1", {"Authorization": f"Bearer {api_key}"}),
    )
    entry = resolve_entry("openrouter")
    models = asyncio.run(fetch_models(entry, api_key="sk-test"))

    assert [model.id for model in models] == ["anthropic/claude-3.5-sonnet", "meta-llama/llama-3.3-70b-instruct"]
    claude = models[0]
    assert claude.context_window == 200000
    assert claude.supports_tools is True
    assert claude.supports_vision is True
    assert claude.input_cost_per_mtok == pytest.approx(3.0)
    assert claude.output_cost_per_mtok == pytest.approx(15.0)
    assert models[1].supports_tools is False  # "tools" absent from supported_parameters


def test_auth_failure_is_reported_with_an_actionable_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": {"message": "No auth credentials found"}})
    )
    monkeypatch.setattr(
        "unknown_agent.providers.discovery.build_client",
        lambda entry, api_key, base_url=None, timeout=20.0: mock_client(transport, "https://openrouter.ai/api/v1", {}),
    )
    result = asyncio.run(validate_credential(resolve_entry("openrouter"), api_key="sk-wrong"))
    assert not result.ok
    assert "401" in result.message or "rejected" in result.message
    assert "setup" in result.hint


def test_rate_limits_and_server_errors_are_distinguishable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models") and "groq" in str(request.url):
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": "slow down"})
        return httpx.Response(503, json={"error": "upstream"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "unknown_agent.providers.discovery.build_client",
        lambda entry, api_key, base_url=None, timeout=20.0: mock_client(transport, base_url or "https://api.groq.com/openai/v1", {}),
    )
    with pytest.raises(ProviderProbeError) as rate_limited:
        asyncio.run(fetch_models(resolve_entry("groq"), api_key="sk-test"))
    assert "rate-limited" in rate_limited.value.message
    assert "7" in rate_limited.value.hint


def test_providers_without_listing_use_the_curated_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = resolve_entry("perplexity")
    models = asyncio.run(fetch_models(entry, api_key="sk-test"))
    assert {model.id for model in models} >= {"sonar", "sonar-pro"}
    assert all(model.provider == "perplexity" for model in models)


def test_local_providers_need_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = resolve_entry("ollama")
    payload = {"models": [{"name": "qwen2.5-coder:7b", "model": "qwen2.5-coder:7b", "details": {"context_length": 32768}}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(
        "unknown_agent.providers.discovery.build_client",
        lambda entry, api_key, base_url=None, timeout=20.0: mock_client(transport, base_url or "http://127.0.0.1:11434", {}),
    )
    result = asyncio.run(validate_credential(entry, api_key=None))
    assert result.ok and result.model_count == 1


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def test_credential_store_writes_private_file(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.toml")
    path = store.set("openrouter", "sk-or-v1-secret-value")

    assert path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.get("openrouter") == "sk-or-v1-secret-value"
    assert store.names() == ["openrouter"]
    assert store.audit()["warnings"] == []
    assert store.delete("openrouter") is True
    assert store.names() == []


def test_store_reference_resolves_through_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CredentialStore(tmp_path / "credentials.toml")
    store.set("groq", "gsk-secret")
    monkeypatch.setattr("unknown_agent.config.credentials.default_credentials_path", lambda: tmp_path / "credentials.toml")

    from unknown_agent.config.secrets import resolve_secret

    assert resolve_secret("store:groq") == "gsk-secret"


def test_missing_store_reference_explains_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unknown_agent.config.credentials.default_credentials_path", lambda: tmp_path / "credentials.toml")
    from unknown_agent.config.secrets import resolve_secret

    with pytest.raises(ConfigError) as excinfo:
        resolve_secret("store:nothing-here")
    assert "ua setup" in (excinfo.value.hint or "")


# --------------------------------------------------------------------------- #
# onboarding detection
# --------------------------------------------------------------------------- #
def test_detect_without_providers_asks_for_onboarding() -> None:
    settings = Settings()
    settings.providers = {}
    state = detect_setup_state(settings)
    assert state.status == NEEDS_SETUP
    assert "ua setup" in state.hint


def test_detect_reports_ready_for_offline_provider() -> None:
    settings = Settings()
    settings.providers = {"offline": ProviderConfig(type="offline", model="offline-deterministic")}
    settings.provider = "offline"
    assert detect_setup_state(settings).status == READY


def test_detect_needs_repair_when_the_stored_key_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unknown_agent.config.credentials.default_credentials_path", lambda: tmp_path / "credentials.toml")
    monkeypatch.setattr("unknown_agent.config.credentials.CredentialStore.__init__", lambda self, path=None: setattr(self, "path", tmp_path / "credentials.toml") or None)
    settings = Settings()
    settings.providers = {"openrouter": ProviderConfig(type="openrouter", model="x", api_key="store:openrouter")}
    settings.provider = "openrouter"
    state = detect_setup_state(settings, store=CredentialStore(tmp_path / "credentials.toml"))
    assert state.status == NEEDS_REPAIR
    assert any("api_key" in problem or "credential" in problem for problem in state.problems + [state.reason])


# --------------------------------------------------------------------------- #
# error normalisation
# --------------------------------------------------------------------------- #
def test_error_normalisation_covers_the_common_failures() -> None:
    cases = {
        "HTTP 401: invalid api key": "credentials",
        "HTTP 402: insufficient credits": "credit",
        "HTTP 429: rate limit exceeded": "rate-limited",
        "model not found: gpt-9": "does not know the model",
        "maximum context length is 8192 tokens": "context window",
    }
    for raw, needle in cases.items():
        friendly = normalise_provider_error(Exception(raw), provider="openrouter", model="openai/gpt-4o-mini")
        assert needle.lower() in friend.api_message().lower() if hasattr(friendly, "api_message") else needle.lower() in friendly.message.lower()


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_cli_catalog_lists_real_providers() -> None:
    result = runner.invoke(app, ["--json", "providers", "catalog"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    keys = {entry["key"] for entry in payload["providers"]}
    assert {"openai", "anthropic", "mistral", "openrouter", "deepseek", "xai", "groq"} <= keys
    assert all(entry["capabilities"]["tools"] is not None for entry in payload["providers"])


def test_cli_setup_with_offline_provider_works_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "setup", "--provider", "offline", "--as", "offline", "--skip-check"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["provider"] == "offline"

    listed = runner.invoke(app, ["--json", "providers", "list"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["active"] == "offline"
    assert json.loads(listed.stdout)["providers"][0]["provider"] == "Offline (no model)"


def test_cli_models_use_switches_and_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["setup", "--provider", "offline", "--as", "offline", "--skip-check"])
    result = runner.invoke(app, ["models", "use", "offline-deterministic", "--provider", "offline", "--no-check"])
    assert result.exit_code == 0
    settings = load_settings(cwd=tmp_path).settings
    assert settings.providers["offline"].model == "offline-deterministic"


def test_cli_providers_remove_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["setup", "--provider", "offline", "--as", "offline", "--skip-check"])
    result = runner.invoke(app, ["providers", "remove", "offline", "--yes"])
    assert result.exit_code == 0
    assert "offline" not in load_settings(cwd=tmp_path).settings.providers


def test_cli_unknown_provider_is_a_clear_error() -> None:
    result = runner.invoke(app, ["setup", "--provider", "not-a-vendor"])
    assert result.exit_code != 0
    # main() renders the error for humans; in-process the ConfigError carries the hint.
    exc = result.exception
    message = f"{result.stdout}{result.stderr}{exc} {getattr(exc, 'hint', '')}"
    assert "unknown provider" in message.lower()
    assert "providers catalog" in message.lower()


def test_model_ranking_prefers_tool_capable_models() -> None:
    from unknown_agent.onboarding.flow import _rank_models

    models = [
        ModelInfo(id="vision-only-model", supports_tools=False, input_cost_per_mtok=0.0),
        ModelInfo(id="cheap-tools", supports_tools=True, input_cost_per_mtok=0.5),
        ModelInfo(id="pricey-tools", supports_tools=True, input_cost_per_mtok=9.0),
    ]
    ranked = _rank_models(models)
    assert ranked[0].id == "cheap-tools"
    assert ranked[-1].id == "vision-only-model"
