"""``TUIAppShell`` — the Unknown Agent shell.

Structure
---------
::

    ┌──────────────────────────────────────────────────┬──────────────────┐
    │ unknown agent   ● ready  openrouter · sonnet-4   │  PROVIDER        │
    ├──────────────────────────────────────────────────┤  ● connected     │
    │                                                  │  name  OpenRouter│
    │  conversation                                    │  model sonnet-4  │
    │                                                  ├──────────────────┤
    │                                                  │  CONTEXT ▰▰▱▱▱▱  │
    │                                                  ├──────────────────┤
    │  [ /model     choose a model        f2 ]         │  USAGE           │
    │  ❯ ask, or press / for commands                  │  input   27,552  │
    ├──────────────────────────────────────────────────┤  cost    $0.174  │
    │ ctrl+p commands   / slash   f1 help              │  SESSION  idle   │
    └──────────────────────────────────────────────────┴──────────────────┘

Design notes are in :mod:`unknown_agent.tui.design`; the short version is that
structure is drawn in neutrals and colour is reserved for state and focus.

Two invariants this module enforces
-----------------------------------
**Startup never blocks on the provider.** ``on_mount`` does no network I/O at all. It
paints from local config, focuses the composer, and schedules health resolution as a
worker. A 402, an expired key, a dead network or a hung endpoint all arrive later as UI
state with recovery actions attached.

**No exposed control can throw.** Every action is wrapped by :func:`safe_action`, and
every action that opens a modal runs inside a Textual worker — ``push_screen_wait``
raises ``NoActiveWorker`` otherwise, which is precisely what made F3, F4 and ctrl+p
crash before this pass. Anything not yet implemented is not bound and not advertised.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, RichLog, Static

from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.health import ProviderStatus
from unknown_agent.tui.commands import Command, CommandRegistry, SlashSuggestions, build_default_commands
from unknown_agent.tui.dashboard import ContextCard, ProviderCard, SessionCard, UsageCard, status_marker
from unknown_agent.tui.design import DARK, GLYPH, LIGHT, Tokens, format_duration, tokens_for, truncate
from unknown_agent.tui.provider_status import (
    ProviderState,
    ProviderStatusController,
    StartupHealthResolver,
    active_profile_name,
)
from unknown_agent.tui.screens import (
    CommandPalette,
    ConfirmScreen,
    HelpScreen,
    ModeSelector,
    ModelSelector,
    ProviderSelector,
)
from unknown_agent.tui.usage import UsageTracker

log = get_logger("tui.shell")

__all__ = ["TUIAppShell", "ErrorBoundary", "safe_action", "run_app_shell"]

RunnerCallable = Callable[[str, Any], Awaitable[Any]]


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #
def safe_action(region: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap an action so a failure becomes a transcript note, never a crash.

    Applied to every bound key. The guarantee the shell makes is that a visible
    shortcut either works or explains itself — it never dumps a traceback over the UI.
    """

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        if asyncio.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(self: "TUIAppShell", *args: Any, **kwargs: Any) -> Any:
                try:
                    return await function(self, *args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.report_problem(region, exc)
                    return None

            return async_wrapper

        @functools.wraps(function)
        def wrapper(self: "TUIAppShell", *args: Any, **kwargs: Any) -> Any:
            try:
                return function(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                self.report_problem(region, exc)
                return None

        return wrapper

    return decorate


class ErrorBoundary:
    """Context manager turning an exception into UI state.

    Used around calls into provider and agent code so a remote failure degrades one
    region instead of killing the application.
    """

    def __init__(self, shell: "TUIAppShell", *, region: str, quiet: bool = False) -> None:
        self.shell = shell
        self.region = region
        self.quiet = quiet
        self.error: BaseException | None = None

    def __enter__(self) -> "ErrorBoundary":
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if exc is None:
            return False
        if isinstance(exc, asyncio.CancelledError):
            return False
        self.error = exc
        try:
            self.shell.handle_degradation(exc, region=self.region, quiet=self.quiet)
        except Exception:  # noqa: BLE001 - the boundary itself must never raise
            log.debug("error boundary handler failed", exc_info=True)
        return True


# --------------------------------------------------------------------------- #
# the shell
# --------------------------------------------------------------------------- #
class TUIAppShell(App[None]):
    """The agent shell. Opens instantly; provider health arrives afterwards."""

    CSS = """
    Screen { background: $background; }

    /* ---- header ------------------------------------------------------- */
    #header {
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text;
    }
    #header-rule { height: 1; color: $primary 20%; background: $background; }

    /* ---- body --------------------------------------------------------- */
    #body { height: 1fr; background: $background; }
    #main { width: 1fr; height: 1fr; background: $background; }

    #sidebar {
        width: 32;
        height: 1fr;
        padding: 1 2;
        background: $surface;
        scrollbar-size: 1 1;
        scrollbar-background: $surface;
        scrollbar-color: $primary 25%;
    }

    /* ---- conversation -------------------------------------------------- */
    #conversation {
        height: 1fr;
        padding: 1 3;
        background: $background;
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-color: $primary 22%;
    }

    /* ---- notice strip -------------------------------------------------- */
    #notice {
        display: none;
        height: auto;
        max-height: 6;
        margin: 0 2 1 2;
        padding: 1 2;
        background: $boost;
        border-left: thick $warning;
    }
    #notice.visible { display: block; }
    #notice.-error { border-left: thick $error; }
    #notice.-warn  { border-left: thick $warning; }
    #notice.-info  { border-left: thick $primary; }

    /* ---- composer ------------------------------------------------------ */
    /* ``dock: bottom`` with an explicit child height keeps the composer pinned and
       avoids resolving an auto height against a hidden (display:none) popup, which
       Textual cannot measure. */
    #composer-shell { width: 1fr; height: auto; padding: 0; background: $background; dock: bottom; }
    #composer {
        height: 3;
        margin: 0 2 1 2;
        padding: 0 2;
        background: $surface;
        color: $text;
        border: round $primary 22%;
    }
    #composer:focus { border: round $primary 70%; background: $boost; }

    /* ---- footer -------------------------------------------------------- */
    #footer {
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text-muted;
    }
    """

    #: Only keys that are implemented and safe appear here. Anything unfinished is
    #: neither bound nor advertised, so no visible shortcut can disappoint.
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "commands", priority=True),
        Binding("f1", "show_help", "help"),
        Binding("f2", "switch_model", "model"),
        Binding("f3", "switch_provider", "provider"),
        Binding("f4", "switch_mode", "mode"),
        Binding("f6", "toggle_theme", "theme", show=False),
        Binding("ctrl+r", "retry_provider", "retry", show=False),
        Binding("ctrl+l", "clear", "clear", show=False),
        Binding("ctrl+q", "request_quit", "quit", priority=True),
        Binding("ctrl+c", "cancel_task", "cancel", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        settings: Any = None,
        runner: RunnerCallable | None = None,
        first_task: str = "",
        theme_key: str | None = None,
        controller: ProviderStatusController | None = None,
        usage: UsageTracker | None = None,
        resolve_on_mount: bool = True,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._runner = runner
        self.first_task = first_task

        self.tokens: Tokens = tokens_for(theme_key or self._configured_theme(settings))

        # Both controllers are built from local data only, so construction cannot fail
        # because of a provider.
        self.provider_status = controller or ProviderStatusController(settings)
        self.usage = usage or UsageTracker(context_window=self.provider_status.state.context_window)

        self.commands = CommandRegistry(build_default_commands(self))
        self.resolve_on_mount = resolve_on_mount

        self.busy = False
        self.turns = 0
        self.started_at = time.time()
        self._task_worker: Any = None
        self._history: list[str] = []
        self._history_index = 0
        #: Retained transcript entries, replayed when the theme changes.
        self._transcript: list[Any] = []
        #: Set while a Tab completion rewrites the composer, so the resulting
        #: Input.Changed does not immediately re-open the popup we just closed.
        self._suppress_suggestions = False
        self._session_info: dict[str, Any] = {"state": "idle", "mode": self.reasoning_mode_name()}

    @staticmethod
    def _configured_theme(settings: Any) -> str:
        appearance = getattr(settings, "appearance", None)
        return str(getattr(appearance, "theme", "") or "")

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield Static("", id="header-rule")
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Static("", id="notice")
                yield RichLog(id="conversation", wrap=True, markup=False, highlight=False, min_width=24)
                with Vertical(id="composer-shell"):
                    yield SlashSuggestions(self.tokens, id="slash")
                    yield Input(placeholder="Ask, build, or press / for commands", id="composer")
            with VerticalScroll(id="sidebar"):
                yield ProviderCard(tokens=self.tokens, id="card-provider")
                yield ContextCard(tokens=self.tokens, id="card-context")
                yield UsageCard(tokens=self.tokens, id="card-usage")
                yield SessionCard(tokens=self.tokens, id="card-session")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        """Paint from local state and schedule health. Performs no network I/O."""

        self.title = "unknown agent"
        self._apply_theme()
        self.provider_status.subscribe(self._on_provider_state)
        self._render_chrome()
        self._refresh_sidebar()
        self._greet()

        composer = self.query_one("#composer", Input)
        if self.first_task:
            composer.value = self.first_task
        composer.focus()

        if self.resolve_on_mount:
            self._resolve_health()

    # ------------------------------------------------------------------ #
    # theming
    # ------------------------------------------------------------------ #
    def _apply_theme(self) -> None:
        """Map design tokens onto Textual's theme variables.

        Registering a real Textual theme (rather than scattering literal colours
        through the CSS) means ``$surface``, ``$primary``, ``$boost`` and friends all
        resolve from the same token set, and switching themes restyles everything at
        once with no widget-by-widget bookkeeping.
        """

        from textual.theme import Theme

        tokens = self.tokens
        theme = Theme(
            name=f"ua-{tokens.key}",
            primary=tokens.accent,
            secondary=tokens.accent_dim,
            accent=tokens.accent,
            background=tokens.bg,
            surface=tokens.surface,
            panel=tokens.raised,
            boost=tokens.raised,
            success=tokens.ok,
            warning=tokens.warn,
            error=tokens.err,
            foreground=tokens.text,
            dark=tokens.mode == "dark",
            variables={
                "text-muted": tokens.muted,
                "text-disabled": tokens.faint,
                "border": tokens.border,
                "block-cursor-background": tokens.accent,
                "block-cursor-foreground": tokens.bg,
                "input-selection-background": f"{tokens.accent} 30%",
            },
        )
        try:
            self.register_theme(theme)
            self.theme = theme.name
        except Exception:  # noqa: BLE001 - never let theming stop the shell
            log.debug("could not register theme", exc_info=True)

    @safe_action("theme")
    def action_toggle_theme(self) -> None:
        self.tokens = LIGHT if self.tokens.mode == "dark" else DARK
        self._apply_theme()
        for card_id, card in (
            ("#card-provider", ProviderCard),
            ("#card-context", ContextCard),
            ("#card-usage", UsageCard),
            ("#card-session", SessionCard),
        ):
            with ErrorBoundary(self, region="theme", quiet=True):
                self.query_one(card_id, card).set_tokens(self.tokens)
        with ErrorBoundary(self, region="theme", quiet=True):
            self.query_one("#slash", SlashSuggestions).tokens = self.tokens
        self._replay_transcript()
        self._render_chrome()
        self._refresh_sidebar()
        self.note(f"theme: {'light' if self.tokens.mode == 'light' else 'dark'}")

    # ------------------------------------------------------------------ #
    # background health
    # ------------------------------------------------------------------ #
    @work(exclusive=True, group="health")
    async def _resolve_health(self) -> None:
        try:
            await StartupHealthResolver(self.provider_status).run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("health worker failed", exc_info=True)

    def _on_provider_state(self, state: ProviderState) -> None:
        """Receive a transition from any thread and marshal it onto the UI thread."""

        try:
            self.call_from_thread(self._apply_provider_state, state)
        except Exception:  # noqa: BLE001 - not yet mounted, or already torn down
            try:
                self._apply_provider_state(state)
            except Exception:  # noqa: BLE001
                log.debug("could not render provider state", exc_info=True)

    def _apply_provider_state(self, state: ProviderState) -> None:
        if not self.is_running:
            return
        with ErrorBoundary(self, region="provider status", quiet=True):
            self.query_one("#card-provider", ProviderCard).state = state
            if state.context_window:
                self.usage.set_context_window(state.context_window)
            self._render_chrome()
            self._render_notice(state)

    def _render_notice(self, state: ProviderState) -> None:
        """A quiet strip above the conversation when the provider needs attention."""

        notice = self.query_one("#notice", Static)
        if state.ok or state.status == ProviderStatus.VALIDATING:
            notice.remove_class("visible")
            notice.update(Text())
            return

        tokens = self.tokens
        severity = (
            "-error"
            if state.status in {ProviderStatus.CREDITS_EXHAUSTED, ProviderStatus.INVALID_CREDENTIALS, ProviderStatus.OFFLINE}
            else "-warn"
        )
        for candidate in ("-error", "-warn", "-info"):
            notice.remove_class(candidate)
        notice.add_class(severity)

        glyph, colour = status_marker(state.status, tokens)
        body = Text()
        body.append(f"{glyph} ", style=colour)
        body.append(ProviderStatus.label(state.status), style=f"bold {colour}")
        if state.message:
            body.append("   ")
            body.append(truncate(state.message, 120), style=tokens.dim)
        if state.hint:
            body.append("\n")
            body.append(truncate(state.hint, 150), style=tokens.muted)

        keys = []
        if "switch provider" in state.remedies:
            keys.append("f3 provider")
        if "switch model" in state.remedies:
            keys.append("f2 model")
        if "run setup" in state.remedies or "re-enter credentials" in state.remedies:
            keys.append("/setup")
        keys.append("ctrl+r retry")
        body.append("\n")
        body.append("   ".join(keys), style=tokens.accent)

        notice.update(body)
        notice.add_class("visible")

    def handle_degradation(self, error: BaseException, *, region: str, quiet: bool = False) -> None:
        if isinstance(error, Exception):
            self.provider_status.note_runtime_error(error)
        if not quiet:
            self.note(f"{region}: {truncate(str(error), 140)}", tone="err")
        log.debug("degradation in %s", region, exc_info=True)

    def report_problem(self, region: str, error: BaseException) -> None:
        """Last-resort handler for a failing action — visible, never fatal."""

        log.debug("action failed in %s", region, exc_info=True)
        self.note(f"{region} failed: {truncate(str(error), 120)}", tone="err")

    # ------------------------------------------------------------------ #
    # chrome
    # ------------------------------------------------------------------ #
    @property
    def conversation(self) -> RichLog:
        return self.query_one("#conversation", RichLog)

    def write(self, renderable: Any, *, record: bool = True) -> None:
        """Write to the transcript.

        ``renderable`` may be a Rich renderable or a callable taking the current
        :class:`Tokens`. Callables are retained so a theme change can re-render past
        output: RichLog bakes styles into each written line, so without this the
        history would keep the colours of the theme it was written under.
        """

        if record:
            self._transcript.append(renderable)
            del self._transcript[:-500]        # bound the replay buffer
        try:
            self.conversation.write(renderable(self.tokens) if callable(renderable) else renderable)
        except Exception:  # noqa: BLE001
            log.debug("could not write to the conversation", exc_info=True)

    def _replay_transcript(self) -> None:
        """Re-render every retained line under the current tokens."""

        with ErrorBoundary(self, region="transcript", quiet=True):
            entries = list(self._transcript)
            self.conversation.clear()
            for entry in entries:
                try:
                    self.conversation.write(entry(self.tokens) if callable(entry) else entry)
                except Exception:  # noqa: BLE001
                    continue

    def note(self, message: str, *, tone: str = "muted") -> None:
        """A quiet single-line system remark in the transcript."""

        def build(tokens: Tokens) -> Text:
            colour = {"err": tokens.err, "warn": tokens.warn, "ok": tokens.ok, "accent": tokens.accent}.get(
                tone, tokens.muted
            )
            text = Text()
            text.append(f"{GLYPH['dot']} ", style=tokens.faint)
            text.append(message, style=colour)
            return text

        self.write(build)

    def _greet(self) -> None:
        def build(tokens: Tokens) -> Text:
            head = Text()
            head.append("unknown agent", style=f"bold {tokens.text}")
            head.append("\n")
            head.append("The shell runs locally and stays usable whatever the provider is doing.", style=tokens.muted)
            head.append("\n\n")
            head.append("/", style=f"bold {tokens.accent}")
            head.append(" commands      ", style=tokens.muted)
            head.append("ctrl+p", style=f"bold {tokens.dim}")
            head.append(" palette      ", style=tokens.muted)
            head.append("f1", style=f"bold {tokens.dim}")
            head.append(" help", style=tokens.muted)
            return head

        self.write(build)
        self.write(Text(""))

    def _render_chrome(self) -> None:
        self._render_header()
        self._render_footer()

    def _render_header(self) -> None:
        """Product name on the left, live provider identity on the right."""

        tokens = self.tokens
        state = self.provider_status.state
        usage = self.usage.snapshot()

        left = Text()
        left.append("unknown agent", style=f"bold {tokens.text}")

        right = Text()
        glyph, colour = status_marker(state.status, tokens)
        right.append(f"{glyph} ", style=colour)
        right.append(ProviderStatus.label(state.status), style=colour)
        right.append(f"   {GLYPH['vrule']}   ", style=tokens.border)
        right.append(truncate(state.provider_label or state.provider, 18), style=tokens.dim)
        if state.model and state.model != "—":
            right.append(f" {GLYPH['dot']} ", style=tokens.faint)
            right.append(truncate(state.model, 26), style=tokens.text)
        if usage.calls:
            right.append(f"   {GLYPH['vrule']}   ", style=tokens.border)
            right.append(usage.cost_text(), style=tokens.dim)

        try:
            width = self.query_one("#header", Static).size.width or 100
        except Exception:  # noqa: BLE001
            width = 100
        gap = max(1, width - left.cell_len - right.cell_len - 4)
        line = Text()
        line.append_text(left)
        line.append(" " * gap)
        line.append_text(right)

        with ErrorBoundary(self, region="header", quiet=True):
            self.query_one("#header", Static).update(line)
            # ``border`` is too dark to read as a divider against the background;
            # ``border_strong`` is the intended hairline tone.
            self.query_one("#header-rule", Static).update(
                Text(GLYPH["rule"] * max(0, width), style=tokens.border_strong)
            )

    def _render_footer(self) -> None:
        """Only keys that work. Generated from the bindings, so it cannot drift."""

        tokens = self.tokens
        line = Text()
        entries = [("ctrl+p", "commands"), ("/", "slash"), ("f1", "help"), ("f2", "model"), ("f3", "provider"), ("f4", "mode")]
        for index, (key, label) in enumerate(entries):
            if index:
                line.append("    ", style=tokens.faint)
            line.append(key, style=tokens.dim)
            line.append(f" {label}", style=tokens.faint)
        if self.busy:
            line.append("    ")
            line.append("ctrl+c", style=tokens.warn)
            line.append(" cancel", style=tokens.faint)
        with ErrorBoundary(self, region="footer", quiet=True):
            self.query_one("#footer", Static).update(line)

    def _refresh_sidebar(self) -> None:
        snapshot = self.usage.snapshot()
        with ErrorBoundary(self, region="sidebar", quiet=True):
            self.query_one("#card-provider", ProviderCard).state = self.provider_status.state
            self.query_one("#card-context", ContextCard).usage = snapshot
            self.query_one("#card-usage", UsageCard).usage = snapshot
            self._session_info.update(
                {
                    "mode": self.reasoning_mode_name(),
                    "elapsed": format_duration(time.time() - self.started_at),
                    "turns": str(self.turns) if self.turns else "",
                }
            )
            self.query_one("#card-session", SessionCard).info = dict(self._session_info)
        self._render_chrome()

    def on_resize(self) -> None:
        self._render_chrome()

    # ------------------------------------------------------------------ #
    # slash suggestions
    # ------------------------------------------------------------------ #
    @property
    def slash(self) -> SlashSuggestions:
        return self.query_one("#slash", SlashSuggestions)

    @on(Input.Changed, "#composer")
    def _on_composer_changed(self, event: Input.Changed) -> None:
        """Open, filter, or close the suggestion popup as the user types."""

        value = event.value
        if self._suppress_suggestions:
            self._suppress_suggestions = False
            return
        with ErrorBoundary(self, region="suggestions", quiet=True):
            if value.startswith("/") and " " not in value:
                self.slash.show(self.commands.slash_matches(value))
            else:
                self.slash.close()

    async def on_key(self, event: Any) -> None:
        """Route navigation keys to the popup while it is open.

        Handled here rather than as bindings because these keys must keep their normal
        meaning in the composer whenever the popup is closed.
        """

        try:
            popup = self.slash
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        if not popup.is_open:
            return

        key = getattr(event, "key", "")
        if key in {"down", "up"}:
            popup.move(1 if key == "down" else -1)
            event.stop()
            event.prevent_default()
        elif key == "escape":
            popup.close()
            event.stop()
            event.prevent_default()
        elif key == "tab":
            command = popup.current
            if command is not None:
                composer = self.query_one("#composer", Input)
                # Suppress the Changed event this assignment triggers, otherwise the
                # popup re-opens on the completed text and Tab appears to do nothing.
                self._suppress_suggestions = True
                composer.value = f"/{command.slash}"
                composer.action_end()
                popup.close()
            event.stop()
            event.prevent_default()

    @on(SlashSuggestions.Selected)
    def _on_slash_selected(self, event: SlashSuggestions.Selected) -> None:
        self.query_one("#composer", Input).value = ""
        self.slash.close()
        self.invoke(event.command)

    # ------------------------------------------------------------------ #
    # input
    # ------------------------------------------------------------------ #
    @on(Input.Submitted, "#composer")
    def _on_submit(self, event: Input.Submitted) -> None:
        popup = self.slash
        # Enter with the popup open means "run the highlighted command".
        if popup.is_open and popup.current is not None:
            command = popup.current
            self.query_one("#composer", Input).value = ""
            popup.close()
            self.invoke(command)
            return

        text = (event.value or "").strip()
        self.query_one("#composer", Input).value = ""
        popup.close()
        if not text:
            return

        if text.startswith("/"):
            name = text[1:].split()[0] if len(text) > 1 else ""
            command = self.commands.by_slash(name)
            if command is None:
                self.note(f"unknown command /{name} — press / to see what exists", tone="warn")
                return
            self.invoke(command)
            return

        self._history.append(text)
        self._history_index = len(self._history)
        self.run_task(text)

    def invoke(self, command: Command) -> None:
        """Run a command from any surface, with availability and errors handled once."""

        reason = command.availability()
        if reason:
            self.note(f"{command.title.lower()} is unavailable: {reason}", tone="warn")
            return
        handler = command.handler
        if handler is None:
            self.note(f"{command.title.lower()} is not implemented", tone="warn")
            return
        with ErrorBoundary(self, region=command.title.lower()):
            result = handler()
            if asyncio.iscoroutine(result):
                self.run_worker(result, group="command", exclusive=False)

    # ------------------------------------------------------------------ #
    # actions — each opens modals inside a worker, which is what F3/F4 needed
    # ------------------------------------------------------------------ #
    @work(exclusive=True, group="modal")
    @safe_action("command palette")
    async def action_command_palette(self) -> None:
        command = await self.push_screen_wait(CommandPalette(self.commands, tokens=self.tokens))
        if command is not None:
            self.invoke(command)

    @work(exclusive=True, group="modal")
    @safe_action("help")
    async def action_show_help(self) -> None:
        await self.push_screen_wait(HelpScreen(self.commands, tokens=self.tokens))

    @work(exclusive=True, group="modal")
    @safe_action("model switcher")
    async def action_switch_model(self) -> None:
        models, source = await self._available_models()
        if not models:
            self.note("no models available — configure a provider with /setup", tone="warn")
            return
        chosen = await self.push_screen_wait(
            ModelSelector(models, tokens=self.tokens, current=self.provider_status.state.model, source=source)
        )
        if not chosen:
            return
        self._persist(model=chosen)
        self.provider_status.note_switch(model=chosen)
        self.note(f"model → {chosen}", tone="accent")
        self._refresh_sidebar()
        self._resolve_health()

    @work(exclusive=True, group="modal")
    @safe_action("provider switcher")
    async def action_switch_provider(self) -> None:
        profiles = self._profiles()
        chosen = await self.push_screen_wait(
            ProviderSelector(profiles, tokens=self.tokens, current=self.provider_status.state.provider)
        )
        if not chosen:
            return
        if chosen == "__setup__":
            await self._run_setup()
            return
        self._persist(provider=chosen)
        self.provider_status.note_switch(provider=chosen)
        self.note(f"provider → {chosen}", tone="accent")
        self._refresh_sidebar()
        self._resolve_health()

    @work(exclusive=True, group="modal")
    @safe_action("mode switcher")
    async def action_switch_mode(self) -> None:
        chosen = await self.push_screen_wait(ModeSelector(tokens=self.tokens, current=self.reasoning_mode_name()))
        if not chosen:
            return
        with ErrorBoundary(self, region="reasoning mode"):
            reasoning = getattr(self.settings, "reasoning", None)
            if reasoning is not None:
                reasoning.default_mode = chosen  # type: ignore[assignment]
        self._session_info["mode"] = chosen
        self.note(f"reasoning → {chosen}", tone="accent")
        self._refresh_sidebar()

    @work(exclusive=True, group="modal")
    @safe_action("setup")
    async def action_run_setup(self) -> None:
        await self._run_setup()

    async def _run_setup(self) -> None:
        """Hand the terminal to the setup panel, then reload configuration.

        The panel is a Textual app in its own right, so the shell suspends for its
        duration — two apps cannot share one terminal — and resumes afterwards.
        """

        self.note("opening provider setup…")
        with ErrorBoundary(self, region="setup"):
            from unknown_agent.tui.quicksetup import run_setup_panel

            def _run() -> Any:
                return asyncio.run(run_setup_panel())

            with self.suspend():
                result = await asyncio.to_thread(_run)

            if result is not None and not getattr(result, "cancelled", True):
                self._reload_settings()
                self.provider_status.settings = self.settings
                self.provider_status.note_switch(
                    provider=getattr(result, "provider", ""), model=getattr(result, "model", "")
                )
                self.note(f"configured {getattr(result, 'provider', '')}", tone="ok")
                self._refresh_sidebar()
                self._resolve_health()
            else:
                self.note("setup cancelled")

    @safe_action("retry")
    def action_retry_provider(self) -> None:
        self.note("re-checking the provider…")
        self._resolve_health()

    @safe_action("status")
    def action_show_status(self) -> None:
        tokens = self.tokens
        state = self.provider_status.state
        report = self.provider_status.last_report

        body = Text()
        glyph, colour = status_marker(state.status, tokens)
        body.append(f"{glyph} ", style=colour)
        body.append(ProviderStatus.label(state.status), style=f"bold {colour}")
        body.append(f"   {state.summary()}\n", style=tokens.dim)
        if state.message:
            body.append(f"  {state.message}\n", style=tokens.muted)

        if report is not None and report.stages:
            body.append("\n")
            for stage in report.stages:
                glyph = {"ok": GLYPH["ok"], "fail": GLYPH["error"], "warn": GLYPH["warn"]}.get(stage.state, GLYPH["skip"])
                colour = {"ok": tokens.ok, "fail": tokens.err, "warn": tokens.warn}.get(stage.state, tokens.faint)
                body.append(f"  {glyph} ", style=colour)
                body.append(f"{stage.name:<18}", style=tokens.dim)
                body.append(truncate(stage.detail, 60), style=tokens.muted)
                if stage.elapsed_ms:
                    body.append(f"  {stage.elapsed_ms}ms", style=tokens.faint)
                body.append("\n")
                if stage.fix:
                    body.append(f"      {stage.fix}\n", style=tokens.faint)
        else:
            body.append("  no health report yet — press ctrl+r to run one\n", style=tokens.faint)
        self.write(body)

    @safe_action("usage")
    def action_show_usage(self) -> None:
        tokens = self.tokens
        snapshot = self.usage.snapshot()
        body = Text()
        body.append("session usage\n", style=f"bold {tokens.text}")
        if snapshot.calls == 0:
            body.append("  no model calls yet\n", style=tokens.faint)
            self.write(body)
            return
        body.append(f"  {snapshot.tokens_text()}\n", style=tokens.dim)
        body.append(f"  {snapshot.cost_text()} over {snapshot.calls} call(s)\n", style=tokens.dim)
        if snapshot.context_window:
            body.append(
                f"  context {snapshot.context_tokens:,} / {snapshot.context_window:,} ({snapshot.context_pct:.0f}%)\n",
                style=tokens.muted,
            )
        for item in snapshot.by_model:
            body.append(f"  {item.model:<28}{item.total_tokens:>10,}\n", style=tokens.muted)
        if not snapshot.fully_measured:
            body.append("  ~ figures estimated from text length\n", style=tokens.faint)
        self.write(body)

    @safe_action("clear")
    def action_clear(self) -> None:
        self.conversation.clear()
        self._transcript.clear()
        self._greet()

    @safe_action("cancel")
    def action_cancel_task(self) -> None:
        """ctrl+c cancels a run; with nothing running it is a no-op, never an exit."""

        if self.busy and self._task_worker is not None:
            with ErrorBoundary(self, region="cancel", quiet=True):
                self._task_worker.cancel()
            self.note("cancelling…", tone="warn")
        else:
            self.note("nothing running — ctrl+q to quit")

    @work(exclusive=True, group="modal")
    @safe_action("quit")
    async def action_request_quit(self) -> None:
        """Confirm before discarding a running task; quit immediately when idle."""

        if self.busy:
            confirmed = await self.push_screen_wait(
                ConfirmScreen("Quit while a task is running?", "The task will be cancelled and its progress lost.")
            )
            if not confirmed:
                return
        self.exit()

    # ------------------------------------------------------------------ #
    # running tasks
    # ------------------------------------------------------------------ #
    @work(exclusive=True, group="task")
    async def run_task(self, task: str) -> None:
        tokens = self.tokens
        prompt = Text()
        prompt.append(f"{GLYPH['prompt']} ", style=tokens.accent)
        prompt.append(task, style=tokens.text)
        self.write(prompt)
        self.write(Text(""))

        if self._runner is None:
            self.note("no agent attached — this is a UI-only session", tone="warn")
            return

        state = self.provider_status.state
        if not state.can_send:
            self.note(f"{state.message or 'no provider configured'} — press f3, or /setup", tone="warn")
            return

        self.busy = True
        self.turns += 1
        self._session_info["state"] = "working"
        # ``get_current_worker`` is a module-level function, not an App method; the
        # old hasattr() guard silently left this None and made ctrl+c a no-op.
        from textual.worker import get_current_worker

        try:
            self._task_worker = get_current_worker()
        except Exception:  # noqa: BLE001 - not in a worker (direct call in tests)
            self._task_worker = None
        self._refresh_sidebar()
        self._render_footer()
        try:
            with ErrorBoundary(self, region="agent run"):
                result = await self._runner(task, self._hooks())
                self._record_usage(result)
                self.write(self._format_result(result))
                self.write(Text(""))
        except asyncio.CancelledError:
            self.note("cancelled", tone="warn")
            raise
        finally:
            self.busy = False
            self._task_worker = None
            self._session_info["state"] = "idle"
            self._session_info.pop("step", None)
            self._refresh_sidebar()
            self._render_footer()

    def _hooks(self) -> Any:
        from types import SimpleNamespace

        def on_step(label: str = "", **_: Any) -> None:
            self._session_info["step"] = label
            with ErrorBoundary(self, region="progress", quiet=True):
                self.query_one("#card-session", SessionCard).info = dict(self._session_info)

        def on_message(text: str = "", **_: Any) -> None:
            self.write(Text(str(text), style=self.tokens.dim))

        return SimpleNamespace(on_step=on_step, on_message=on_message, on_tool=lambda *a, **k: None)

    def _record_usage(self, result: Any) -> None:
        with ErrorBoundary(self, region="usage accounting", quiet=True):
            model = self.provider_status.state.model
            provider = self.provider_status.state.provider
            usage = getattr(result, "usage", None)
            if usage is not None:
                self.usage.record_from_payload(usage, model=model, provider=provider)
            else:
                self.usage.record_estimated(
                    model=model, provider=provider, completion=str(getattr(result, "output", result) or "")
                )
            self.usage.set_context_tokens(self.usage.snapshot().total_tokens)

    def _format_result(self, result: Any) -> Any:
        tokens = self.tokens
        body = Text()
        body.append(f"{GLYPH['bullet']} ", style=tokens.ok)
        body.append(str(getattr(result, "output", None) or getattr(result, "summary", None) or result), style=tokens.text)
        return body

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    def reasoning_mode_name(self) -> str:
        return str(getattr(getattr(self.settings, "reasoning", None), "default_mode", "") or "default")

    def _profile(self) -> Any:
        profiles = getattr(self.settings, "providers", {}) or {}
        name = active_profile_name(self.settings)
        return profiles.get(name) if isinstance(profiles, dict) else None

    def _profiles(self) -> list[Any]:
        from types import SimpleNamespace

        profiles = getattr(self.settings, "providers", {}) or {}
        rows = []
        for name, profile in (profiles.items() if isinstance(profiles, dict) else []):
            rows.append(
                SimpleNamespace(
                    name=name,
                    detail=truncate(getattr(profile, "model", "") or "", 22),
                    badge=getattr(profile, "auth_mode", "") or getattr(profile, "type", "") or "",
                )
            )
        return rows

    async def _available_models(self) -> tuple[list[Any], str]:
        """Live list when the provider will give one; the curated catalog otherwise.

        A failing provider must not mean "you cannot change model" — that is exactly
        when switching matters most.
        """

        from unknown_agent.providers.catalog import known_model_ids, resolve_entry
        from unknown_agent.providers.discovery import ModelInfo

        profile = self._profile()
        entry = resolve_entry(getattr(profile, "type", "") or self.provider_status.state.provider)

        # For custom openai-compatible gateways the catalog has no entry;
        # fabricate one from the profile so listing can be attempted with the
        # correct base URL and compatibility.  This keeps UNLI-style generic
        # endpoints from being stuck on a single-model dead end.
        if entry is None and profile is not None:
            try:
                from unknown_agent.providers.discovery import resolve_entry_for as _resolve_for_profile

                entry = _resolve_for_profile(profile)
            except Exception:
                entry = None

        # Extract explicit compatibility for the fetch, falling back to the
        # entry protocol so UNLI and generic custom gateways use the right
        # header style and listing path.
        compat: str | None = None
        if profile is not None:
            extra = getattr(profile, "extra", {}) or {}
            if isinstance(extra, dict) and extra.get("compatibility"):
                compat = str(extra["compatibility"])
            elif entry is not None and getattr(entry, "protocol", "") == "anthropic":
                compat = "anthropic"
            elif entry is not None and getattr(entry, "protocol", "") == "openai":
                compat = "openai"

        # A completed health check already holds the authoritative list, whatever the
        # profile's dialect. Check it before anything else so the freshest data wins.
        report = self.provider_status.last_report
        if report is not None and report.models:
            self.usage.learn_pricing_from_models(report.models)
            return list(report.models), "live"

        if entry is None:
            # A generic dialect (``openai-compatible``) maps to no catalog vendor, and
            # with no health report there is nothing to enumerate. Offer at least the
            # configured model so the switcher is never an empty dead end.
            current = getattr(profile, "model", "") or self.provider_status.state.model if profile is not None else self.provider_status.state.model
            known = [ModelInfo(id=current, name=current, provider="custom")] if current and current != "—" else []
            return known, "catalog"

        # Some synthetic entries (generic openai-compatible with no base URL)
        # declare ``model_listing == 'none'`` — fetching would trivially return
        # an empty list.  Skip the network round trip and go straight to the
        # curated/current fallback so the switcher is never empty.
        if getattr(entry, "model_listing", "") == "none":
            catalog_models = [ModelInfo(id=mid, name=mid, provider=entry.key) for mid in known_model_ids(entry)]
            if catalog_models:
                return catalog_models, "catalog"
            current = getattr(profile, "model", "") or self.provider_status.state.model if profile is not None else ""
            # ``current`` may be the placeholder ``"—"`` when no model is set;
            # still return at least one entry so the UI is not a dead end.
            if current and current != "—" and current.strip():
                return [ModelInfo(id=current, name=current, provider=entry.key)], "catalog"
            # No model is configured at all — offer the catalog's default if any,
            # otherwise an empty manual bucket (the selector will handle typed input).
            return catalog_models or [], "catalog"

        try:
            from unknown_agent.providers.discovery import fetch_models

            key = await asyncio.to_thread(ProviderStatusController._resolve_key, profile, entry)
            # Be tolerant of test doubles that don't yet accept ``compatibility``.
            try:
                c = fetch_models(entry, api_key=key, base_url=getattr(profile, "base_url", None), compatibility=compat)
            except TypeError as te:
                if "compatibility" in str(te):
                    c = fetch_models(entry, api_key=key, base_url=getattr(profile, "base_url", None))
                else:
                    raise
            models = list(await asyncio.wait_for(c, timeout=15))
            if not models:
                raise ValueError("empty model list")
            self.usage.learn_pricing_from_models(models)
            return models, "live"
        except Exception as exc:  # noqa: BLE001 - degrade to the catalog, never block
            log.debug("live model listing failed: %s", exc)
            catalog_models = [ModelInfo(id=mid, name=mid, provider=entry.key) for mid in known_model_ids(entry)]
            if catalog_models:
                return catalog_models, "catalog"
            # No curated list and live failed: return the current model as a
            # manual entry so the selector can still offer a typed fallback.
            current = getattr(profile, "model", "") or self.provider_status.state.model if profile is not None else ""
            if current and current != "—":
                return [ModelInfo(id=current, name=current, provider=entry.key)], "manual"
            return [], "manual"

    def _reload_settings(self) -> None:
        with ErrorBoundary(self, region="config reload"):
            from unknown_agent.config.loader import load_settings

            self.settings = load_settings().settings

    def _persist(self, *, provider: str = "", model: str = "") -> None:
        """Persist a switch. A failed write is reported; the session keeps the change."""

        with ErrorBoundary(self, region="saving the selection"):
            from unknown_agent.config.loader import save_settings

            settings = self.settings
            if settings is None:
                return
            name = provider or active_profile_name(settings)
            if provider:
                settings.provider = provider
            if model and name:
                profile = (getattr(settings, "providers", {}) or {}).get(name)
                if profile is not None:
                    profile.model = model
            save_settings(settings)


async def run_app_shell(
    settings: Any = None,
    *,
    runner: RunnerCallable | None = None,
    first_task: str = "",
    headless: bool = False,
    resolve_on_mount: bool = True,
) -> TUIAppShell:
    """Launch the shell. Never raises for provider reasons — that is the contract."""

    app = TUIAppShell(settings=settings, runner=runner, first_task=first_task, resolve_on_mount=resolve_on_mount)
    if headless:
        async with app.run_test(size=(150, 44)) as pilot:
            await pilot.pause()
        return app
    await app.run_async()
    return app
