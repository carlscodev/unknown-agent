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
    Screen { background: $background; layers: base overlay; }

    /* ---- header — refined, with subtle transitions -------------------- */
    #header {
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text;
        transition: padding 180ms in_out_cubic;
    }
    #header-rule { height: 1; color: $primary 18%; background: $background; }
    TUIAppShell.is-phone #header { padding: 0 1; }
    TUIAppShell.is-narrow #header { padding: 0 1; }
    TUIAppShell.is-compact #header { padding: 0 2; }

    /* ---- body — responsive: horizontal on desktop, vertical on phone -- */
    #body { width: 1fr; height: 1fr; background: $background; layout: horizontal; }
    TUIAppShell.is-narrow #body { layout: vertical; }
    TUIAppShell.is-phone #body { layout: vertical; }
    #main { width: 1fr; height: 1fr; background: $background; min-width: 20; }
    TUIAppShell.is-phone #main { min-width: 12; }

    /* ---- sidebar — collapsible, animated, never fights for width ----- */
    #sidebar {
        width: 32;
        height: 1fr;
        padding: 1 2;
        background: $surface;
        scrollbar-size: 1 1;
        scrollbar-background: $surface;
        scrollbar-color: $primary 22%;
        transition: width 220ms in_out_cubic, opacity 200ms, padding 180ms;
        hatch: right $surface;
    }
    TUIAppShell.is-compact #sidebar { width: 27; padding: 1 1; }
    TUIAppShell.is-narrow #sidebar {
        width: 1fr;
        height: auto;
        max-height: 18;
        padding: 1 1;
        border-top: solid $primary 12%;
        /* stacked below main, not fighting for columns */
    }
    TUIAppShell.is-phone #sidebar {
        width: 1fr;
        height: auto;
        max-height: 14;
        padding: 1 1;
        border-top: solid $primary 14%;
    }
    TUIAppShell.is-narrow #sidebar.hidden { display: none; }
    TUIAppShell.is-phone #sidebar.hidden { display: none; }
    /* collapsed state on desktop (user toggled) */
    TUIAppShell.sidebar-collapsed #sidebar { display: none; }
    TUIAppShell.sidebar-collapsed #body { layout: horizontal; }

    /* ---- conversation — breathing room adapts to width ---------------- */
    #conversation {
        height: 1fr;
        padding: 1 3;
        background: $background;
        scrollbar-size: 1 1;
        scrollbar-background: $background;
        scrollbar-color: $primary 22%;
    }
    TUIAppShell.is-compact #conversation { padding: 1 2; }
    TUIAppShell.is-narrow #conversation { padding: 1 1; }
    TUIAppShell.is-phone #conversation { padding: 1 1; }

    /* ---- notice — slide + fade, not pop ------------------------------- */
    #notice {
        display: none;
        height: auto;
        max-height: 6;
        margin: 0 2 1 2;
        padding: 1 2;
        background: $boost;
        border-left: thick $warning;
        opacity: 0;
        offset-y: -1;
        transition: opacity 200ms, offset 200ms in_out_cubic;
    }
    #notice.visible { display: block; opacity: 1; offset-y: 0; }
    #notice.-error { border-left: thick $error; }
    #notice.-warn  { border-left: thick $warning; }
    #notice.-info  { border-left: thick $primary; }
    TUIAppShell.is-narrow #notice { margin: 0 1 1 1; padding: 1 1; max-height: 5; }
    TUIAppShell.is-phone #notice { margin: 0 1 1 1; padding: 1 1; max-height: 4; }

    /* ---- composer — docked, subtle focus, never clipped --------------- */
    #composer-shell { width: 1fr; height: auto; padding: 0; background: $background; dock: bottom; }
    #composer {
        height: 3;
        margin: 0 2 1 2;
        padding: 0 2;
        background: $surface;
        color: $text;
        border: round $primary 22%;
        transition: border 150ms, background 150ms, margin 180ms, padding 180ms;
    }
    #composer:focus { border: round $primary 70%; background: $boost; }
    TUIAppShell.is-compact #composer { margin: 0 1 1 1; padding: 0 1; }
    TUIAppShell.is-narrow #composer { margin: 0 1 1 1; padding: 0 1; }
    TUIAppShell.is-phone #composer { margin: 0 1 1 1; padding: 0 1; }

    /* ---- footer — compact on mobile, rich on desktop ------------------ */
    #footer {
        height: 1;
        padding: 0 2;
        background: $background;
        color: $text-muted;
        transition: padding 180ms;
    }
    TUIAppShell.is-narrow #footer { padding: 0 1; }
    TUIAppShell.is-phone #footer { padding: 0 1; }

    /* ---- cards — opacity fade for polish -------------------------------- */
    SidebarCard { height: auto; padding: 0; margin: 0 0 1 0; opacity: 1; transition: opacity 180ms; }
    SidebarCard.hidden { display: none; }
    TUIAppShell.is-phone SidebarCard { margin: 0 0 1 0; }

    /* ---- slash popup — subtle slide ------------------------------------ */
    SlashSuggestions { transition: opacity 150ms, offset 150ms in_out_cubic; }
    TUIAppShell.is-narrow SlashSuggestions { margin: 0 1; max-height: 7; }
    TUIAppShell.is-phone SlashSuggestions { margin: 0 1; max-height: 6; }

    /* ---- busy — tasteful, not noisy ------------------------------------ */
    TUIAppShell.is-busy #header { color: $accent; }
    TUIAppShell.is-busy #composer { border: round $accent 45%; }
    TUIAppShell.is-busy #footer { color: $accent; }

    /* ---- context bar — only on narrow, keeps mobile aware without sidebar -- */
    #context-bar { display: none; height: 1; padding: 0 2; background: $surface; color: $text-muted; border-bottom: solid $primary 10%; }
    TUIAppShell.is-narrow #context-bar.has-content { display: block; }
    TUIAppShell.is-phone #context-bar.has-content { display: block; padding: 0 1; }
    #context-bar.has-content { display: block; }
    """

    #: Only keys that are implemented and safe appear here. Anything unfinished is
    #: neither bound nor advertised, so no visible shortcut can disappoint.
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "commands", priority=True),
        Binding("f1", "show_help", "help"),
        Binding("f2", "switch_model", "model"),
        Binding("f3", "switch_provider", "provider"),
        Binding("f4", "switch_mode", "mode"),
        Binding("f5", "toggle_sidebar", "sidebar", show=False),
        Binding("ctrl+b", "toggle_sidebar", "sidebar", show=False),
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
        #: Last exception raised while trying to build a chat runner.  Surfaced in
        #: diagnostics so “backend attach failed” is not conflated with “no provider”.
        self._attach_error: str | None = None

        # — responsive state: breakpoints drive stacked vs side-by-side —
        # Termius on a phone is often 40-60 cols.  Good TUI systems explicitly
        # switch layouts at width breakpoints instead of assuming desktop width.
        self._breakpoint: str = "wide"  # wide | compact | narrow | phone
        self._sidebar_visible: bool = True  # honored on narrow/phone
        self._sidebar_auto_hidden: bool = False

        # Auto-attach a chat runner if config already contains a valid provider/model.
        # This is the fix for “no agent attached” when the TUI was launched without
        # an explicit runner (cli/tui_command did not build one).  The shell now
        # recovers on its own instead of staying UI-only until /setup is run.
        try:
            if self._runner is None and self.settings is not None:
                self._ensure_runner()
        except Exception:  # noqa: BLE001
            log.debug("auto-attach at init failed", exc_info=True)

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
        yield Static("", id="context-bar")
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
        # If settings were loaded after construction (or the init auto-attach raced
        # with a not-yet-ready provider_status), give it one more try now.
        try:
            if self._runner is None:
                self._ensure_runner()
        except Exception:  # noqa: BLE001
            log.debug("auto-attach at mount failed", exc_info=True)
        self._render_chrome()
        self._refresh_sidebar()
        self._greet()

        composer = self.query_one("#composer", Input)
        if self.first_task:
            composer.value = self.first_task
        composer.focus()

        if self.resolve_on_mount:
            self._resolve_health()
        # Responsive pass must run after first paint so size is known.
        try:
            self._update_responsive()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # responsive — width breakpoints, stacked vs side-by-side
    # ------------------------------------------------------------------ #
    def _breakpoint_for_width(self, width: int) -> str:
        """Phone-first breakpoints.  Termius phone portrait is ~40-60 cols."""

        if width < 60:
            return "phone"
        if width < 80:
            return "narrow"
        if width < 110:
            return "compact"
        return "wide"

    def _update_responsive(self) -> None:
        """Choose a breakpoint from ``self.size`` and apply it via classes.

        Called on mount and on every resize.  No network I/O, purely local.
        """

        try:
            width = int(getattr(self.size, "width", 0) or 0)
        except Exception:
            width = 0
        if width <= 0:
            try:
                width = self.console.width  # type: ignore[attr-defined]
            except Exception:
                width = 120
        new_bp = self._breakpoint_for_width(width)
        # Always re-apply even if breakpoint unchanged — height may have changed
        # and sidebar visibility logic depends on it.
        self._apply_breakpoint(new_bp, width)

    def _apply_breakpoint(self, bp: str, width: int) -> None:
        prev = getattr(self, "_breakpoint", "wide")
        self._breakpoint = bp

        # Map breakpoint → App classes (TUIAppShell.is-*) which drive the CSS above.
        for cls in ("is-phone", "is-narrow", "is-compact", "is-wide"):
            try:
                self.remove_class(cls)
            except Exception:
                pass
        try:
            self.add_class(f"is-{bp}")
        except Exception:
            pass

        # Sidebar policy — responsive defaults, user toggle preserved:
        # - wide/compact (desktop): sidebar visible by default.  If the user
        #   collapsed it on desktop via f5, keep it collapsed.  When coming
        #   from mobile (where we default-hide), reset to visible so desktop
        #   is not unexpectedly empty.
        # - narrow/phone (mobile): sidebar hidden by default to give the
        #   conversation full width; the header shows the compact status and
        #   f5/ctrl+b reveals the stacked panels.
        is_mobile = bp in ("phone", "narrow")
        should_show: bool
        if not is_mobile:
            # Coming from mobile where we default-hide — restore desktop default
            if prev in ("phone", "narrow"):
                # If the user had explicitly shown the sidebar on mobile, keep
                # that intent; otherwise default to visible on desktop.
                # We treat “user wanted it visible on mobile” as True → keep visible.
                # If they never toggled (still False), still default to visible.
                self._sidebar_visible = True
            should_show = bool(self._sidebar_visible)
            try:
                self.query_one("#sidebar").remove_class("hidden")
            except Exception:
                pass
            try:
                self.remove_class("sidebar-collapsed")
            except Exception:
                pass
            if not should_show:
                try:
                    self.add_class("sidebar-collapsed")
                except Exception:
                    pass
        else:
            # Mobile: hidden by default.  Only show if user explicitly toggled on.
            if prev not in ("phone", "narrow"):
                # First time entering mobile — hide by default, regardless of
                # previous desktop visibility.  User can press f5 to reveal.
                self._sidebar_visible = False
                should_show = False
            else:
                should_show = bool(self._sidebar_visible)
            try:
                sidebar = self.query_one("#sidebar")
                if should_show:
                    sidebar.remove_class("hidden")
                else:
                    sidebar.add_class("hidden")
            except Exception:
                pass

        # Re-render chrome so header/footer adapt to new width
        try:
            self._render_chrome()
            self._refresh_sidebar()
        except Exception:
            pass

        # Also adapt composer placeholder for cramped widths
        try:
            composer = self.query_one("#composer", Input)
            if bp == "phone":
                composer.placeholder = "Ask or / for commands"
            elif bp == "narrow":
                composer.placeholder = "Ask, or press /"
            else:
                composer.placeholder = "Ask, build, or press / for commands"
        except Exception:
            pass

    @safe_action("sidebar")
    def action_toggle_sidebar(self) -> None:
        """Toggle the sidebar — crucial on phone where it is hidden by default."""

        self._sidebar_visible = not self._sidebar_visible
        is_mobile = self._breakpoint in ("phone", "narrow")
        try:
            if is_mobile:
                sidebar = self.query_one("#sidebar")
                if self._sidebar_visible:
                    sidebar.remove_class("hidden")
                    self.note("sidebar shown — f5 to hide", tone="muted")
                else:
                    sidebar.add_class("hidden")
                    self.note("sidebar hidden — f5 to show", tone="muted")
            else:
                if self._sidebar_visible:
                    self.remove_class("sidebar-collapsed")
                    try:
                        self.query_one("#sidebar").remove_class("hidden")
                    except Exception:
                        pass
                    self.note("sidebar shown", tone="muted")
                else:
                    self.add_class("sidebar-collapsed")
                    self.note("sidebar hidden — f5 to show", tone="muted")
        except Exception as exc:  # noqa: BLE001
            self.report_problem("sidebar", exc)
        try:
            self._render_chrome()
        except Exception:
            pass

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
            # A health transition to “can_send” may mean the provider that was
            # previously invalid is now usable (user ran /setup, network came
            # back).  If the shell still lacks a runner, attach one now.
            try:
                if self._runner is None and state.can_send:
                    self._ensure_runner()
            except Exception:  # noqa: BLE001
                log.debug("auto-attach on provider state failed", exc_info=True)
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
        """Product name on the left, live provider identity on the right.

        Responsive: on phone/narrow the header collapses to the essentials —
        status dot + provider + truncated model — and hides cost/context to
        avoid clipping.  On wide it shows the full richer line.
        """

        tokens = self.tokens
        state = self.provider_status.state
        usage = self.usage.snapshot()
        bp = getattr(self, "_breakpoint", "wide")

        # Left: full name on desktop, compact “ua” on phone where every col matters
        left = Text()
        if bp == "phone":
            left.append("ua", style=f"bold {tokens.text}")
        else:
            left.append("unknown agent", style=f"bold {tokens.text}")

        # Right: build adaptively
        right = Text()
        glyph, colour = status_marker(state.status, tokens)
        right.append(f"{glyph} ", style=colour)

        # On phone, shorten status label (“ready”→“ok” is already short, but we keep it)
        status_label = ProviderStatus.label(state.status)
        if bp == "phone" and len(status_label) > 8:
            status_label = status_label[:6] + "…"
        right.append(status_label, style=colour)

        # Divider + provider
        right.append(f"   {GLYPH['vrule']}   ", style=tokens.border)
        # Truncation adapts to width: 18→12 on phone
        prov_limit = 10 if bp == "phone" else (12 if bp == "narrow" else (16 if bp == "compact" else 18))
        model_limit = 12 if bp == "phone" else (16 if bp == "narrow" else (22 if bp == "compact" else 26))
        right.append(truncate(state.provider_label or state.provider, prov_limit), style=tokens.dim)
        if state.model and state.model != "—":
            # On phone, hide model if it would overflow; keep dot only if space
            if bp != "phone" or len(state.provider_label or state.provider) < 8:
                right.append(f" {GLYPH['dot']} ", style=tokens.faint)
                right.append(truncate(state.model, model_limit), style=tokens.text)

        # Cost/context only on wide/compact where it does not clip
        if usage.calls and bp in ("wide", "compact"):
            right.append(f"   {GLYPH['vrule']}   ", style=tokens.border)
            right.append(usage.cost_text(), style=tokens.dim)
        elif bp in ("phone", "narrow") and self._sidebar_visible is False:
            # Hint that sidebar is hidden and can be toggled — subtle, not noisy
            right.append(f"   {GLYPH['vrule']}   ", style=tokens.border)
            right.append("f5", style=tokens.dim)
            right.append(" info", style=tokens.faint)

        try:
            width = self.query_one("#header", Static).size.width or 100
        except Exception:  # noqa: BLE001
            width = 100
        # Ensure we never produce negative gap — truncate right if needed
        max_right = max(10, width - left.cell_len - 4)
        if right.cell_len > max_right:
            # Re-truncate more aggressively
            right = Text()
            right.append(f"{glyph} ", style=colour)
            right.append(truncate(status_label, 6), style=colour)
            right.append(f" {GLYPH['dot']} ", style=tokens.faint)
            right.append(truncate(state.provider_label or state.provider, 8), style=tokens.dim)

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
        """Only keys that work. Responsive: fewer keys on narrow so they never clip."""

        tokens = self.tokens
        bp = getattr(self, "_breakpoint", "wide")
        line = Text()
        # On phone, every column counts — show only the essentials and hint at more
        if bp == "phone":
            entries = [("ctrl+p", "cmds"), ("/", "slash"), ("f1", "help")]
            # Add sidebar toggle hint when hidden
            if not getattr(self, "_sidebar_visible", True):
                entries.append(("f5", "info"))
        elif bp == "narrow":
            entries = [("ctrl+p", "commands"), ("/", "slash"), ("f1", "help"), ("f5", "info")]
        elif bp == "compact":
            entries = [("ctrl+p", "commands"), ("/", "slash"), ("f1", "help"), ("f2", "model"), ("f3", "provider")]
        else:
            entries = [("ctrl+p", "commands"), ("/", "slash"), ("f1", "help"), ("f2", "model"), ("f3", "provider"), ("f4", "mode")]

        # Check available width and drop trailing entries if they'd clip
        try:
            width = self.query_one("#footer", Static).size.width or 100
        except Exception:
            width = 100
        # Estimate length and prune from the end while it overflows
        def _est_len(entries: list[tuple[str,str]]) -> int:
            # key + space + label + 4 spacer per entry
            return sum(len(k) + 1 + len(l) + 4 for k,l in entries) - 4
        while entries and _est_len(entries) + (12 if self.busy else 0) > width - 2 and len(entries) > 2:
            entries.pop()

        for index, (key, label) in enumerate(entries):
            if index:
                line.append("    ", style=tokens.faint)
            line.append(key, style=tokens.dim)
            line.append(f" {label}", style=tokens.faint)
        if self.busy:
            line.append("    ")
            line.append("ctrl+c", style=tokens.warn)
            line.append(" cancel", style=tokens.faint)
        elif bp in ("phone","narrow") and self.busy is False:
            # On mobile, hint that more commands exist in palette
            if len(entries) < 4:
                line.append("    ")
                line.append("…", style=tokens.faint)
        with ErrorBoundary(self, region="footer", quiet=True):
            self.query_one("#footer", Static).update(line)

    def _refresh_sidebar(self) -> None:
        snapshot = self.usage.snapshot()
        bp = getattr(self, "_breakpoint", "wide")
        with ErrorBoundary(self, region="sidebar", quiet=True):
            self.query_one("#card-provider", ProviderCard).state = self.provider_status.state
            # On phone/narrow, hide secondary cards by default to avoid ugly density —
            # they are still reachable via f5 toggle which reveals the whole sidebar.
            # We keep Provider + Session (the most actionable) and collapse the rest.
            ctx_card = self.query_one("#card-context", ContextCard)
            usage_card = self.query_one("#card-usage", UsageCard)
            ctx_card.usage = snapshot
            usage_card.usage = snapshot
            # Toggle visibility via display
            try:
                if bp in ("phone", "narrow"):
                    # On mobile, usage/context are secondary — hide if sidebar is
                    # in compact stacked mode and there are no calls yet, or keep
                    # but with reduced height.  For now, keep provider+session always,
                    # hide usage when empty, and keep context but it is already compact.
                    if snapshot.calls == 0:
                        usage_card.add_class("hidden")
                    else:
                        usage_card.remove_class("hidden")
                    # Context always visible but meter will adapt in dashboard.py
                    ctx_card.remove_class("hidden")
                else:
                    ctx_card.remove_class("hidden")
                    usage_card.remove_class("hidden")
            except Exception:
                pass
            self._session_info.update(
                {
                    "mode": self.reasoning_mode_name(),
                    "elapsed": format_duration(time.time() - self.started_at),
                    "turns": str(self.turns) if self.turns else "",
                }
            )
            self.query_one("#card-session", SessionCard).info = dict(self._session_info)
        self._render_chrome()
        self._render_context_bar()

    def _render_context_bar(self) -> None:
        """Thin context meter shown only on narrow/phone (sidebar hidden).

        On mobile the sidebar is collapsed by default, so the user would otherwise
        have no idea how full the context window is.  This bar keeps the most
        critical signal visible without taking a full panel.
        """

        try:
            bar = self.query_one("#context-bar", Static)
        except Exception:
            return
        bp = getattr(self, "_breakpoint", "wide")
        snapshot = self.usage.snapshot()
        tokens = self.tokens

        # Only show on narrow/phone and when we have a real window
        if bp not in ("phone", "narrow") or snapshot.context_window <= 0:
            try:
                bar.remove_class("has-content")
                bar.update(Text(""))
            except Exception:
                pass
            return

        # Build a compact line:  label + meter +  “ 12k/128k 9% ”
        try:
            width = bar.size.width or 40
        except Exception:
            width = 40
        # Meter width adapts to bar width: leave room for text
        meter_width = max(8, min(16, width - 20))
        fraction = snapshot.context_tokens / max(1, snapshot.context_window)
        # Use same meter helper but smaller
        from unknown_agent.tui.design import meter as _meter

        line = Text()
        line.append("ctx ", style=tokens.muted)
        line.append_text(_meter(fraction, meter_width, tokens))
        line.append(f"  {snapshot.context_pct:.0f}%", style=tokens.dim)
        # Only add token counts if we have space
        if width > 36:
            from unknown_agent.tui.design import compact_number
            line.append(f"  {compact_number(snapshot.context_tokens)}/{compact_number(snapshot.context_window)}", style=tokens.faint)
        try:
            bar.update(line)
            bar.add_class("has-content")
        except Exception:
            pass

    def on_resize(self) -> None:
        try:
            self._update_responsive()
        except Exception:  # noqa: BLE001
            pass
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
            # ``handler`` may be:
            # * a ``@work``-decorated action  → returns ``Worker`` (already scheduled)
            # * a plain ``async def``          → returns a coroutine that we must schedule
            # * a sync function               → returns None / value
            # The previous code only handled coroutines, so a ``Worker`` was silently
            # ignored and a slash command that returned a coroutine was not always
            # scheduled in tests.  Handle both.
            try:
                from textual.worker import Worker as _TextualWorker

                if isinstance(result, _TextualWorker):
                    return
            except Exception:  # noqa: BLE001
                pass
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
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
        self._attach_error = None
        self._runner = None
        try:
            self._ensure_runner()
        except Exception:  # noqa: BLE001
            pass
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
        self._attach_error = None
        self._runner = None
        try:
            self._ensure_runner()
        except Exception:  # noqa: BLE001
            pass
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

        The previous implementation used ``asyncio.to_thread(asyncio.run(...))`` which
        runs the second app in a different thread while the first is suspended in the
        main thread.  That is not how Textual's ``suspend`` is intended to be used
        and it fails in headless tests (``App.suspend is not supported``) and can
        race in a real terminal.  The correct pattern is ``with self.suspend(): await
        run_setup_panel()`` in the same loop — the suspended app releases the
        terminal and the new app takes it over directly.
        """

        self.note("opening provider setup…")
        with ErrorBoundary(self, region="setup"):
            from unknown_agent.tui.quicksetup import run_setup_panel

            result: Any = None
            try:
                # Preferred path: suspend the shell and run the setup panel in the
                # same event loop.  This is what Textual documents and what works
                # under ``run_test`` when suspend is supported.
                with self.suspend():
                    result = await run_setup_panel()
            except Exception as exc:  # noqa: BLE001 - suspend not supported in headless
                message = str(exc).lower()
                if "suspend" in message or "not supported" in message or isinstance(exc, NotImplementedError):
                    # Headless / test environment: run without suspend.  The panel
                    # will still render in the test harness, it just won't suspend.
                    log.debug("suspend not supported, running setup without suspend: %s", exc)
                    try:
                        result = await run_setup_panel()
                    except Exception as inner:  # noqa: BLE001
                        self.report_problem("setup", inner)
                        return
                else:
                    self.report_problem("setup", exc)
                    return

            if result is not None and not getattr(result, "cancelled", True):
                self._reload_settings()
                self.provider_status.settings = self.settings
                self.provider_status.note_switch(
                    provider=getattr(result, "provider", ""), model=getattr(result, "model", "")
                )
                # A newly configured provider/model must immediately become a live
                # backend — do not wait for the next health tick or the next user
                # message.  This is what makes “/setup → Hola” work without a restart.
                self._attach_error = None
                self._runner = None
                try:
                    self._ensure_runner()
                except Exception:  # noqa: BLE001
                    pass
                self.note(f"configured {getattr(result, 'provider', '')}", tone="ok")
                self._refresh_sidebar()
                self._resolve_health()
            else:
                self.note("setup cancelled")

    @safe_action("retry")
    def action_retry_provider(self) -> None:
        self.note("re-checking the provider…")
        self._attach_error = None
        try:
            self._ensure_runner()
        except Exception:  # noqa: BLE001
            pass
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

        # Lazy recovery: the user may have typed while health was still
        # validating, or the shell was started without a runner.  Try once to
        # attach before we declare the session UI-only.
        if self._runner is None:
            try:
                self._ensure_runner()
            except Exception:  # noqa: BLE001
                log.debug("lazy attach in run_task failed", exc_info=True)

        if self._runner is None:
            can, reason, details = self._diagnose_attachment()
            # No runner and we cannot attach — show a precise, actionable note
            # instead of the old generic yellow warning.
            hint = details.get("hint") or ""
            provider = details.get("provider") or "—"
            model = details.get("model") or "—"
            status = details.get("status") or ""
            # Build a compact diagnostic line: reason + what we tried + how to fix.
            msg = reason or "no agent attached"
            # Include provider/model that was tried so the user sees what failed.
            context = f" (provider {provider} · model {model})" if provider != "—" or model != "—" else ""
            remedies: list[str] = []
            if "no provider" in reason or "no default provider" in reason:
                remedies.append("/setup")
                remedies.append("f3 provider")
            elif "no default model" in reason:
                remedies.append("f2 model")
                remedies.append("/setup")
            elif status in {"invalid_credentials", "credits_exhausted", "offline"} or not can:
                # Health-reported failure: suggest retry + setup
                remedies.append("ctrl+r retry")
                remedies.append("/setup")
                if "invalid" in reason.lower() or "credential" in reason.lower():
                    remedies.append("check API key")
            else:
                remedies.append("ctrl+r retry")
                remedies.append("/setup")
            remedy_str = "  →  ".join(remedies)
            if hint and hint not in remedy_str:
                remedy_str = f"{hint}  →  {remedy_str}" if remedy_str else hint
            self.note(f"{msg}{context} — {remedy_str}" if remedy_str else f"{msg}{context}", tone="warn")
            return

        state = self.provider_status.state
        if not state.can_send:
            # Even with a runner, provider health says we cannot send (e.g. key
            # expired mid-session).  Show provider's own message + hint rather
            # than “no agent attached”.
            msg = getattr(state, "message", "") or "provider cannot be used right now"
            hint = getattr(state, "hint", "") or "press ctrl+r to retry or /setup to reconfigure"
            provider_label = getattr(state, "provider_label", "") or getattr(state, "provider", "")
            model = getattr(state, "model", "")
            context = f" [{provider_label} · {model}]" if provider_label else ""
            self.note(f"{msg}{context} — {hint}", tone="warn")
            return

        self.busy = True
        self.turns += 1
        self._session_info["state"] = "working"
        # Premium polish: subtle busy state drives header/footer animations
        try:
            self.add_class("is-busy")
        except Exception:
            pass
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
            try:
                self.remove_class("is-busy")
            except Exception:
                pass
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
    # ------------------------------------------------------------------ #
    # agent attachment — the “no agent attached” root cause lives here
    # ------------------------------------------------------------------ #
    def _diagnose_attachment(self) -> tuple[bool, str, dict[str, str]]:
        """Precise diagnosis of why a chat cannot run.

        Returns ``(can_attach, reason, details)`` where ``reason`` is a short
        human sentence and ``details`` carries the tried provider/model and the
        underlying error.  This is what the UI shows instead of the vague
        “no agent attached — this is a UI-only session”.

        The product previously conflated four distinct states:

        * provider configured  — a profile exists in ``settings.providers``
        * provider available   — health says the credential works
        * model selected       — ``profile.model`` is set
        * agent attached       — a live ``_runner`` callable exists

        A valid config can still leave ``_runner is None`` when the shell was
        launched without a runner (``tui_command`` never built one) or when the
        provider failed to instantiate.
        """

        state = getattr(self.provider_status, "state", None)
        profile = self._profile()
        profile_name = active_profile_name(self.settings) if self.settings is not None else ""
        tried_provider = getattr(state, "provider", "—") if state is not None else profile_name or "—"
        tried_model = getattr(state, "model", "—") if state is not None else (getattr(profile, "model", "") if profile is not None else "—")
        tried_label = getattr(state, "provider_label", "") if state is not None else ""

        # 1. No settings at all
        if self.settings is None:
            return False, "no settings loaded", {"provider": tried_provider, "model": tried_model, "hint": "Run /setup"}
        providers = getattr(self.settings, "providers", {}) or {}
        if not providers:
            return False, "no provider configured", {"provider": tried_provider, "model": tried_model, "hint": "Press /setup or f3 to add one"}
        # 2. No active profile
        if not profile_name or profile is None:
            return False, "no default provider selected", {"provider": tried_provider, "model": tried_model, "hint": "Press f3 to pick a provider or /setup to create one"}
        # 3. No model
        model = getattr(profile, "model", "") or (getattr(state, "model", "") if state is not None else "")
        if not model or model == "—":
            return False, "no default model selected", {"provider": tried_provider or profile_name, "model": "—", "hint": "Press f2 to pick a model or re-run /setup"}
        # 4. Provider health says it cannot send
        if state is not None and not getattr(state, "can_send", True):
            msg = getattr(state, "message", "") or "provider cannot be used"
            hint = getattr(state, "hint", "") or "Press ctrl+r to retry, /setup to reconfigure"
            return False, msg, {"provider": tried_label or tried_provider, "model": tried_model, "hint": hint, "status": getattr(state, "status", "")}
        # 5. Runner attach previously failed — surface the actual build error so
        #    “backend attach failed” is not conflated with “no agent attached”.
        if self._runner is None and getattr(self, "_attach_error", None):
            return False, f"backend attach failed: {self._attach_error}", {"provider": tried_label or tried_provider, "model": tried_model, "hint": "Press ctrl+r to retry or /setup to reconfigure", "status": "attach_failed"}
        # 6. Runner not attached even though provider/model look ok — this is the
        #    bug the user reported.  It happens because ``tui_command`` launched
        #    ``TUIAppShell`` without a runner.  We can still recover by building a
        #    direct provider runner on demand.
        if self._runner is None:
            # We *can* attach — the caller should try to build a runner.  Distinguish
            # “can attach” from “already attached” by returning True but with a
            # special reason.
            return True, "runner not yet attached but provider/model are valid — will attach on demand", {"provider": tried_label or tried_provider, "model": tried_model}
        return True, "", {"provider": tried_label or tried_provider, "model": tried_model}

    def _build_chat_runner(self) -> RunnerCallable | None:
        """Build a minimal chat runner from the current settings.

        This is the fallback that makes ``Hola`` work when the shell was launched
        without an explicit ``_runner``.  It directly uses the configured provider
        to answer, with streaming if the provider supports it.  No LiteLLM is
        involved — it goes through ``build_provider`` and the provider's own
        ``stream``/``complete`` methods, so the correct wire format (OpenAI SSE
        vs Anthropic SSE vs Ollama) is used automatically.
        """

        profile = self._profile()
        if profile is None:
            return None
        profile_name = active_profile_name(self.settings) if self.settings is not None else "default"
        # Capture current profile/model at build time, but re-resolve provider on
        # each call so a later ``/setup`` or ``f3`` takes effect immediately.
        def _resolve_provider():  # type: ignore[no-untyped-def]
            try:
                from unknown_agent.providers.registry import build_provider

                # Re-read settings in case it changed since runner was built
                current_profile = self._profile()
                current_name = active_profile_name(self.settings) if self.settings is not None else profile_name
                if current_profile is None:
                    current_profile = profile
                    current_name = profile_name
                return build_provider(current_profile, name=current_name or "default")
            except Exception as exc:  # noqa: BLE001
                log.debug("chat runner: could not build provider: %s", exc)
                # Remember the root cause so diagnostics can surface “backend attach
                # failed” instead of the generic “no provider available”.
                try:
                    self._attach_error = str(exc)[:250]  # type: ignore[attr-defined]
                except Exception:
                    pass
                return None

        async def _chat_runner(task: str, hooks: Any) -> Any:  # type: ignore[no-untyped-def]
            # Simple prompt classification: greetings like "Hola" must be normal
            # chat, not build/planning.  We treat any short message without
            # build verbs as chat.  The full ``Runner`` would do heavier
            # analysis, but for the shell's direct chat path we just need to
            # answer normally.
            from unknown_agent.core.models import Message
            from unknown_agent.providers.base import ModelRequest

            provider = _resolve_provider()
            if provider is None:
                raise RuntimeError("no provider available — run /setup")
            # Build request — single user turn, stream if the provider advertises it
            current_profile = self._profile()
            model = getattr(current_profile, "model", provider.model) if current_profile is not None else provider.model
            temperature = float(getattr(current_profile, "temperature", provider.temperature) if current_profile is not None else provider.temperature)
            max_tokens = int(getattr(current_profile, "max_output_tokens", provider.max_output_tokens) if current_profile is not None else provider.max_output_tokens)
            request = ModelRequest(
                messages=[Message(role="user", content=task)],
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
            # ``provider.model`` may differ from the profile's model; honor the
            # profile.
            original_model = provider.model
            if model:
                provider.model = model
            try:
                # Prefer streaming when available — it is SSE for OpenAI/Anthropic
                # and chunked JSON for Ollama, handled inside each provider.
                if getattr(provider, "supports_streaming", False):
                    full = ""
                    usage = None
                    finish = None
                    async for chunk in provider.stream(request):
                        if chunk.error:
                            raise RuntimeError(chunk.error)
                        if chunk.delta:
                            full += chunk.delta
                            # Stream incrementally via hooks so the transcript
                            # updates live.  ``_hooks().on_message`` writes dim text.
                            try:
                                cb = getattr(hooks, "on_message", None)
                                if callable(cb):
                                    cb(chunk.delta)
                            except Exception:  # noqa: BLE001
                                pass
                        if chunk.usage is not None:
                            usage = chunk.usage
                        if chunk.finish_reason is not None:
                            finish = chunk.finish_reason
                    # Record usage for the dashboard
                    from types import SimpleNamespace

                    return SimpleNamespace(output=full, content=full, usage=usage, finish_reason=finish, model=model)
                # Fallback to single complete
                response = await provider.complete(request)
                if response.error:
                    raise RuntimeError(response.error)
                from types import SimpleNamespace

                return SimpleNamespace(output=response.content, content=response.content, usage=response.usage, finish_reason=response.finish_reason, model=model, raw=response.raw)
            finally:
                provider.model = original_model
                try:
                    await provider.aclose()
                except Exception:  # noqa: BLE001
                    pass

        # Eagerly validate that we can actually build a provider with the current
        # config.  Without this, ``_ensure_runner`` would report success even when
        # the underlying ``build_provider`` will fail on the first real call (e.g.
        # missing API key, bad base_url).  That would make “Hola” hit the generic
        # agent-run error instead of the precise “backend attach failed” diagnostic.
        try:
            probe = _resolve_provider()
            if probe is None:
                # _resolve_provider already stored _attach_error
                if not getattr(self, "_attach_error", None):
                    self._attach_error = "could not build provider — check /setup"
                return None
            # Probe succeeded — close it if possible (best effort, sync fallback)
            try:
                # Most providers expose an async ``aclose``; we can't await here, so
                # schedule it if an event loop is running, otherwise ignore.
                import asyncio as _asyncio

                if _asyncio.get_event_loop().is_running():
                    _asyncio.create_task(probe.aclose())  # type: ignore[attr-defined]
                else:
                    # No running loop — provider will be re-created on first use.
                    pass
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            self._attach_error = str(exc)[:250]
            return None

        return _chat_runner

    def _ensure_runner(self) -> bool:
        """Ensure ``self._runner`` exists if a valid provider/model is configured.

        Called at startup, after ``/setup``, and lazily before the first chat.
        Returns True if a runner is now attached.
        """

        if self._runner is not None:
            self._attach_error = None
            return True
        can_attach, reason, details = self._diagnose_attachment()
        # ``can_attach`` True with “runner not yet attached” means we should try.
        if not can_attach:
            # Only attach when the diagnosis says we *can* (provider/model valid).
            # Otherwise leave ``_runner`` as None and let ``run_task`` show the
            # precise reason.
            if "runner not yet attached" not in reason:
                return False
            # If the only reason we cannot attach is a previous attach failure,
            # we *should* retry — the user may have fixed the config via /setup.
            if "backend attach failed" in reason and getattr(self, "_attach_error", None):
                pass  # fall through to retry
            else:
                # For the “will attach on demand” case, fall through to build.
                if "will attach on demand" not in reason:
                    return False
        try:
            runner = self._build_chat_runner()
            if runner is not None:
                self._runner = runner
                self._attach_error = None
                log.info("chat runner attached for %s/%s", details.get("provider"), details.get("model"))
                return True
            else:
                if not getattr(self, "_attach_error", None):
                    self._attach_error = "could not build provider — check /setup"
        except Exception as exc:  # noqa: BLE001
            self._attach_error = str(exc)[:200]
            log.debug("could not attach runner: %s", exc)
        return self._runner is not None

    @work(exclusive=True, group="modal")
    @safe_action("attach agent")
    async def action_attach_agent(self) -> None:
        """Manual recovery: try to attach a runner and re-check provider."""

        self.note("attaching agent…")
        self._reload_settings()
        self.provider_status.settings = self.settings
        attached = self._ensure_runner()
        self._resolve_health()
        await asyncio.sleep(0.1)
        if attached and self._runner is not None:
            self.note(f"agent attached — {self.provider_status.state.provider_label or self.provider_status.state.provider} · {self.provider_status.state.model}", tone="ok")
        else:
            can, reason, details = self._diagnose_attachment()
            self.note(f"still detached: {reason} — {details.get('hint','press /setup')}", tone="warn")

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
    """Launch the shell. Never raises for provider reasons — that is the contract.

    When ``runner`` is ``None`` but ``settings`` contains a valid provider/model,
    the ``TUIAppShell`` itself will auto-attach a streaming chat runner on
    construction (``_ensure_runner``).  This function also attempts an eager
    attach here so that callers inspecting ``app._runner`` immediately after
    construction — e.g. tests running in ``headless`` mode — see a live backend
    without waiting for ``on_mount``.
    """

    app = TUIAppShell(settings=settings, runner=runner, first_task=first_task, resolve_on_mount=resolve_on_mount)
    if app._runner is None and settings is not None:
        try:
            app._ensure_runner()
        except Exception:  # noqa: BLE001
            pass
    if headless:
        async with app.run_test(size=(150, 44)) as pilot:
            await pilot.pause()
        return app
    await app.run_async()
    return app
