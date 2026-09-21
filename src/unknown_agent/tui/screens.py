"""Modal surfaces: command palette, and the model / provider / mode selectors.

All four share :class:`ChooserScreen`, so they behave identically — type to filter,
arrows to move, Enter to accept, Escape to cancel — and are styled once. Consistency
across modals is most of what makes an interface feel designed rather than assembled.

Every screen returns a value through ``dismiss`` instead of mutating global state. The
shell decides what a choice means, which keeps these screens independently testable
and keeps mutation in one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from unknown_agent.tui.commands import Command, CommandRegistry
from unknown_agent.tui.design import GLYPH, Tokens, truncate

__all__ = ["Choice", "ChooserScreen", "CommandPalette", "ModelSelector", "ProviderSelector", "ModeSelector", "HelpScreen", "ConfirmScreen"]


#: Shared chrome for every modal. One border weight, one radius, generous internal
#: padding, and a dimmed scrim so the modal reads as floating above the shell.
_MODAL_CSS = """
ChooserScreen, CommandPalette, ModelSelector, ProviderSelector, ModeSelector, HelpScreen, ConfirmScreen {
    align: center middle;
    background: $background 60%;
}
ChooserScreen > #chooser, CommandPalette > #chooser, ModelSelector > #chooser,
ProviderSelector > #chooser, ModeSelector > #chooser {
    width: 92;
    max-width: 92%;
    height: auto;
    max-height: 80%;
    background: $surface;
    border: round $primary 45%;
    padding: 1 2;
}
#chooser-title { color: $text; text-style: bold; height: 1; }
#chooser-caption { color: $text-muted; height: auto; margin-bottom: 1; }
#chooser-input {
    border: round $primary 25%;
    background: $boost;
    height: 3;
    padding: 0 1;
    margin-bottom: 1;
    color: $text;
}
#chooser-input:focus { border: round $primary 60%; }
#chooser-list {
    height: auto;
    max-height: 16;
    background: transparent;
    border: none;
    padding: 0;
    scrollbar-size: 1 1;
}
#chooser-list > ListItem {
    background: transparent;
    padding: 0 1;
    height: auto;
    border-left: blank;
}
#chooser-list > ListItem.-highlight,
#chooser-list:focus > ListItem.-highlight {
    background: $accent 15%;
    border-left: thick $accent;
}
#chooser-empty { display: none; color: $text-muted; padding: 1; height: auto; }
#chooser-empty.visible { display: block; }
#chooser-footer { color: $text-muted; height: 1; margin-top: 1; }
"""


@dataclass(slots=True)
class Choice:
    """One selectable row."""

    value: str
    label: str
    detail: str = ""
    badge: str = ""
    current: bool = False
    #: Non-empty disables the row and explains why.
    disabled_reason: str = ""

    @property
    def enabled(self) -> bool:
        return not self.disabled_reason

    @property
    def haystack(self) -> str:
        return f"{self.label} {self.detail} {self.badge} {self.value}".lower()


class ChooserScreen(ModalScreen[str | None]):
    """A filterable list that returns the chosen value, or ``None`` if cancelled."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("down", "move_down", "down", show=False, priority=True),
        Binding("up", "move_up", "up", show=False, priority=True),
        Binding("enter", "accept", "select", show=False, priority=True),
    ]

    DEFAULT_CSS = _MODAL_CSS

    def __init__(
        self,
        title: str,
        choices: Sequence[Choice],
        *,
        tokens: Tokens,
        caption: str = "",
        placeholder: str = "Filter…",
        footer: str = "↑↓ move    enter select    esc cancel",
        empty_text: str = "Nothing matches that filter.",
    ) -> None:
        super().__init__()
        self.heading = title
        self.caption = caption
        self.choices = list(choices)
        self.tokens = tokens
        self.placeholder = placeholder
        self.footer_text = footer
        self.empty_text = empty_text
        self._visible: list[Choice] = list(choices)

    def compose(self) -> ComposeResult:
        with Vertical(id="chooser"):
            yield Static(self.heading, id="chooser-title")
            if self.caption:
                yield Static(self.caption, id="chooser-caption")
            yield Input(placeholder=self.placeholder, id="chooser-input")
            yield ListView(id="chooser-list")
            yield Static("", id="chooser-empty")
            yield Static(self.footer_text, id="chooser-footer")

    def on_mount(self) -> None:
        self._populate(self.choices)
        self.query_one("#chooser-input", Input).focus()

    # -- list management ---------------------------------------------------- #
    def _populate(self, choices: Sequence[Choice]) -> None:
        self._visible = list(choices)
        listing = self.query_one("#chooser-list", ListView)
        listing.clear()
        for choice in self._visible:
            listing.append(ListItem(Label(self._render_choice(choice))))

        empty = self.query_one("#chooser-empty", Static)
        if self._visible:
            empty.remove_class("visible")
            listing.display = True
            current = next((i for i, item in enumerate(self._visible) if item.current), 0)
            listing.index = current
        else:
            listing.display = False
            empty.add_class("visible")
            empty.update(self.empty_text)

    def _render_choice(self, choice: Choice) -> Text:
        tokens = self.tokens
        row = Text()
        # A single leading marker column keeps every row aligned whether or not it is
        # the active one — no reflow when the selection moves.
        if choice.current:
            row.append(f"{GLYPH['ready']} ", style=tokens.accent)
        else:
            row.append("  ")

        label_style = tokens.faint if choice.disabled_reason else (f"bold {tokens.text}" if choice.current else tokens.text)
        row.append(f"{truncate(choice.label, 34):<34}", style=label_style)

        if choice.badge:
            row.append(f"{truncate(choice.badge, 16):<16}", style=tokens.faint if choice.disabled_reason else tokens.accent_dim)
        detail = choice.disabled_reason or choice.detail
        if detail:
            row.append(truncate(detail, 22), style=tokens.faint if choice.disabled_reason else tokens.muted)
        return row

    def _filter(self, query: str) -> None:
        query = query.strip()
        if not query:
            self._populate(self.choices)
            return
        from unknown_agent.tui.commands import fuzzy_score

        scored = ((fuzzy_score(query, item.haystack), item) for item in self.choices)
        hits = sorted(((s, i) for s, i in scored if s > 0), key=lambda pair: -pair[0])
        self._populate([item for _, item in hits])

    @on(Input.Changed, "#chooser-input")
    def _on_filter(self, event: Input.Changed) -> None:
        self._filter(event.value)

    @on(ListView.Selected, "#chooser-list")
    def _on_click(self, event: ListView.Selected) -> None:
        self._accept_index(event.list_view.index or 0)

    # -- actions ------------------------------------------------------------- #
    def action_move_down(self) -> None:
        self._move(1)

    def action_move_up(self) -> None:
        self._move(-1)

    def _move(self, delta: int) -> None:
        listing = self.query_one("#chooser-list", ListView)
        if not self._visible:
            return
        listing.index = ((listing.index or 0) + delta) % len(self._visible)

    def action_accept(self) -> None:
        self._accept_index(self.query_one("#chooser-list", ListView).index or 0)

    def _accept_index(self, index: int) -> None:
        if 0 <= index < len(self._visible):
            choice = self._visible[index]
            if choice.enabled:
                self.dismiss(choice.value)
                return
        # Disabled rows are visible but inert: the reason is already on screen.

    def action_cancel(self) -> None:
        self.dismiss(None)


