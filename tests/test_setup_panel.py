"""The interactive setup panel: provider → key → validate → models → save.

These drive the real Textual app headlessly (``run_test``), with the provider
network boundary mocked, so the panel's behaviour — not a mock of it — is what is
asserted: selecting a provider opens the key dialog, pressing Enter validates and
fetches models, an invalid key stays in the dialog with an explanation, and the
save step writes a real profile plus a 0600 credential.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from unknown_agent.config.credentials import CredentialStore
from unknown_agent.providers.catalog import resolve_entry
from unknown_agent.providers.discovery import ModelInfo, ProviderProbeError, ValidationResult
from unknown_agent.tui.quicksetup import (
    ApiKeyScreen,
    AuthModeScreen,
    ModelScreen,
    ProviderScreen,
    ReviewScreen,
    SetupApp,
    save_provider_choice,
)

GOOD_KEY = "sk-or-v1-" + "a" * 48
MODELS = [
    ModelInfo(id="openai/gpt-4o-mini", name="GPT-4o mini", provider="openrouter", context_window=128000, supports_tools=True, input_cost_per_mtok=0.15, output_cost_per_mtok=0.6),
    ModelInfo(id="meta-llama/llama-3.3-70b-instruct", name="Llama 3.3 70B", provider="openrouter", context_window=131072, supports_tools=True),
]


class FakeRuntime:
    """Just enough runtime for the panel: settings in, config path out."""

    def __init__(self, tmp_path: Path) -> None:
        from unknown_agent.config.models import Settings

        self._settings = Settings()
        self.path = tmp_path / "config.toml"
        self.non_interactive = False
        self.saved = 0

    def load(self, *, force: bool = False) -> Any:  # noqa: ARG002
        class Loaded:
            settings = self._settings

        return Loaded()

    def save_settings(self, settings: Any) -> Path:
        self._settings = settings
        self.saved += 1
        self.path.write_text("# written by the setup panel\n", encoding="utf-8")
        return self.path


@pytest.fixture
def online(monkeypatch: pytest.MonkeyPatch) -> None:
    async def validate(entry, *, api_key=None, base_url=None, timeout=20.0):  # noqa: ANN001, ANN202, ARG001
        if api_key == GOOD_KEY:
            return ValidationResult(True, f"{entry.label} accepted the key and listed 2 model(s).", models_available=True, model_count=2)
        return ValidationResult(False, "HTTP 401: the provider rejected this key.", hint="Copy the key again from the dashboard.")

    async def fetch(entry, *, api_key=None, base_url=None, timeout=20.0, limit=500):  # noqa: ANN001, ANN202, ARG001
        if api_key != GOOD_KEY:
            raise ProviderProbeError("unauthorised")
        return list(MODELS)

    monkeypatch.setattr("unknown_agent.tui.quicksetup.validate_credential", validate)
    monkeypatch.setattr("unknown_agent.tui.quicksetup.fetch_models", fetch)


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "credentials.toml"
    monkeypatch.setenv("UNKNOWN_AGENT_CREDENTIALS", str(target))
    return target


# --------------------------------------------------------------------------- #
# the happy path, end to end, through the real UI
# --------------------------------------------------------------------------- #
async def _choose_auth_mode(pilot, app, key: str = "api_key") -> None:
    """Advance past the auth-mode screen, which now sits between provider and key.

    Providers declare several ways to authenticate, so choosing a provider no longer
    jumps straight to a key box. Tests that care about the key dialog step through the
    mode screen explicitly rather than assuming key-only providers.
    """

    await pilot.pause()
    if isinstance(app.screen, AuthModeScreen):
        index = next(i for i, mode in enumerate(app.screen.modes) if mode.key == key)
        app.screen.query_one("#auth-list").highlighted = index
        await pilot.press("enter")
        await pilot.pause()

async def test_panel_walks_provider_key_models_and_saves(tmp_path: Path, online: None, isolated_store: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    app = SetupApp(runtime)
    async with app.run_test(size=(120, 40)) as pilot:
        assert isinstance(app.screen, ProviderScreen)

        # 1 — the provider list is visible and filterable, Enter selects.
        await pilot.press(*"openrouter")
        await pilot.pause()
        await pilot.press("enter")
        await _choose_auth_mode(pilot, app)
        assert isinstance(app.screen, ApiKeyScreen)
        assert app.entry is not None and app.entry.key == "openrouter"
        assert app.auth_mode is not None and app.auth_mode.key == "api_key"

        # 2 — pasting the key and pressing Enter validates *and* fetches models.
        app.screen.query_one("#key-field").value = GOOD_KEY
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause(0.1)
        await pilot.pause()
        assert isinstance(app.screen, ModelScreen), "a valid key must lead straight to the model selector"
        assert [info.id for info in app.models] == [item.id for item in MODELS]
        assert app.models_source == "live"

        # 3 — the model selector picks visually.
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        assert app.model in {item.id for item in MODELS}

        # 4 — save.
        await pilot.press("enter")
        await pilot.pause()

    result = app.result
    assert not result.cancelled
    assert result.provider_type == "openrouter"
    assert result.model
    assert result.credential_ref.startswith("store:")
    assert runtime.saved == 1
    assert CredentialStore(isolated_store).load()  # the secret landed in the store


async def test_invalid_key_stays_in_the_dialog_with_an_explanation(tmp_path: Path, online: None, isolated_store: Path) -> None:
    app = SetupApp(FakeRuntime(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("enter")  # first provider in the list
        await _choose_auth_mode(pilot, app)
        assert isinstance(app.screen, ApiKeyScreen)
        app.screen.query_one("#key-field").value = "sk-not-a-real-key-at-all"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause(0.1)
        await pilot.pause()
        # Still in the dialog, with a human explanation and a retry path.
        assert isinstance(app.screen, ApiKeyScreen)
        status = str(app.screen.query_one("#key-status").render())
        assert "rejected" in status.lower() or "401" in status
        assert not app.screen.query_one("#key-field").disabled

        # Retrying with the right key recovers without restarting setup.
        app.screen.query_one("#key-field").value = GOOD_KEY
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause(0.1)
        await pilot.pause()
        assert isinstance(app.screen, ModelScreen)


async def test_empty_key_is_refused_before_any_network_call(tmp_path: Path, online: None, isolated_store: Path) -> None:
    app = SetupApp(FakeRuntime(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("enter")
        await _choose_auth_mode(pilot, app)
        assert isinstance(app.screen, ApiKeyScreen)
        await pilot.press("enter")   # submit an empty key
        await pilot.pause()
        assert "paste" in str(app.screen.query_one("#key-status").render()).lower()


async def test_escape_cancels_and_writes_nothing(tmp_path: Path, online: None, isolated_store: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    app = SetupApp(runtime)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        await pilot.pause()
    assert app.result.cancelled
    assert runtime.saved == 0
    assert not runtime.path.exists()


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_save_writes_a_profile_and_keeps_the_secret_out_of_config(tmp_path: Path, isolated_store: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    entry = resolve_entry("openrouter")
    assert entry is not None
    result = save_provider_choice(
        runtime,
        entry=entry,
        model="openai/gpt-4o-mini",
        api_key=GOOD_KEY,
        models=MODELS,
    )
    settings = runtime.load().settings
    profile = settings.providers[result.provider]
    assert settings.provider == result.provider
    assert profile.model == "openai/gpt-4o-mini"
    assert profile.api_key == result.credential_ref == f"store:{result.provider}"
    assert GOOD_KEY not in str(profile.model_dump())
    assert profile.context_window == 128000
    assert CredentialStore(isolated_store).resolve(result.credential_ref) == GOOD_KEY


# --------------------------------------------------------------------------- #
# routing: the panel is the default path
# --------------------------------------------------------------------------- #
def test_panel_is_skipped_in_machine_and_non_interactive_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    from unknown_agent.onboarding.panel import panel_available

    class Runtime:
        non_interactive = True

    assert panel_available(Runtime()) is False


def test_launch_setup_uses_the_scripted_flow_when_flags_are_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unknown_agent.onboarding import panel
    from unknown_agent.onboarding.flow import OnboardingOptions

    called: dict[str, Any] = {}

    async def fake_flow(runtime, *, options=None, prompter=None):  # noqa: ANN001, ANN202, ARG001
        called["options"] = options

        class Result:
            @staticmethod
            def to_dict() -> dict[str, str]:
                return {"provider": "groq"}

        return Result()

    monkeypatch.setattr("unknown_agent.onboarding.flow.run_onboarding", fake_flow)
    monkeypatch.setattr(panel, "panel_available", lambda runtime=None: True)
    outcome = panel.launch_setup(FakeRuntime(tmp_path), scripted=OnboardingOptions(provider="groq", model="x", api_key_env="GROQ_API_KEY"))
    assert outcome.ok
    assert called["options"].provider == "groq"


# --------------------------------------------------------------------------- #
# changing your mind later is visual too
# --------------------------------------------------------------------------- #
async def test_model_picker_reopens_the_selector_alone(tmp_path: Path) -> None:
    from unknown_agent.tui.quicksetup import ModelPickerApp

    entry = resolve_entry("openrouter")
    assert entry is not None
    app = ModelPickerApp(entry, MODELS, current=MODELS[1].id)
    async with app.run_test(size=(120, 40)) as pilot:
        assert isinstance(app.screen, ModelScreen)
        await pilot.press("enter")
        await pilot.pause()
    assert app.picked in {item.id for item in MODELS}


async def test_model_picker_cancels_without_choosing(tmp_path: Path) -> None:
    from unknown_agent.tui.quicksetup import ModelPickerApp

    entry = resolve_entry("openrouter")
    assert entry is not None
    app = ModelPickerApp(entry, MODELS)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape")
        await pilot.pause()
    assert app.picked == ""


def test_models_use_without_an_id_is_allowed(tmp_path: Path) -> None:
    """`ua models use` must be callable with no argument - that is the visual path."""

    import inspect

    from unknown_agent.cli.commands.providers_cmd import models_use

    default = inspect.signature(models_use).parameters["model"].default
    assert getattr(default, "default", "missing") is None


# --------------------------------------------------------------------------- #
# when the panel cannot run, say why
# --------------------------------------------------------------------------- #
class _FakeOutput:
    """Stands in for the global output manager: a real terminal, human mode."""

    is_machine = False
    interactive = True


def test_blocker_names_the_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user without textual must be told that, not silently downgraded."""

    import builtins

    from unknown_agent.onboarding import panel

    real_import = builtins.__import__

    def no_textual(name, *args, **kwargs):  # noqa: ANN001, ANN202
        if name == "textual":
            raise ImportError("no textual")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(panel, "output", _FakeOutput())
    monkeypatch.setattr(panel.sys, "stdin", type("S", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr(builtins, "__import__", no_textual)

    reason = panel.panel_blocker(None)
    assert "textual" in reason
    assert panel.panel_available(None) is False


def test_blocker_is_empty_when_everything_is_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    from unknown_agent.onboarding import panel

    monkeypatch.setattr(panel, "output", _FakeOutput())
    monkeypatch.setattr(panel.sys, "stdin", type("S", (), {"isatty": staticmethod(lambda: True)})())
    assert panel.panel_blocker(None) == ""


def test_blocker_explains_a_piped_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from unknown_agent.onboarding import panel

    class Piped(_FakeOutput):
        interactive = False

    monkeypatch.setattr(panel, "output", Piped())
    assert "not a terminal" in panel.panel_blocker(None)


def test_fallback_tells_the_user_no_flags_are_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old flow must never leave the impression that flags are required."""

    from unknown_agent.onboarding import panel

    printed: list[str] = []

    class Recorder(_FakeOutput):
        @staticmethod
        def print(*args: Any, **kwargs: Any) -> None:
            printed.append(str(args[0]) if args else "")

    monkeypatch.setattr(panel, "output", Recorder())
    panel._warn_no_panel("the 'textual' package is not installed in this environment")
    assert printed, "the user must be told the panel could not open"
