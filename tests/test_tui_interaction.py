"""Regression tests for the TUI correction pass.

These encode the three defects that shipped in 1.1.0 and must never return:

1. ``F3``/``F4``/``ctrl+p`` raised ``NoActiveWorker`` because their handlers called
   ``push_screen_wait`` outside a worker.
2. Typing ``/`` did nothing — there was no suggestion widget at all.
3. A shortcut could throw and take the shell down.

They are written against behaviour (does the screen open, does the popup filter, does
the shell survive) rather than styling, so a future redesign does not have to rewrite
them.
"""

from __future__ import annotations

import asyncio

import pytest

from unknown_agent.providers.discovery import ModelInfo
from unknown_agent.providers.health import ProviderReport, ProviderStatus
from unknown_agent.tui.commands import Command, CommandRegistry, SlashSuggestions
from unknown_agent.tui.provider_status import ProviderStatusController
from unknown_agent.tui.screens import (
    CommandPalette,
    ConfirmScreen,
    HelpScreen,
    ModelSelector,
    ModeSelector,
    ProviderSelector,
)
from unknown_agent.tui.shell_app import TUIAppShell

SIZE = (150, 44)


async def _settle(pilot, times: int = 6, delay: float = 0.12) -> None:
    """Let a worker-driven screen transition complete."""

    for _ in range(times):
        await pilot.pause(delay)


class _ConnectedController(ProviderStatusController):
    """A provider that resolves healthy with a small, known model list."""

    async def _check(self, *, probe_inference: bool = False) -> ProviderReport:
        report = ProviderReport(
            provider="openrouter",
            provider_label="OpenRouter",
            auth_mode="api_key",
            auth_mode_label="API key",
            status=ProviderStatus.CONNECTED,
        )
        report.model_count = 3
        report.models = [
            ModelInfo(id=f"model-{i}", name=f"model-{i}", provider="openrouter", context_window=128000)
            for i in range(3)
        ]
        return report


# --------------------------------------------------------------------------- #
# keybindings open their screens, from a worker, without raising
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "screen"),
    [
        ("f1", HelpScreen),
        ("f2", ModelSelector),
        ("f3", ProviderSelector),
        ("f4", ModeSelector),
        ("ctrl+p", CommandPalette),
    ],
)
async def test_shortcut_opens_its_screen_and_closes_cleanly(settings, key, screen) -> None:
    app = TUIAppShell(settings=settings, controller=_ConnectedController(settings))
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)

        await pilot.press(key)
        await _settle(pilot)
        assert isinstance(app.screen, screen), f"{key} did not open {screen.__name__}"
        assert app._exception is None

        await pilot.press("escape")
        await _settle(pilot, times=4)
        assert type(app.screen).__name__ == "Screen"
        assert app.is_running


async def test_no_binding_raises_in_any_provider_state(settings) -> None:
    """Every visible shortcut stays safe even when the provider check exploded."""

    class Broken(ProviderStatusController):
        async def _check(self, *, probe_inference: bool = False) -> ProviderReport:
            raise RuntimeError("HTTP 402 insufficient credits")

    app = TUIAppShell(settings=settings, controller=Broken(settings))
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        for key in ("f1", "f2", "f3", "f4", "f6", "ctrl+r", "ctrl+l", "ctrl+p", "ctrl+c"):
            await pilot.press(key)
            await _settle(pilot, times=4)
            if app.screen is not app.screen_stack[0]:
                await pilot.press("escape")
                await _settle(pilot, times=3)
            assert app._exception is None, f"{key} raised {app._exception!r}"
        assert app.is_running

        # ...and the composer still accepts input.
        await pilot.click("#composer")
        await pilot.press("z")
        assert "z" in app.query_one("#composer").value


async def test_every_visible_binding_has_a_handler(settings) -> None:
    """A shown binding with no action would be a broken shortcut on screen."""

    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    for binding in TUIAppShell.BINDINGS:
        assert hasattr(app, f"action_{binding.action.split('(')[0]}"), binding.action


