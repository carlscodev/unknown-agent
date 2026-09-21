"""Providers declare auth modes; conflicts are detected; health is a report."""

from __future__ import annotations

import pytest

from unknown_agent.providers.auth import (
    auth_modes_for,
    detect_auth_conflicts,
    resolve_auth_mode,
)
from unknown_agent.providers.catalog import resolve_entry
from unknown_agent.providers.health import ProviderStatus, classify_provider_error, run_provider_check


# --------------------------------------------------------------------------- #
# declaration
# --------------------------------------------------------------------------- #
def test_codex_declares_subscription_and_api_key() -> None:
    """The motivating case: two products, two billing stories, both offered."""

    modes = auth_modes_for(resolve_entry("codex"))
    keys = [mode.key for mode in modes]
    assert "chatgpt" in keys and "api_key" in keys

    subscription = next(mode for mode in modes if mode.key == "chatgpt")
    api_key = next(mode for mode in modes if mode.key == "api_key")
    assert subscription.kind == "subscription"
    assert api_key.kind == "api_key"
    # The billing difference must be stated, not implied.
    assert "ChatGPT" in subscription.billing
    assert "per token" in api_key.billing
    assert subscription.billing != api_key.billing


def test_no_provider_is_assumed_key_only() -> None:
    """Every keyed provider still offers a custom endpoint, not just a key box."""

    for key in ("openai", "anthropic", "mistral", "groq"):
        kinds = {mode.kind for mode in auth_modes_for(resolve_entry(key))}
        assert "api_key" in kinds
        assert "endpoint" in kinds, f"{key} offers no custom-endpoint mode"


def test_keyless_provider_offers_no_credential_mode() -> None:
    modes = auth_modes_for(resolve_entry("ollama"))
    assert modes[0].kind == "none"
    assert modes[0].needs_secret is False


def test_resolve_auth_mode_prefers_recommended_then_named() -> None:
    entry = resolve_entry("codex")
    assert resolve_auth_mode(entry, None).key == "chatgpt"      # recommended
    assert resolve_auth_mode(entry, "api_key").key == "api_key"  # explicit wins
    assert resolve_auth_mode(entry, "nonsense").key == "chatgpt"  # unknown falls back


def test_catalog_entry_serialises_its_modes() -> None:
    payload = resolve_entry("codex").to_dict()
    assert [mode["key"] for mode in payload["auth_modes"]] == ["chatgpt", "api_key", "routed"]


# --------------------------------------------------------------------------- #
# conflicts
# --------------------------------------------------------------------------- #
def test_env_key_conflicts_with_a_subscription_session() -> None:
    """An exported key can silently bill you instead of your plan — so warn."""

    entry = resolve_entry("codex")
    mode = resolve_auth_mode(entry, "chatgpt")
    conflicts = detect_auth_conflicts(entry, mode, env={"OPENAI_API_KEY": "sk-test"})
    titles = " ".join(item.title for item in conflicts)
    assert "OPENAI_API_KEY" in titles
    warning = next(item for item in conflicts if "OPENAI_API_KEY" in item.title)
    assert warning.severity == "warn"
    assert "unset" in warning.fix


def test_no_env_key_means_no_override_warning() -> None:
    entry = resolve_entry("codex")
    mode = resolve_auth_mode(entry, "chatgpt")
    conflicts = detect_auth_conflicts(entry, mode, env={})
    assert not any("OPENAI_API_KEY is set" in item.title for item in conflicts)


def test_missing_cli_is_an_error_not_a_warning() -> None:
    entry = resolve_entry("codex")
    mode = resolve_auth_mode(entry, "chatgpt")
    conflicts = detect_auth_conflicts(entry, mode, env={})
    # `codex` is not installed in CI, so this must be reported as blocking.
    assert any(item.severity == "error" for item in conflicts)


def test_stored_key_plus_env_key_is_flagged() -> None:
    entry = resolve_entry("openai")
    mode = resolve_auth_mode(entry, "api_key")
    conflicts = detect_auth_conflicts(entry, mode, env={"OPENAI_API_KEY": "sk-test"})
    assert any("also set" in item.title for item in conflicts)


# --------------------------------------------------------------------------- #
# error classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("HTTP 402: insufficient credits", ProviderStatus.CREDITS_EXHAUSTED),
        ("insufficient_quota for this account", ProviderStatus.CREDITS_EXHAUSTED),
        ("Payment Required", ProviderStatus.CREDITS_EXHAUSTED),
        ("401 invalid api key", ProviderStatus.INVALID_CREDENTIALS),
        ("unauthorized", ProviderStatus.INVALID_CREDENTIALS),
        ("429 rate limit exceeded", ProviderStatus.RATE_LIMITED),
        ("Temporary failure in name resolution", ProviderStatus.OFFLINE),
        ("request timed out", ProviderStatus.OFFLINE),
        ("something entirely unexpected", ProviderStatus.DEGRADED),
    ],
)
def test_classify_provider_error(error: str, expected: str) -> None:
    assert classify_provider_error(error) == expected


def test_402_is_never_classified_as_a_credential_problem() -> None:
    """A paying-account problem must not read as 'your key is wrong'."""

    assert classify_provider_error("402 payment required") != ProviderStatus.INVALID_CREDENTIALS


# --------------------------------------------------------------------------- #
# health checks
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_missing_credential_is_reported_not_raised() -> None:
    entry = resolve_entry("openai")
    mode = resolve_auth_mode(entry, "api_key")
    report = await run_provider_check(entry, mode, api_key=None, env={})
    assert report.status == ProviderStatus.NOT_CONFIGURED
    assert report.ok is False
    assert report.stage("credential").state == "fail"
    assert "run setup" in " ".join(report.remedies).lower()


@pytest.mark.asyncio
async def test_subscription_without_cli_reports_setup_required() -> None:
    entry = resolve_entry("codex")
    mode = resolve_auth_mode(entry, "chatgpt")
    report = await run_provider_check(entry, mode, env={})
    # No `codex` binary in CI: a blocking conflict, reported as data.
    assert report.status in {ProviderStatus.SETUP_REQUIRED, ProviderStatus.LOGIN_REQUIRED}
    assert report.ok is False
    assert report.hint


@pytest.mark.asyncio
async def test_report_serialises_for_machine_output() -> None:
    entry = resolve_entry("openai")
    payload = (await run_provider_check(entry, resolve_auth_mode(entry, "api_key"), env={})).to_dict()
    for key in ("status", "status_label", "ok", "usable", "stages", "conflicts", "remedies"):
        assert key in payload


def test_provider_status_labels_cover_every_state() -> None:
    """Every state the UI can enter must have a label and a style."""

    for name in dir(ProviderStatus):
        if name.isupper() and isinstance(getattr(ProviderStatus, name), str) and name != "LABELS":
            value = getattr(ProviderStatus, name)
            assert ProviderStatus.label(value)
            assert ProviderStatus.style(value)