class CommandPalette(ModalScreen[Command | None]):
    """Fuzzy search over every command, grouped and annotated with shortcuts."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("down", "move_down", "down", show=False, priority=True),
        Binding("up", "move_up", "up", show=False, priority=True),
        Binding("enter", "accept", "run", show=False, priority=True),
    ]

    DEFAULT_CSS = _MODAL_CSS

    def __init__(self, registry: CommandRegistry, *, tokens: Tokens) -> None:
        super().__init__()
        self.registry = registry
        self.tokens = tokens
        self._results: list[Command] = registry.all()

    def compose(self) -> ComposeResult:
        with Vertical(id="chooser"):
            yield Static("Commands", id="chooser-title")
            yield Static("Search every action in the shell.", id="chooser-caption")
            yield Input(placeholder="Type a command…", id="chooser-input")
            yield ListView(id="chooser-list")
            yield Static("", id="chooser-empty")
            yield Static("↑↓ move    enter run    esc close", id="chooser-footer")

    def on_mount(self) -> None:
        self._populate(self.registry.all())
        self.query_one("#chooser-input", Input).focus()

    def _populate(self, commands: Sequence[Command]) -> None:
        self._results = list(commands)
        listing = self.query_one("#chooser-list", ListView)
        listing.clear()
        for command in self._results:
            listing.append(ListItem(Label(self._render_command(command))))
        empty = self.query_one("#chooser-empty", Static)
        if self._results:
            empty.remove_class("visible")
            listing.display = True
            listing.index = 0
        else:
            listing.display = False
            empty.add_class("visible")
            empty.update("No command matches that.")

    def _render_command(self, command: Command) -> Text:
        tokens = self.tokens
        reason = command.availability()
        row = Text()
        row.append("  ")
        row.append(f"{truncate(command.title, 28):<29}", style=tokens.faint if reason else tokens.text)
        row.append(f"{command.shortcut or '':<9}", style=tokens.faint)
        detail = reason or command.description
        row.append(truncate(detail, 44), style=tokens.faint if reason else tokens.muted)
        return row

    @on(Input.Changed, "#chooser-input")
    def _on_filter(self, event: Input.Changed) -> None:
        self._populate(self.registry.search(event.value))

    @on(ListView.Selected, "#chooser-list")
    def _on_click(self, event: ListView.Selected) -> None:
        self._accept_index(event.list_view.index or 0)

    def action_move_down(self) -> None:
        self._move(1)

    def action_move_up(self) -> None:
        self._move(-1)

    def _move(self, delta: int) -> None:
        if not self._results:
            return
        listing = self.query_one("#chooser-list", ListView)
        listing.index = ((listing.index or 0) + delta) % len(self._results)

    def action_accept(self) -> None:
        self._accept_index(self.query_one("#chooser-list", ListView).index or 0)

    def _accept_index(self, index: int) -> None:
        if 0 <= index < len(self._results):
            command = self._results[index]
            if command.available:
                self.dismiss(command)
                return

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- #
# concrete selectors
# --------------------------------------------------------------------------- #
class ModelSelector(ChooserScreen):
    """Pick a model, with capabilities and context size visible for each."""

    def __init__(self, models: Sequence[Any], *, tokens: Tokens, current: str = "", source: str = "catalog") -> None:
        choices: list[Choice] = []
        for item in models:
            model_id = getattr(item, "id", str(item))
            badges = []
            if getattr(item, "supports_tools", None):
                badges.append("tools")
            if getattr(item, "supports_vision", False):
                badges.append("vision")
            if getattr(item, "supports_reasoning", False):
                badges.append("thinks")
            window = int(getattr(item, "context_window", 0) or 0)
            choices.append(
                Choice(
                    value=model_id,
                    label=model_id,
                    detail=f"{window // 1000}k context" if window else "",
                    badge=" ".join(badges),
                    current=model_id == current,
                )
            )
        caption = (
            f"{len(choices)} models from the live provider list"
            if source == "live"
            else f"{len(choices)} models from the bundled catalog — the live list was unavailable"
        )
        super().__init__("Model", choices, tokens=tokens, caption=caption, placeholder="Filter models…")


class ProviderSelector(ChooserScreen):
    """Pick a configured provider profile, or jump to setup."""

    def __init__(self, profiles: Sequence[Any], *, tokens: Tokens, current: str = "") -> None:
        choices = [
            Choice(
                value=getattr(item, "name", str(item)),
                label=getattr(item, "name", str(item)),
                detail=getattr(item, "detail", ""),
                badge=getattr(item, "badge", ""),
                current=getattr(item, "name", str(item)) == current,
            )
            for item in profiles
        ]
        choices.append(Choice(value="__setup__", label="Configure a new provider…", detail="opens setup"))
        caption = "Switching is immediate; the new provider is verified in the background."
        super().__init__("Provider", choices, tokens=tokens, caption=caption, placeholder="Filter providers…")


class ModeSelector(ChooserScreen):
    """Pick a reasoning depth, with the cost/latency trade-off stated."""

    MODES: tuple[tuple[str, str], ...] = (
        ("default", "fastest and cheapest — everyday work"),
        ("medium", "some deliberation before acting"),
        ("high", "plans carefully; slower and dearer"),
        ("super", "deep reasoning for difficult problems"),
        ("max", "maximum deliberation; expect long waits"),
        ("ultra", "everything enabled; use sparingly"),
    )

    def __init__(self, *, tokens: Tokens, current: str = "default") -> None:
        choices = [
            Choice(value=key, label=key, detail=detail, current=key == current) for key, detail in self.MODES
        ]
        super().__init__(
            "Reasoning mode",
            choices,
            tokens=tokens,
            caption="Higher modes spend more tokens and take longer.",
            placeholder="Filter modes…",
        )


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question. Used so quitting mid-run cannot silently lose work."""

    BINDINGS = [
        Binding("escape", "deny", "no", show=False),
        Binding("y", "confirm", "yes", show=False),
        Binding("n", "deny", "no", show=False),
        Binding("enter", "confirm", "yes", show=False, priority=True),
    ]

    DEFAULT_CSS = _MODAL_CSS + """
    ConfirmScreen > #confirm {
        width: 56; height: auto; background: $surface;
        border: round $primary 45%; padding: 1 2;
    }
    #confirm-title { text-style: bold; color: $text; height: 1; }
    #confirm-body { color: $text-muted; height: auto; margin: 1 0; }
    #confirm-footer { color: $text-muted; height: 1; }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.heading = title
        self.body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(self.heading, id="confirm-title")
            yield Static(self.body, id="confirm-body")
            yield Static("enter / y  confirm     esc / n  cancel", id="confirm-footer")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    """Keys and slash commands, generated from the registry so it cannot go stale."""

    BINDINGS = [
        Binding("escape", "close", "close", show=False),
        Binding("q", "close", "close", show=False),
        Binding("f1", "close", "close", show=False),
        # The body scrolls, so it must be reachable from the keyboard, not just a wheel.
        Binding("down", "scroll_body(1)", "down", show=False),
        Binding("up", "scroll_body(-1)", "up", show=False),
        Binding("pagedown", "scroll_body(10)", "page down", show=False),
        Binding("pageup", "scroll_body(-10)", "page up", show=False),
    ]

    DEFAULT_CSS = _MODAL_CSS + """
    HelpScreen > #help {
        width: 76; max-width: 92%; height: 86%;
        background: $surface; border: round $primary 45%; padding: 1 2;
    }
    #help-title { text-style: bold; color: $text; height: 1; }
    #help-body { height: auto; }
    #help-scroll {
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-background: $surface;
        scrollbar-color: $primary 30%;
    }
    #help-footer { color: $text-muted; height: 1; margin-top: 1; }
    """

    def __init__(self, registry: CommandRegistry, *, tokens: Tokens) -> None:
        super().__init__()
        self.registry = registry
        self.tokens = tokens

    def compose(self) -> ComposeResult:
        with Vertical(id="help"):
            yield Static("Keyboard & commands", id="help-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(self.render_body(), id="help-body")
            yield Static("↑↓ scroll    esc close", id="help-footer")

    def render_body(self) -> Text:
        tokens = self.tokens
        body = Text()

        body.append("\nSHORTCUTS\n", style=f"bold {tokens.muted}")
        rows = [
            ("ctrl+p", "command palette"),
            ("/", "slash-command suggestions"),
            ("f1", "this help"),
            ("f2", "switch model"),
            ("f3", "switch provider"),
            ("f4", "reasoning mode"),
            ("f6", "toggle light/dark"),
            ("ctrl+r", "re-check the provider"),
            ("ctrl+l", "clear the conversation"),
            ("ctrl+c", "cancel the running task"),
            ("ctrl+q", "quit"),
        ]
        for key, label in rows:
            body.append(f"  {key:<10}", style=tokens.accent)
            body.append(f"{label}\n", style=tokens.dim)

        body.append("\nSLASH COMMANDS\n", style=f"bold {tokens.muted}")
        body.append("  Type ", style=tokens.muted)
        body.append("/", style=f"bold {tokens.accent}")
        body.append(" in the composer to search them inline.\n\n", style=tokens.muted)
        for command in sorted(self.registry.all(), key=lambda item: item.slash):
            if not command.slash:
                continue
            body.append(f"  /{command.slash:<11}", style=tokens.text)
            body.append(f"{truncate(command.description or command.title, 44)}\n", style=tokens.muted)
        return body

    def action_scroll_body(self, amount: int) -> None:
        try:
            self.query_one("#help-scroll", VerticalScroll).scroll_relative(y=amount, animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_close(self) -> None:
        self.dismiss(None)
