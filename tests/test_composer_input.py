"""The chat composer behaves like a chat input: history, draft, clear, focus.

These drive the real :class:`TUIAppShell` headlessly with no provider attached, so
submitting a prompt records history and degrades to a friendly note instead of a
crash.  What is asserted is the *input* behaviour — the part a user's fingers feel:

* ↑/↓ walk the prompt history (and recall does not re-open the slash popup);
* walking back past the newest entry restores the unsent draft;
* Esc clears the composer;
* the composer keeps focus after a send, so the next message needs no extra tap.
"""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Input  # noqa: E402

from unknown_agent.tui.shell_app import TUIAppShell  # noqa: E402


def _shell() -> TUIAppShell:
    return TUIAppShell(settings=None, resolve_on_mount=False)


async def _submit(pilot, app, text: str) -> None:
    composer = app.query_one("#composer", Input)
    composer.value = text
    await pilot.press("enter")
    await pilot.pause()


async def test_up_down_walk_the_prompt_history() -> None:
    app = _shell()
    async with app.run_test(size=(100, 30)) as pilot:
        await _submit(pilot, app, "hola")
        await _submit(pilot, app, "segunda pregunta")
        composer = app.query_one("#composer", Input)
        assert composer.value == ""

        await pilot.press("up")
        assert composer.value == "segunda pregunta"
        await pilot.press("up")
        assert composer.value == "hola"
        await pilot.press("down")
        assert composer.value == "segunda pregunta"
        await pilot.press("down")
        assert composer.value == "", "past the newest entry the box is empty again"


async def test_recall_preserves_and_restores_the_unsent_draft() -> None:
    app = _shell()
    async with app.run_test(size=(100, 30)) as pilot:
        await _submit(pilot, app, "primera")
        composer = app.query_one("#composer", Input)
        composer.value = "borrador a medias"

        await pilot.press("up")
        assert composer.value == "primera"
        await pilot.press("down")
        assert composer.value == "borrador a medias", "the draft must come back"


async def test_recall_does_not_reopen_the_slash_popup() -> None:
    app = _shell()
    async with app.run_test(size=(100, 30)) as pilot:
        await _submit(pilot, app, "hola")
        await pilot.press("up")
        await pilot.pause()
        assert app.slash.is_open is False
        assert app.query_one("#composer", Input).value == "hola"


async def test_escape_clears_the_composer() -> None:
    app = _shell()
    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#composer", Input)
        composer.value = "texto que estorba"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert composer.value == ""


async def test_composer_keeps_focus_after_sending() -> None:
    app = _shell()
    async with app.run_test(size=(100, 30)) as pilot:
        await _submit(pilot, app, "hola")
        await pilot.pause()
        assert app.query_one("#composer", Input) is app.focused


async def test_up_with_empty_history_is_a_no_op() -> None:
    app = _shell()
    async with app.run_test(size=(100, 30)) as pilot:
        composer = app.query_one("#composer", Input)
        composer.value = "algo"
        await pilot.press("up")
        await pilot.pause()
        # Nothing submitted yet: the draft must be untouched, not clobbered.
        assert composer.value == "algo"