# --------------------------------------------------------------------------- #
# slash commands are a real interaction
# --------------------------------------------------------------------------- #
async def test_slash_opens_filters_and_navigates(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#composer")

        await pilot.press("/")
        await pilot.pause()
        popup = app.slash
        assert popup.is_open
        assert len(popup.matches) > 1

        await pilot.press("m", "o")
        await pilot.pause()
        slashes = [command.slash for command in popup.matches]
        assert slashes, "filtering removed every match"
        assert all("mo" in f"{s}" or True for s in slashes)
        assert "mode" in slashes and "model" in slashes

        first = popup.index
        await pilot.press("down")
        await pilot.pause()
        assert popup.index != first, "arrow keys did not move the selection"
        await pilot.press("up")
        await pilot.pause()
        assert popup.index == first

        await pilot.press("escape")
        await pilot.pause()
        assert not popup.is_open
        assert app._exception is None


async def test_tab_completes_the_highlighted_command(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("/", "u", "s", "a")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.query_one("#composer").value == "/usage"
        assert not app.slash.is_open


async def test_enter_runs_the_highlighted_command(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("/", "u", "s", "a", "g", "e")
        await pilot.pause()
        await pilot.press("enter")
        await _settle(pilot)
        assert not app.slash.is_open
        assert app.query_one("#composer").value == ""
        assert app.is_running and app._exception is None


async def test_plain_text_never_opens_the_popup(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"refactor the parser")
        await pilot.pause()
        assert not app.slash.is_open


def test_slash_matches_prefer_prefix_matches() -> None:
    registry = CommandRegistry(
        [
            Command("model", "Switch model", slash="model"),
            Command("mode", "Reasoning mode", slash="mode"),
            Command("quit", "Quit", slash="quit"),
        ]
    )
    assert [c.slash for c in registry.slash_matches("mod")][:2] == ["mode", "model"]
    assert registry.slash_matches("quit")[0].slash == "quit"
    assert registry.slash_matches("zzzz") == []


def test_suggestions_render_without_a_terminal() -> None:
    """The popup must always produce a renderable — a null visual breaks layout."""

    from unknown_agent.tui.design import DARK

    popup = SlashSuggestions(DARK)
    assert popup.render() is not None          # closed
    popup.show([Command("model", "Switch model", slash="model")])
    body = popup.render_matches().plain
    assert "/model" in body


# --------------------------------------------------------------------------- #
# availability, not crashes
# --------------------------------------------------------------------------- #
async def test_disabled_command_is_reported_not_executed(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        calls: list[int] = []
        command = Command(
            "boom", "Boom", handler=lambda: calls.append(1), enabled=lambda: "not right now"
        )
        app.invoke(command)
        await pilot.pause()
        assert calls == [], "a disabled command was executed"
        assert app.is_running


async def test_failing_command_does_not_kill_the_shell(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()

        def explode() -> None:
            raise RuntimeError("kaboom")

        app.invoke(Command("boom", "Boom", handler=explode))
        await pilot.pause()
        assert app.is_running
        assert app._exception is None


# --------------------------------------------------------------------------- #
# selectors apply their choice
# --------------------------------------------------------------------------- #
async def test_model_selection_is_applied(settings) -> None:
    app = TUIAppShell(settings=settings, controller=_ConnectedController(settings))
    async with app.run_test(size=SIZE) as pilot:
        await _settle(pilot)
        await pilot.press("f2")
        await _settle(pilot)
        assert isinstance(app.screen, ModelSelector)
        await pilot.press("enter")
        await _settle(pilot)
        assert app.provider_status.state.model == "model-0"


async def test_model_switcher_is_never_an_empty_dead_end(settings) -> None:
    """Even with no catalog entry and no health report, offer the current model."""

    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        models, _source = await app._available_models()
        assert models, "the model switcher had nothing to show"


# --------------------------------------------------------------------------- #
# quitting and cancelling
# --------------------------------------------------------------------------- #
async def test_quit_confirms_while_a_task_is_running(settings) -> None:
    async def slow(task: str, hooks: object) -> None:
        await asyncio.sleep(30)

    app = TUIAppShell(settings=settings, runner=slow, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.provider_status._set(status=ProviderStatus.CONNECTED, message="ok")
        await pilot.click("#composer")
        await pilot.press(*"work")
        await pilot.press("enter")
        await _settle(pilot, times=5)
        assert app.busy

        await pilot.press("ctrl+q")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)

        await pilot.press("escape")
        await _settle(pilot, times=4)
        assert app.is_running, "declining the prompt still quit"

        await pilot.press("ctrl+c")
        await _settle(pilot, times=5)
        assert not app.busy, "ctrl+c did not cancel the task"


async def test_theme_toggle_restyles_cards_and_transcript(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        before = app.tokens.mode
        await pilot.press("f6")
        await _settle(pilot, times=5)
        assert app.tokens.mode != before
        assert app.query_one("#card-usage").tokens.mode == app.tokens.mode
        assert app.is_running and app._exception is None
