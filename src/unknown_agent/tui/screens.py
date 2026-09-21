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

__all__ = ["Choice", "ChooserScreen", "CommandPalette", "ModelSelector", "ProviderSelector", "ModeSelector", "HelpScreen", "ConfirmScreen", "SessionHistoryScreen", "SessionDetailScreen"]


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
        if source == "live":
            caption = f"{len(choices)} models from the live provider list"
        elif source == "manual":
            caption = "No catalog was available — the current model is shown; type any model id to switch"
        else:
            caption = f"{len(choices)} models from the bundled catalog — the live list was unavailable"
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
            ("f7", "session history / resume"),
            ("ctrl+o", "session history"),
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


# --------------------------------------------------------------------------- #
# session history — polished list + resume
# --------------------------------------------------------------------------- #
class SessionHistoryScreen(ModalScreen[str | None]):
    """Polished recent-sessions list.  Returns the chosen session ``id`` or ``None``."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=False),
        Binding("down", "move_down", "down", show=False, priority=True),
        Binding("up", "move_up", "up", show=False, priority=True),
        Binding("enter", "accept", "resume", show=False, priority=True),
        Binding("d", "delete", "delete", show=False),
        Binding("n", "new_session", "new", show=False),
    ]

    DEFAULT_CSS = _MODAL_CSS + """
    SessionHistoryScreen { align: center middle; background: $background 60%; }
    SessionHistoryScreen > #session-chooser {
        width: 96; max-width: 96%; height: 88%; max-height: 88%;
        background: $surface; border: round $primary 45%; padding: 1 2;
    }
    #session-title { color: $text; text-style: bold; height: 1; }
    #session-caption { color: $text-muted; height: auto; margin-bottom: 1; }
    #session-meta { color: $text-muted; height: 1; margin-bottom: 1; }
    #session-input {
        border: round $primary 25%; background: $boost; height: 3; padding: 0 1; margin-bottom: 1; color: $text;
    }
    #session-input:focus { border: round $primary 60%; }
    #session-list {
        height: 1fr; background: transparent; border: none; padding: 0; scrollbar-size: 1 1;
    }
    #session-list > ListItem { background: transparent; padding: 0 1; height: auto; border-left: blank; }
    #session-list > ListItem.-highlight,
    #session-list:focus > ListItem.-highlight { background: $accent 15%; border-left: thick $accent; }
    #session-empty { display: none; color: $text-muted; padding: 1; height: auto; }
    #session-empty.visible { display: block; }
    #session-footer { color: $text-muted; height: auto; margin-top: 1; }
    #session-hint { color: $text-muted; height: 1; }
    """

    def __init__(self, sessions: Sequence[Any], *, tokens: Tokens, current_id: str = "") -> None:
        super().__init__()
        self.tokens = tokens
        self.sessions = list(sessions)
        self.current_id = current_id
        self._visible: list[Any] = list(sessions)
        # Map for quick lookup
        self._by_id = {getattr(s, "id", str(s)): s for s in self.sessions}

    def compose(self) -> ComposeResult:
        with Vertical(id="session-chooser"):
            yield Static("Sessions", id="session-title")
            caption = ""
            if self.sessions:
                caption = f"{len(self.sessions)} recent sessions — current highlighted · type to filter"
                if self.current_id:
                    caption += f" · current: {self.current_id[:8]}"
            else:
                caption = "No sessions yet — your conversations will appear here"
            yield Static(caption, id="session-caption")
            # Show current session summary if present
            current = self._by_id.get(self.current_id) if self.current_id else None
            if current is not None:
                info = Text()
                info.append("current: ", style=self.tokens.faint)
                info.append(getattr(current, "name", "")[:50] or getattr(current, "title", "")[:50], style=f"bold {self.tokens.text}")
                info.append(f"  ·  {getattr(current, 'short_id', current.id[:8])}", style=self.tokens.muted)
                info.append(f"  ·  {getattr(current, 'provider', '')}/{getattr(current, 'model', '')}", style=self.tokens.dim)
                yield Static(info, id="session-meta")
            yield Input(placeholder="Filter by name, id, date, provider…", id="session-input")
            yield ListView(id="session-list")
            yield Static("", id="session-empty")
            yield Static(self._footer_text(), id="session-footer")
            yield Static("", id="session-hint")

    def _footer_text(self) -> str:
        return "↑↓ move    enter resume    d delete    n new session    esc close"

    def on_mount(self) -> None:
        self._populate(self.sessions)
        self.query_one("#session-input", Input).focus()
        self._update_hint()

    def _populate(self, sessions: Sequence[Any]) -> None:
        self._visible = list(sessions)
        listing = self.query_one("#session-list", ListView)
        listing.clear()
        for rec in self._visible:
            is_current = getattr(rec, "id", "") == self.current_id
            listing.append(ListItem(Label(self._render_session(rec, is_current=is_current))))
        empty = self.query_one("#session-empty", Static)
        if self._visible:
            empty.remove_class("visible")
            listing.display = True
            # Focus current if present
            idx = next((i for i, s in enumerate(self._visible) if getattr(s, "id", "") == self.current_id), 0)
            listing.index = idx
        else:
            listing.display = False
            empty.add_class("visible")
            empty.update("No sessions match that filter.")

    def _render_session(self, rec: Any, *, is_current: bool = False) -> Text:
        t = self.tokens
        text = Text()
        # Marker
        if is_current:
            text.append(f"{GLYPH['ready']} ", style=t.accent)
        else:
            # status dot
            status = getattr(rec, "status", "completed")
            if status == "active":
                text.append("● ", style=t.ok)
            elif status == "interrupted":
                text.append("◐ ", style=t.warn)
            else:
                text.append("○ ", style=t.faint)

        # Name — bold if current
        name = getattr(rec, "name", "") or getattr(rec, "title", "") or f"Session {getattr(rec, 'id', '')[:8]}"
        name = truncate(name, 32)
        style = f"bold {t.text}" if is_current else t.text
        text.append(f"{name:<32}", style=style)

        # Short ID
        sid = getattr(rec, "short_id", getattr(rec, "id", "")[:8])
        text.append(f"{sid:<9}", style=t.accent_dim if is_current else t.faint)

        # Date + time
        date = getattr(rec, "date_str", "") or time.strftime("%Y-%m-%d", time.localtime(getattr(rec, "created_at", 0)))
        tm = getattr(rec, "time_str", "") or time.strftime("%H:%M", time.localtime(getattr(rec, "created_at", 0)))
        try:
            import time as _time
            date = getattr(rec, "date_str", _time.strftime("%Y-%m-%d", _time.localtime(getattr(rec, "created_at", 0))))
            tm = getattr(rec, "time_str", _time.strftime("%H:%M:%S", _time.localtime(getattr(rec, "created_at", 0))))
        except Exception:
            pass
        text.append(f"{date} ", style=t.muted)
        text.append(f"{tm:<8}", style=t.muted)

        # Provider/model
        provider = getattr(rec, "provider", "") or "—"
        model = getattr(rec, "model", "") or "—"
        pm = f"{truncate(provider, 10)}/{truncate(model, 12)}" if provider != "—" else truncate(model, 18)
        text.append(f"{truncate(pm, 20):<20}", style=t.dim)

        # Turns + messages
        turns = getattr(rec, "turns", 0)
        msgs = getattr(rec, "message_count", 0)
        if turns or msgs:
            text.append(f" {turns}τ {msgs}✉", style=t.faint)
        if is_current:
            text.append("  CURRENT", style=f"bold {t.accent}")
        return text

    def _update_hint(self) -> None:
        try:
            hint = self.query_one("#session-hint", Static)
            idx = self.query_one("#session-list", ListView).index or 0
            if 0 <= idx < len(self._visible):
                rec = self._visible[idx]
                detail = Text()
                detail.append("resume: ", style=self.tokens.faint)
                detail.append(f"ua tui --resume {getattr(rec, 'id', '')[:8]}", style=self.tokens.accent)
                detail.append("  ·  ", style=self.tokens.faint)
                cwd = getattr(rec, "cwd", "")
                if cwd:
                    # Show only last two path components for polish
                    parts = cwd.rsplit("/", 2)
                    short_cwd = "/".join(parts[-2:]) if len(parts) >= 2 else cwd
                    detail.append(truncate(short_cwd, 36), style=self.tokens.muted)
                    detail.append("  ", style=self.tokens.faint)
                last = getattr(rec, "last_task", "") or getattr(rec, "summary", "")
                if last:
                    detail.append(truncate(last, 44), style=self.tokens.muted)
                hint.update(detail)
            else:
                hint.update(Text(""))
        except Exception:
            pass

    def _filter(self, query: str) -> None:
        query = query.strip().lower()
        if not query:
            self._populate(self.sessions)
            self._update_hint()
            return
        from unknown_agent.tui.commands import fuzzy_score as _fuzzy
        scored = []
        for rec in self.sessions:
            hay = " ".join(
                [
                    getattr(rec, "name", ""),
                    getattr(rec, "title", ""),
                    getattr(rec, "id", ""),
                    getattr(rec, "short_id", ""),
                    getattr(rec, "provider", ""),
                    getattr(rec, "model", ""),
                    getattr(rec, "last_task", ""),
                    getattr(rec, "date_str", ""),
                    getattr(rec, "cwd", ""),
                ]
            ).lower()
            # Prefer direct substring for dates/ids, fuzzy for names
            if query in hay:
                score = 1000 - hay.index(query)
            else:
                score = _fuzzy(query, hay)
            if score > 0:
                scored.append((score, rec))
        scored.sort(key=lambda p: -p[0])
        self._populate([r for _, r in scored])
        self._update_hint()

    @on(Input.Changed, "#session-input")
    def _on_filter(self, event: Input.Changed) -> None:
        self._filter(event.value)

    @on(ListView.Selected, "#session-list")
    def _on_click(self, event: ListView.Selected) -> None:
        self._accept_index(event.list_view.index or 0)

    def action_move_down(self) -> None:
        self._move(1)

    def action_move_up(self) -> None:
        self._move(-1)

    def _move(self, delta: int) -> None:
        listing = self.query_one("#session-list", ListView)
        if not self._visible:
            return
        listing.index = ((listing.index or 0) + delta) % len(self._visible)
        self._update_hint()

    def action_accept(self) -> None:
        self._accept_index(self.query_one("#session-list", ListView).index or 0)

    def _accept_index(self, index: int) -> None:
        if 0 <= index < len(self._visible):
            rec = self._visible[index]
            self.dismiss(getattr(rec, "id", ""))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_delete(self) -> None:
        # Deletion is handled by the shell — we just signal intent via a special value
        # that the shell interprets.  For now, ignore inside the modal and let shell handle.
        listing = self.query_one("#session-list", ListView)
        idx = listing.index or 0
        if 0 <= idx < len(self._visible):
            rec = self._visible[idx]
            # Store for shell to handle — dismiss with a prefix
            self.dismiss(f"__delete__{getattr(rec, 'id', '')}")

    def action_new_session(self) -> None:
        self.dismiss("__new__")


class SessionDetailScreen(ModalScreen[None]):
    """Read-only metadata view for a single session."""

    DEFAULT_CSS = _MODAL_CSS + """
    SessionDetailScreen { align: center middle; background: $background 60%; }
    SessionDetailScreen > #detail { width: 76; max-width: 92%; height: auto; max-height: 84%; background: $surface; border: round $primary 45%; padding: 1 2; }
    #detail-title { color: $text; text-style: bold; height: 1; }
    #detail-body { color: $text-muted; height: auto; margin: 1 0; }
    #detail-footer { color: $text-muted; height: 1; }
    """

    def __init__(self, record: Any, *, tokens: Tokens) -> None:
        super().__init__()
        self.record = record
        self.tokens = tokens

    def compose(self) -> ComposeResult:
        with Vertical(id="detail"):
            yield Static(f"Session {getattr(self.record, 'short_id', getattr(self.record, 'id', '')[:8])}", id="detail-title")
            yield Static(self._body(), id="detail-body")
            yield Static("esc close", id="detail-footer")

    def _body(self) -> Text:
        r = self.record
        t = self.tokens
        body = Text()
        def row(label: str, value: str) -> None:
            body.append(f"{label:<14}", style=t.muted)
            body.append(value + "\n", style=t.text)
        row("name", getattr(r, "name", "") or getattr(r, "title", ""))
        row("id", getattr(r, "id", ""))
        row("date", getattr(r, "datetime_str", "") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(getattr(r, "created_at", 0))))
        row("updated", getattr(r, "updated_str", "") or time.strftime("%Y-%m-%d %H:%M", time.localtime(getattr(r, "updated_at", 0))))
        row("status", getattr(r, "status", ""))
        row("cwd", getattr(r, "cwd", "") or "—")
        row("provider", f"{getattr(r, 'provider', '') or '—'} / {getattr(r, 'model', '') or '—'}")
        row("turns", str(getattr(r, "turns", 0)))
        row("messages", str(getattr(r, "message_count", 0)))
        dur = getattr(r, "duration_s", 0)
        try:
            mins, secs = divmod(int(dur), 60)
            row("duration", f"{mins}m {secs}s" if mins else f"{secs}s")
        except Exception:
            row("duration", str(dur))
        row("resumes", str(getattr(r, "resume_count", 0)))
        if getattr(r, "last_task", ""):
            body.append("\nlast task:\n", style=f"bold {t.muted}")
            body.append(truncate(getattr(r, "last_task", ""), 400) + "\n", style=t.dim)
        body.append("\nresume:\n", style=f"bold {t.muted}")
        body.append(f"  ua tui --resume {getattr(r, 'id', '')[:8]}\n", style=t.accent)
        body.append(f"  ua sessions resume {getattr(r, 'id', '')[:8]}\n", style=t.accent_dim)
        return body

    BINDINGS = [Binding("escape", "close", "close", show=False), Binding("q", "close", "close", show=False)]

    def action_close(self) -> None:
        self.dismiss(None)
