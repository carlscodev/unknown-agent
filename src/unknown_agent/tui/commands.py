"""Command model, inline slash suggestions, and the command palette.

Three surfaces, one registry
----------------------------
A command is declared once and becomes reachable three ways — a shortcut, a slash
command typed in the composer, and the palette. They cannot drift apart because they
all read the same :class:`CommandRegistry`, and the footer/help text is generated from
it rather than hand-maintained.

The important piece here is :class:`SlashSuggestions`. Typing ``/`` previously did
nothing at all: the composer only parsed the text *after* submission, so there was no
discovery whatsoever. This implements the real interaction — an inline popup anchored
above the composer that appears on ``/``, filters as you type, is navigable with the
arrow keys, completes on Tab and runs on Enter.

Safety
------
Every command declares whether it is available, and the UI never offers what it
cannot do. :meth:`Command.availability` returns a reason when a command is disabled,
which is rendered inline — so a user learns *why* something is unavailable instead of
pressing a key and getting a traceback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from unknown_agent.tui.design import GLYPH, Tokens, truncate

__all__ = [
    "Command",
    "CommandRegistry",
    "SlashSuggestions",
    "fuzzy_score",
    "build_default_commands",
]


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Command:
    """One user-invocable action.

    ``handler`` may be sync or async; the shell awaits it if needed. ``enabled`` is a
    predicate rather than a flag so availability is evaluated at display time and can
    depend on live state (a provider being configured, a run being in flight).
    """

    key: str
    title: str
    description: str = ""
    group: str = "general"
    shortcut: str = ""
    slash: str = ""
    handler: Callable[[], Any] | None = None
    #: Return ``""`` when runnable, or a short reason why not.
    enabled: Callable[[], str] = lambda: ""
    #: Hidden commands stay invokable but are not advertised.
    hidden: bool = False

    def availability(self) -> str:
        """Why this command cannot run right now, or ``""`` if it can."""

        try:
            return self.enabled() or ""
        except Exception:  # noqa: BLE001 - a broken predicate must not hide the command
            return ""

    @property
    def available(self) -> bool:
        return not self.availability()

    @property
    def haystack(self) -> str:
        return f"{self.slash} {self.title} {self.description} {self.group}".lower()


def fuzzy_score(query: str, candidate: str) -> int:
    """Subsequence matching with prefix and word-boundary bonuses.

    Deliberately tiny and dependency-free — the palette must feel instantaneous, and
    a general-purpose fuzzy library is not worth a dependency for a few dozen entries.
    Returns 0 for no match; higher is better.
    """

    query = query.strip().lower()
    candidate = candidate.lower()
    if not query:
        return 1
    if query in candidate:
        index = candidate.index(query)
        return 1000 - index * 2 + (250 if index == 0 else 0)

    score = 0
    position = 0
    previous_end = -1
    for char in query:
        found = candidate.find(char, position)
        if found < 0:
            return 0
        score += 10
        if found == 0 or candidate[found - 1] in " -_/.":
            score += 18
        if found == previous_end:
            score += 10
        previous_end = found + 1
        position = found + 1
    return max(1, score)


class CommandRegistry:
    """The single source of truth for what the shell can do."""

    def __init__(self, commands: Iterable[Command] = ()) -> None:
        self._commands: list[Command] = list(commands)

    def add(self, command: Command) -> Command:
        self._commands.append(command)
        return command

    def extend(self, commands: Iterable[Command]) -> None:
        self._commands.extend(commands)

    def all(self, *, include_hidden: bool = False) -> list[Command]:
        return [item for item in self._commands if include_hidden or not item.hidden]

    def by_key(self, key: str) -> Command | None:
        return next((item for item in self._commands if item.key == key), None)

    def by_slash(self, name: str) -> Command | None:
        name = name.lstrip("/").strip().lower()
        if not name:
            return None
        return next((item for item in self._commands if item.slash and item.slash.lower() == name), None)

    def search(self, query: str, *, limit: int = 50) -> list[Command]:
        """Rank commands against a query; available ones always sort above disabled."""

        commands = self.all()
        if not query.strip():
            return sorted(commands, key=lambda item: (not item.available, item.group, item.title))[:limit]
        scored = ((fuzzy_score(query, item.haystack), item) for item in commands)
        hits = [(score, item) for score, item in scored if score > 0]
        hits.sort(key=lambda pair: (not pair[1].available, -pair[0], pair[1].title))
        return [item for _, item in hits[:limit]]

    def slash_matches(self, fragment: str, *, limit: int = 8) -> list[Command]:
        """Commands matching a ``/fragment`` being typed, best first."""

        fragment = fragment.lstrip("/").strip().lower()
        candidates = [item for item in self.all() if item.slash]
        if not fragment:
            return sorted(candidates, key=lambda item: (not item.available, item.title))[:limit]
        scored = []
        for item in candidates:
            slash = item.slash.lower()
            if slash.startswith(fragment):
                score = 1000 - len(slash)          # prefix wins outright
            elif fragment in slash:
                score = 600
            else:
                score = fuzzy_score(fragment, f"{slash} {item.title.lower()}")
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (not pair[1].available, -pair[0], pair[1].title))
        return [item for _, item in scored[:limit]]


# --------------------------------------------------------------------------- #
# inline slash suggestions
# --------------------------------------------------------------------------- #
class SlashSuggestions(Static):
    """Inline command suggestions, anchored above the composer.

    Shown the moment ``/`` is typed at the start of the composer and refreshed on every
    keystroke. Purely presentational: it owns a selection index and emits messages, and
    the shell decides what a selection means. That keeps it testable on its own and
    keeps command execution in exactly one place.
    """

    class Selected(Message):
        """The user chose a command from the popup."""

        def __init__(self, command: Command) -> None:
            super().__init__()
            self.command = command

    class Completed(Message):
        """The user asked to complete the text (Tab) without running it."""

        def __init__(self, command: Command) -> None:
            super().__init__()
            self.command = command

    DEFAULT_CSS = """
    SlashSuggestions {
        display: none;
        height: auto;
        min-height: 0;
        max-height: 11;
        margin: 0 2;
        padding: 1 1 0 1;
        background: $panel;
        border: round $primary 35%;
    }
    SlashSuggestions.visible { display: block; }
    """

    def __init__(self, tokens: Tokens, **kwargs: Any) -> None:
        # Seed with a real (empty) Text, never "": Static resolves an empty string to a
        # null visual, and Textual then cannot compute a height for an ``height: auto``
        # widget -- it raises during layout even while the widget is hidden.
        super().__init__(**kwargs)
        self.tokens = tokens
        self._matches: list[Command] = []
        self._index = 0

    # -- state ------------------------------------------------------------- #
    @property
    def matches(self) -> list[Command]:
        return list(self._matches)

    @property
    def index(self) -> int:
        return self._index

    @property
    def is_open(self) -> bool:
        return self.has_class("visible")

    @property
    def current(self) -> Command | None:
        if 0 <= self._index < len(self._matches):
            return self._matches[self._index]
        return None

    def show(self, matches: Sequence[Command]) -> None:
        """Display ``matches``, keeping the selection in range."""

        self._matches = list(matches)
        self._index = min(self._index, max(0, len(self._matches) - 1))
        if not self._matches:
            self.close()
            return
        self.add_class("visible")
        self.refresh(layout=True)

    def close(self) -> None:
        self._matches = []
        self._index = 0
        self.remove_class("visible")
        self.refresh(layout=True)

    def move(self, delta: int) -> None:
        """Move the selection, wrapping — arrow keys should never dead-end."""

        if not self._matches:
            return
        self._index = (self._index + delta) % len(self._matches)
        self.refresh()

    def choose(self) -> Command | None:
        command = self.current
        if command is not None:
            self.post_message(self.Selected(command))
        return command

    def complete(self) -> Command | None:
        command = self.current
        if command is not None:
            self.post_message(self.Completed(command))
        return command

    # -- rendering ---------------------------------------------------------- #
    def render(self) -> Text:
        """Always return a real renderable.

        Implemented as ``render()`` rather than pushing content through ``update()``:
        Static caches a visual, and a blank update can leave that cache holding
        ``None``, which makes Textual fail to measure an ``height: auto`` widget.
        """

        return self.render_matches()

    def render_matches(self) -> Text:
        """Build the popup body. Separate from ``update`` so tests can read it."""

        tokens = self.tokens
        body = Text()
        for position, command in enumerate(self._matches):
            selected = position == self._index
            disabled_reason = command.availability()

            if selected:
                body.append(f" {GLYPH['chevron']} ", style=f"bold {tokens.accent}")
            else:
                body.append("   ")

            name_style = tokens.faint if disabled_reason else (f"bold {tokens.text}" if selected else tokens.dim)
            body.append(f"/{command.slash:<11}", style=name_style)

            # Pad the description to a fixed width so the shortcut column lines up
            # down the list instead of stepping in and out with each description.
            detail = disabled_reason or command.description or command.title
            detail_style = tokens.faint if disabled_reason else (tokens.dim if selected else tokens.muted)
            body.append(f"{truncate(detail, 46):<48}", style=detail_style)

            if command.shortcut and not disabled_reason:
                body.append(command.shortcut, style=tokens.faint)
            body.append("\n")

        # A rule separates the results from the key hints, so the hints do not read
        # as another (unselectable) command row.
        hint = Text()
        hint.append(f"{GLYPH['rule'] * 62}\n", style=tokens.border)
        hint.append("↑↓", style=tokens.muted)
        hint.append(" move   ", style=tokens.faint)
        hint.append("tab", style=tokens.muted)
        hint.append(" complete   ", style=tokens.faint)
        hint.append("enter", style=tokens.muted)
        hint.append(" run   ", style=tokens.faint)
        hint.append("esc", style=tokens.muted)
        hint.append(" dismiss", style=tokens.faint)
        body.append_text(hint)
        return body


# --------------------------------------------------------------------------- #
# default command set
# --------------------------------------------------------------------------- #
def build_default_commands(shell: Any) -> list[Command]:
    """The shell's commands, bound to ``shell``'s actions.

    Declared in one place so the palette, the slash popup, the footer and the help
    screen cannot disagree about what exists or what a key does.
    """

    def needs_provider() -> str:
        state = getattr(shell, "provider_status", None)
        if state is None:
            return ""
        return "" if getattr(state.state, "provider_label", "") or getattr(state.state, "provider", "") not in ("", "—") else "no provider configured yet"

    def not_while_busy() -> str:
        return "a task is running" if getattr(shell, "busy", False) else ""

    return [
        Command("model", "Switch model", "choose a model from this provider", "provider", "f2", "model",
                handler=shell.action_switch_model, enabled=needs_provider),
        Command("provider", "Switch provider", "change the active provider profile", "provider", "f3", "provider",
                handler=shell.action_switch_provider),
        Command("mode", "Reasoning mode", "trade speed against deliberation", "agent", "f4", "mode",
                handler=shell.action_switch_mode),
        Command("setup", "Provider setup", "configure a provider and its authentication", "provider", "", "setup",
                handler=shell.action_run_setup, enabled=not_while_busy),
        Command("retry", "Re-check provider", "run the health check again", "provider", "ctrl+r", "retry",
                handler=shell.action_retry_provider),
        Command("status", "Provider status", "full health report with every check", "provider", "", "status",
                handler=shell.action_show_status),
        Command("usage", "Session usage", "tokens and cost for this session", "session", "", "usage",
                handler=shell.action_show_usage),
        Command("theme", "Toggle light/dark", "switch between the light and dark theme", "view", "f6", "theme",
                handler=shell.action_toggle_theme),
        Command("clear", "Clear conversation", "empty the transcript", "session", "", "clear",
                handler=shell.action_clear),
        Command("help", "Keyboard shortcuts", "keys and slash commands", "general", "f1", "help",
                handler=shell.action_show_help),
        Command("quit", "Quit", "leave the shell", "general", "ctrl+q", "quit",
                handler=shell.action_request_quit),
    ]
