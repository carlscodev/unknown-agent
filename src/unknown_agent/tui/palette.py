"""Command palette and the interactive switcher panels.

The old shell exposed its capabilities as function keys documented in a footer.  That
does not scale, and it is not discoverable: a user cannot find "switch to a cheaper
model" by pressing keys at random.

Everything is now a :class:`Command` in one registry, reachable three ways:

* the **palette** (``ctrl+p``) — fuzzy search over every command;
* a **slash command** typed into the composer (``/model``, ``/provider``);
* a **shortcut** for the handful used constantly.

The switcher panels (model, provider, mode) are modal screens that *return a value*
rather than mutating global state, so the shell decides what to do with a choice and
the panels stay testable in isolation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

__all__ = [
    "Command",
    "CommandRegistry",
    "CommandPalette",
    "ModelSelectorPanel",
    "ProviderSwitcher",
    "ModeSelectorPanel",
    "Choice",
    "fuzzy_score",
]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Command:
    """One thing the user can do, however they reach it."""

    key: str
    title: str
    description: str = ""
    group: str = "general"
    shortcut: str = ""
    #: Typed form, without the slash: ``/model`` → ``model``.
    slash: str = ""
    handler: Callable[[], Any] | None = None
    #: Commands that need a live provider are still *listed* when it is down — they
    #: just say so — because hiding controls during an outage is how users get stuck.
    needs_provider: bool = False

    @property
    def haystack(self) -> str:
        return f"{self.title} {self.description} {self.slash} {self.group}".lower()


def fuzzy_score(query: str, candidate: str) -> int:
    """Subsequence match with bonuses for prefixes and word starts.

    Small and dependency-free on purpose: the palette must feel instant, and a real
    fuzzy library is not worth a dependency for a list of a few dozen commands.
    Returns 0 for no match, higher is better.
    """

    query = query.strip().lower()
    candidate = candidate.lower()
    if not query:
        return 1
    if query in candidate:
        # Exact substring: strongly preferred, and prefixes most of all.
        return 1000 - candidate.index(query) * 2 + (200 if candidate.startswith(query) else 0)

    score = 0
    position = 0
    previous_matched = False
    for char in query:
        found = candidate.find(char, position)
        if found < 0:
            return 0
        score += 10
        if found == 0 or candidate[found - 1] in " -_/.":
            score += 15          # word start
        if previous_matched and found == position:
            score += 8           # contiguous run
        previous_matched = True
        position = found + 1
    return max(1, score)


class CommandRegistry:
    """Every command in the shell, searchable."""

    def __init__(self, commands: Iterable[Command] = ()) -> None:
        self._commands: list[Command] = list(commands)

    def add(self, command: Command) -> Command:
        self._commands.append(command)
        return command

    def extend(self, commands: Iterable[Command]) -> None:
        self._commands.extend(commands)

    def all(self) -> list[Command]:
        return list(self._commands)

    def by_slash(self, name: str) -> Command | None:
        name = name.lstrip("/").strip().lower()
        return next((item for item in self._commands if item.slash and item.slash.lower() == name), None)

    def by_key(self, key: str) -> Command | None:
        return next((item for item in self._commands if item.key == key), None)

    def search(self, query: str, *, limit: int = 40) -> list[Command]:
        if not query.strip():
            return self._commands[:limit]
        scored = [(fuzzy_score(query, item.haystack), item) for item in self._commands]
        hits = [(score, item) for score, item in scored if score > 0]
        hits.sort(key=lambda pair: (-pair[0], pair[1].title))
        return [item for _, item in hits[:limit]]


# --------------------------------------------------------------------------- #
# palette
# --------------------------------------------------------------------------- #
class CommandPalette(ModalScreen[Command | None]):
    """Fuzzy-searchable list of every command.  Returns the chosen one, or ``None``."""

    BINDINGS = [
        Binding("escape", "dismiss_palette", "close", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
    ]

    DEFAULT_CSS = """
    CommandPalette { align: center top; }
    CommandPalette > Vertical {
        width: 74; max-height: 24; margin-top: 4;
        border: round $accent; background: $panel; padding: 0 1;
    }
    CommandPalette Input { border: none; background: $panel; }
    CommandPalette ListView { height: auto; max-height: 18; background: $panel; }
    CommandPalette .palette-hint { color: $text-muted; padding: 0 1; }
    """

    def __init__(self, registry: CommandRegistry, *, placeholder: str = "type a command…") -> None:
        super().__init__()
        self.registry = registry
        self.placeholder = placeholder
        self._results: list[Command] = registry.all()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(placeholder=self.placeholder, id="palette-input")
            yield ListView(id="palette-results")
            yield Static("enter to run · esc to close", classes="palette-hint")

    def on_mount(self) -> None:
        self._populate(self.registry.all())
        self.query_one("#palette-input", Input).focus()

    def _populate(self, commands: Sequence[Command]) -> None:
        self._results = list(commands)
        listing = self.query_one("#palette-results", ListView)
        listing.clear()
        for command in self._results:
            line = Text()
            line.append(f"{command.title:<34}", style="bold")
            if command.shortcut:
                line.append(f"{command.shortcut:<10}", style="dim")
            if command.description:
                line.append(command.description, style="dim")
            listing.append(ListItem(Label(line)))
        if self._results:
            listing.index = 0

    @on(Input.Changed, "#palette-input")
    def _filter(self, event: Input.Changed) -> None:
        self._populate(self.registry.search(event.value))

    @on(Input.Submitted, "#palette-input")
    def _submit(self) -> None:
        self._choose(self.query_one("#palette-results", ListView).index or 0)

    @on(ListView.Selected, "#palette-results")
    def _selected(self, event: ListView.Selected) -> None:
        self._choose(event.list_view.index or 0)

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._results):
            self.dismiss(self._results[index])
        else:
            self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#palette-results", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#palette-results", ListView).action_cursor_up()

    def action_dismiss_palette(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- #
# generic chooser
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Choice:
    """One selectable row in a switcher panel."""

    value: str
    label: str
    detail: str = ""
    badge: str = ""
    current: bool = False


class _ChooserScreen(ModalScreen[str | None]):
    """A searchable list that returns the chosen value.

    Shared by the model, provider and mode panels so all three behave identically:
    type to filter, arrows to move, enter to pick, escape to cancel.
    """

    BINDINGS = [
        Binding("escape", "dismiss_chooser", "cancel", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
    ]

    DEFAULT_CSS = """
    _ChooserScreen { align: center middle; }
    _ChooserScreen > Vertical {
        width: 80; max-height: 28; border: round $accent; background: $panel; padding: 0 1;
    }
    _ChooserScreen .chooser-title { text-style: bold; padding: 0 1; }
    _ChooserScreen .chooser-hint { color: $text-muted; padding: 0 1; }
    _ChooserScreen Input { border: none; background: $panel; }
    _ChooserScreen ListView { height: auto; max-height: 20; background: $panel; }
    """

    def __init__(self, title: str, choices: Sequence[Choice], *, hint: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.choices = list(choices)
        self.hint = hint or "enter to select · esc to cancel"
        self._visible: list[Choice] = list(choices)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.title_text, classes="chooser-title")
            yield Input(placeholder="filter…", id="chooser-input")
            yield ListView(id="chooser-results")
            yield Static(self.hint, classes="chooser-hint")

    def on_mount(self) -> None:
        self._populate(self.choices)
        self.query_one("#chooser-input", Input).focus()

    def _populate(self, choices: Sequence[Choice]) -> None:
        self._visible = list(choices)
        listing = self.query_one("#chooser-results", ListView)
        listing.clear()
        for choice in self._visible:
            line = Text()
            line.append("● " if choice.current else "  ", style="bold green" if choice.current else "dim")
            line.append(f"{choice.label:<34}", style="bold" if choice.current else "")
            if choice.badge:
                line.append(f"{choice.badge:<14}", style="cyan")
            if choice.detail:
                line.append(choice.detail, style="dim")
            listing.append(ListItem(Label(line)))
        current = next((index for index, item in enumerate(self._visible) if item.current), 0)
        if self._visible:
            listing.index = current

    @on(Input.Changed, "#chooser-input")
    def _filter(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if not query:
            self._populate(self.choices)
            return
        scored = [(fuzzy_score(query, f"{item.label} {item.detail} {item.value}"), item) for item in self.choices]
        hits = sorted(((score, item) for score, item in scored if score > 0), key=lambda pair: -pair[0])
        self._populate([item for _, item in hits])

    @on(Input.Submitted, "#chooser-input")
    def _submit(self) -> None:
        self._choose(self.query_one("#chooser-results", ListView).index or 0)

    @on(ListView.Selected, "#chooser-results")
    def _selected(self, event: ListView.Selected) -> None:
        self._choose(event.list_view.index or 0)

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._visible):
            self.dismiss(self._visible[index].value)
        else:
            self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one("#chooser-results", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#chooser-results", ListView).action_cursor_up()

    def action_dismiss_chooser(self) -> None:
        self.dismiss(None)


class ModelSelectorPanel(_ChooserScreen):
    """Pick a model, with capability badges so the choice is informed."""

    def __init__(self, models: Sequence[Any], *, current: str = "", source: str = "catalog") -> None:
        choices = []
        for item in models:
            model_id = getattr(item, "id", str(item))
            badges = []
            if getattr(item, "supports_tools", None):
                badges.append("tools")
            if getattr(item, "supports_vision", False):
                badges.append("vision")
            if getattr(item, "supports_reasoning", False):
                badges.append("reasoning")
            window = getattr(item, "context_window", 0) or 0
            detail = f"{window // 1000}k ctx" if window else ""
            choices.append(
                Choice(value=model_id, label=model_id, detail=detail, badge=",".join(badges), current=model_id == current)
            )
        super().__init__(
            f"select a model  ({len(choices)} from the {source} list)",
            choices,
            hint="enter to switch · esc to keep the current model",
        )


class ProviderSwitcher(_ChooserScreen):
    """Pick a configured profile, or jump to setup for a new one."""

    def __init__(self, profiles: Sequence[Any], *, current: str = "") -> None:
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
        choices.append(Choice(value="__setup__", label="+ configure a new provider", detail="opens the setup panel"))
        super().__init__("switch provider", choices, hint="enter to switch · esc to cancel")


class ModeSelectorPanel(_ChooserScreen):
    """Pick a reasoning depth, with its cost/latency implication spelled out."""

    #: Depth → what it actually costs you.  Users choose badly without this.
    MODES: tuple[tuple[str, str], ...] = (
        ("default", "fastest, cheapest — everyday edits"),
        ("medium", "some deliberation before acting"),
        ("high", "plans carefully; noticeably slower and dearer"),
        ("super", "deep reasoning for gnarly bugs"),
        ("max", "maximum deliberation; expect long waits"),
        ("ultra", "everything on; use sparingly"),
    )

    def __init__(self, *, current: str = "default") -> None:
        choices = [
            Choice(value=key, label=key, detail=detail, current=key == current) for key, detail in self.MODES
        ]
        super().__init__("reasoning mode", choices, hint="higher modes cost more tokens · esc to cancel")
