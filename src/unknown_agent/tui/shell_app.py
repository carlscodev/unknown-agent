"""``TUIAppShell`` — the product shell: conversation, composer, command bar, dashboard.

Why this exists instead of an edit to :mod:`unknown_agent.tui.app`
------------------------------------------------------------------
The old ``AgentShell`` conflated three responsibilities that need different lifetimes:
drawing the UI, knowing the provider, and running the agent.  Because they were built
in one constructor, a provider failure during construction was a startup failure — the
HTTP 402 bug.  You cannot fix an ordering bug by reordering statements inside a
constructor that also does I/O; the boundary has to move.

So the shell now owns only local state, and the two remote concerns are injected as
*controllers* it observes:

    TUIAppShell            local: layout, transcript, composer, palette, history
      ├── ProviderStatusController   remote: health, resolved after first paint
      ├── UsageTracker               local accounting of remote calls
      └── runner (injected)          the agent itself, called only on submit

Startup contract, enforced by structure
---------------------------------------
``on_mount`` performs **no network I/O of any kind**.  It paints, focuses the composer,
and schedules :class:`StartupHealthResolver` as a Textual worker.  Every provider
outcome — 402, expired login, DNS failure, invalid key, a model that no longer exists —
arrives later as a state transition and renders in the sidebar.  The user can type,
scroll, read history, open the palette and reconfigure throughout.

Layout::

    ┌────────────────────────────────────────────────────────┬──────────────────┐
    │ ● ready   openrouter · claude-sonnet-4 · api key        │  provider        │
    ├────────────────────────────────────────────────────────┤  ● connected     │
    │ conversation                                           │  312 models      │
    │   you › build a task API                               ├──────────────────┤
    │   agent › planning…                                    │  context ███░░░░ │
    │                                                        ├──────────────────┤
    ├────────────────────────────────────────────────────────┤  session         │
    │ › ask, or /command                             [send]  │  in/out/cost     │
    ├────────────────────────────────────────────────────────┤                  │
    │ ctrl+p palette · /model · /provider · /mode · f1 help   │  agent: idle     │
    └────────────────────────────────────────────────────────┴──────────────────┘
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, RichLog, Static

from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.health import ProviderStatus
from unknown_agent.tui.dashboard import AgentStateCard, ContextGauge, ProviderStatusCard, SessionMetricsPanel, status_dot
from unknown_agent.tui.palette import (
    Command,
    CommandPalette,
    CommandRegistry,
    ModeSelectorPanel,
    ModelSelectorPanel,
    ProviderSwitcher,
)
from unknown_agent.tui.provider_status import (
    ProviderState,
    ProviderStatusController,
    StartupHealthResolver,
    active_profile_name,
)
from unknown_agent.tui.themes import apply_to_app
from unknown_agent.tui.usage import UsageTracker
from unknown_agent.ui.theme_defs import get_palette

log = get_logger("tui.shell")

__all__ = ["TUIAppShell", "ErrorBoundary", "run_app_shell"]

RunnerCallable = Callable[[str, Any], Awaitable[Any]]


class ErrorBoundary:
    """Converts an exception into UI state instead of a dead application.

    Used around every call into provider or agent code.  The rule it enforces: a
    failure in remote work degrades a *region* of the UI, and the shell keeps running.
    """

    def __init__(self, shell: "TUIAppShell", *, region: str) -> None:
        self.shell = shell
        self.region = region
        self.error: BaseException | None = None

    def __enter__(self) -> "ErrorBoundary":
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        if exc is None:
            return False
        self.error = exc
        log.debug("error boundary caught in %s", self.region, exc_info=(exc_type, exc, tb))
        try:
            self.shell.handle_degradation(exc, region=self.region)
        except Exception:  # noqa: BLE001 - the boundary itself must never raise
            log.debug("error boundary handler failed", exc_info=True)
        return True  # swallowed: the shell lives


class TUIAppShell(App[None]):
    """The agent shell.  Opens instantly; provider health arrives later."""

    CSS = """
    Screen { layers: base overlay; background: $background; }

    #shell-body { height: 1fr; }

    #main { width: 3fr; height: 1fr; }
    #sidebar {
        width: 34; height: 1fr; background: $panel;
        border-left: solid $accent 30%; padding: 1 1;
    }

    #statusbar {
        height: 1; padding: 0 2; background: $boost; color: $text;
    }

    #conversation { height: 1fr; padding: 0 2; background: $background; scrollbar-size: 1 1; }

    #composer-row { height: 3; padding: 0 1; background: $panel; }
    #composer { border: round $accent 40%; background: $surface; }
    #composer:focus { border: round $accent; }
    #send-button { min-width: 8; }

    #alert {
        height: auto; max-height: 7; padding: 1 2; display: none;
        border-left: thick $error; background: $panel;
    }
    #alert.visible { display: block; }

    .card { height: auto; padding: 0 0 1 0; }
    #sidebar > .card { border-bottom: solid $accent 12%; margin-bottom: 1; }
    """

    BINDINGS = [
        Binding("ctrl+p", "command_palette", "palette"),
        Binding("ctrl+q", "quit", "quit"),
        Binding("f2", "switch_model", "model"),
        Binding("f3", "switch_provider", "provider"),
        Binding("f4", "switch_mode", "mode"),
        Binding("ctrl+r", "retry_provider", "retry"),
        Binding("f1", "show_help", "help"),
    ]

    def __init__(
        self,
        *,
        settings: Any = None,
        runner: RunnerCallable | None = None,
        first_task: str = "",
        palette: str | None = None,
        controller: ProviderStatusController | None = None,
        usage: UsageTracker | None = None,
        resolve_on_mount: bool = True,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._runner = runner
        self.first_task = first_task
        self.palette_key = palette or getattr(getattr(settings, "appearance", None), "theme", None) or "auto"

        # Both controllers are constructed from *local* data only. Nothing here can
        # touch the network, so nothing here can fail because of a provider.
        self.provider_status = controller or ProviderStatusController(settings)
        self.usage = usage or UsageTracker(context_window=self.provider_status.state.context_window)
        self.commands = CommandRegistry()
        self.resolve_on_mount = resolve_on_mount

        self.agent_state: dict[str, Any] = {"state": "idle", "mode": self._reasoning_mode_name(), "steps": ""}
        self.busy = False
        self.started_at = time.time()
        self._history: list[str] = []
        self._models_cache: list[Any] = []
        self._register_commands()

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Static("", id="statusbar")
        with Horizontal(id="shell-body"):
            with Vertical(id="main"):
                yield Static("", id="alert")
                yield RichLog(id="conversation", wrap=True, markup=True, highlight=True, min_width=20)
                with Horizontal(id="composer-row"):
                    yield Input(placeholder="ask, build, or type / for commands", id="composer")
                    yield Button("send", id="send-button", variant="primary")
            with VerticalScroll(id="sidebar"):
                yield ProviderStatusCard(palette_key=self.palette_key, id="card-provider", classes="card")
                yield ContextGauge(palette_key=self.palette_key, id="card-context", classes="card")
                yield SessionMetricsPanel(palette_key=self.palette_key, id="card-usage", classes="card")
                yield AgentStateCard(palette_key=self.palette_key, id="card-agent", classes="card")
        yield Footer()

    def on_mount(self) -> None:
        """Paint, focus, schedule.  **No I/O happens in this method** — that is the fix.

        The provider is not contacted, no credential is read, no model list is fetched.
        Whatever is wrong with the account, the shell is already usable by the time the
        health worker starts.
        """

        apply_to_app(self, None if self.palette_key == "auto" else self.palette_key)
        self.title = "unknown agent"

        # Render everything from local state first.
        self.provider_status.subscribe(self._on_provider_state)
        self._refresh_sidebar()
        self._welcome()

        composer = self.query_one("#composer", Input)
        if self.first_task:
            composer.value = self.first_task
        composer.focus()

        if self.resolve_on_mount:
            self._resolve_health()

    # ------------------------------------------------------------------ #
    # background health (the decoupled part)
    # ------------------------------------------------------------------ #
    @work(exclusive=True, group="health")
    async def _resolve_health(self) -> None:
        """Resolve provider health off the UI thread.  Cannot fail the app.

        :class:`StartupHealthResolver` already swallows everything; the extra guard
        here covers cancellation and teardown races so a quit during validation never
        surfaces as a traceback.
        """

        try:
            await StartupHealthResolver(self.provider_status).run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("health worker failed", exc_info=True)

    def _on_provider_state(self, state: ProviderState) -> None:
        """Push a health transition into the widgets.  Called from any thread."""

        try:
            self.call_from_thread(self._apply_provider_state, state)
        except Exception:  # noqa: BLE001 - before mount / after unmount there is no UI yet
            try:
                self._apply_provider_state(state)
            except Exception:  # noqa: BLE001
                log.debug("could not render provider state", exc_info=True)

    def _apply_provider_state(self, state: ProviderState) -> None:
        if not self.is_running:
            return
        try:
            self.query_one("#card-provider", ProviderStatusCard).state = state
        except Exception:  # noqa: BLE001 - widget may not exist yet
            return
        if state.context_window:
            self.usage.set_context_window(state.context_window)
        self._render_statusbar(state)
        self._announce(state)

    def _announce(self, state: ProviderState) -> None:
        """Put a persistent, actionable banner above the conversation when degraded.

        This is where a 402 lands.  It is a coloured strip with the remedies next to
        it — not an exception, not a modal, and above all not a reason to exit.
        """

        try:
            alert = self.query_one("#alert", Static)
        except Exception:  # noqa: BLE001
            return

        if state.ok or state.status == ProviderStatus.VALIDATING:
            alert.remove_class("visible")
            alert.update("")
            return

        palette = get_palette(None if self.palette_key == "auto" else self.palette_key)
        colour = {
            ProviderStatus.CREDITS_EXHAUSTED: palette.color("err"),
            ProviderStatus.INVALID_CREDENTIALS: palette.color("err"),
            ProviderStatus.OFFLINE: palette.color("err"),
        }.get(state.status, palette.color("warn"))

        body = Text()
        body.append(f"{ProviderStatus.label(state.status)}  ", style=f"bold {colour}")
        body.append(state.message or "", style=palette.color("text"))
        if state.hint:
            body.append("\n")
            body.append(state.hint, style=palette.color("text_dim"))
        if state.remedies:
            body.append("\n")
            body.append("ctrl+r retry · f3 provider · f2 model · ctrl+p palette", style=palette.color("brand"))
        alert.update(body)
        alert.add_class("visible")

    def handle_degradation(self, error: BaseException, *, region: str) -> None:
        """Entry point used by :class:`ErrorBoundary`: turn a crash into a status."""

        if isinstance(error, Exception):
            self.provider_status.note_runtime_error(error)
        self.write_line(f"[dim]{region} failed: {str(error)[:200]}[/dim]")

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    @property
    def conversation(self) -> RichLog:
        return self.query_one("#conversation", RichLog)

    def write_line(self, renderable: Any) -> None:
        try:
            self.conversation.write(renderable)
        except Exception:  # noqa: BLE001 - never let logging kill the shell
            log.debug("could not write to conversation", exc_info=True)

    def _welcome(self) -> None:
        palette = get_palette(None if self.palette_key == "auto" else self.palette_key)
        head = Text()
        head.append("unknown agent", style=f"bold {palette.color('brand')}")
        head.append("   the shell is local — it works whatever the provider is doing\n", style=palette.color("text_dim"))
        head.append("ctrl+p", style=f"bold {palette.color('text')}")
        head.append(" command palette   ", style=palette.color("text_dim"))
        head.append("/model /provider /mode", style=f"bold {palette.color('text')}")
        head.append(" switchers", style=palette.color("text_dim"))
        self.write_line(head)

    def _render_statusbar(self, state: ProviderState | None = None) -> None:
        state = state or self.provider_status.state
        palette = get_palette(None if self.palette_key == "auto" else self.palette_key)
        usage = self.usage.snapshot()
        bar = Text()
        bar.append_text(status_dot(state.status, palette))
        bar.append("   ")
        bar.append(state.summary(), style=palette.color("text"))
        if usage.calls:
            bar.append("   ")
            bar.append(usage.tokens_text(), style=palette.color("text_dim"))
            bar.append("   ")
            bar.append(usage.cost_text(), style=palette.color("brand"))
        if self.busy:
            bar.append("   working…", style=palette.color("warn"))
        try:
            self.query_one("#statusbar", Static).update(bar)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_sidebar(self) -> None:
        snapshot = self.usage.snapshot()
        try:
            self.query_one("#card-provider", ProviderStatusCard).state = self.provider_status.state
            self.query_one("#card-context", ContextGauge).usage = snapshot
            self.query_one("#card-usage", SessionMetricsPanel).usage = snapshot
            self.agent_state["elapsed"] = f"{int(time.time() - self.started_at)}s"
            self.query_one("#card-agent", AgentStateCard).info = dict(self.agent_state)
        except Exception:  # noqa: BLE001 - during teardown widgets disappear
            return
        self._render_statusbar()

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def _register_commands(self) -> None:
        self.commands.extend(
            [
                Command("model", "Switch model", "choose from this provider's models", "provider", "f2", "model",
                        handler=lambda: self.action_switch_model()),
                Command("provider", "Switch provider", "change the active profile", "provider", "f3", "provider",
                        handler=lambda: self.action_switch_provider()),
                Command("mode", "Reasoning mode", "trade cost against deliberation", "agent", "f4", "mode",
                        handler=lambda: self.action_switch_mode()),
                Command("setup", "Run provider setup", "configure auth for a provider", "provider", "", "setup",
                        handler=lambda: self.action_run_setup()),
                Command("retry", "Retry provider", "re-check provider health now", "provider", "ctrl+r", "retry",
                        handler=lambda: self.action_retry_provider()),
                Command("status", "Show provider status", "full health report", "provider", "", "status",
                        handler=lambda: self.action_show_status()),
                Command("usage", "Show session usage", "tokens and cost so far", "session", "", "usage",
                        handler=lambda: self.action_show_usage()),
                Command("clear", "Clear conversation", "empty the transcript", "session", "", "clear",
                        handler=lambda: self.action_clear()),
                Command("help", "Help", "keys and slash commands", "general", "f1", "help",
                        handler=lambda: self.action_show_help()),
                Command("quit", "Quit", "leave the shell", "general", "ctrl+q", "quit",
                        handler=lambda: self.exit()),
            ]
        )

    async def action_command_palette(self) -> None:
        chosen = await self.push_screen_wait(CommandPalette(self.commands))
        if chosen is not None and chosen.handler is not None:
            result = chosen.handler()
            if asyncio.iscoroutine(result):
                await result

    # -- switchers ----------------------------------------------------- #
    async def action_switch_model(self) -> None:
        """Offer this provider's models.

        Fetching the live list may fail — no credit, no network, expired key.  That is
        caught and the curated catalog is offered instead, because "you cannot change
        model" is the worst possible response to "your provider is broken".
        """

        models, source = await self._available_models()
        if not models:
            self.write_line("[dim]no models to choose from; run /setup to configure a provider[/dim]")
            return
        chosen = await self.push_screen_wait(
            ModelSelectorPanel(models, current=self.provider_status.state.model, source=source)
        )
        if not chosen:
            return
        self._persist_choice(model=chosen)
        self.provider_status.note_switch(model=chosen)
        self.write_line(f"[bold]model → {chosen}[/bold]")
        self._resolve_health()

    async def _available_models(self) -> tuple[list[Any], str]:
        """Live list if the provider will give one, curated catalog otherwise."""

        from unknown_agent.providers.catalog import known_model_ids, resolve_entry
        from unknown_agent.providers.discovery import ModelInfo

        profile = self._profile()
        entry = resolve_entry(getattr(profile, "type", "") or self.provider_status.state.provider)
        if entry is None:
            return [], "catalog"

        report = self.provider_status.last_report
        if report is not None and report.models:
            self.usage.learn_pricing_from_models(report.models)
            return list(report.models), "live"

        try:
            from unknown_agent.providers.discovery import fetch_models

            key = await asyncio.to_thread(ProviderStatusController._resolve_key, profile, entry)
            models = list(await asyncio.wait_for(fetch_models(entry, api_key=key, base_url=getattr(profile, "base_url", None)), timeout=15))
            self.usage.learn_pricing_from_models(models)
            return models, "live"
        except Exception as exc:  # noqa: BLE001 - degrade to the catalog, never block
            log.debug("live model listing failed: %s", exc)
            return [ModelInfo(id=mid, name=mid, provider=entry.key) for mid in known_model_ids(entry)], "catalog"

    async def action_switch_provider(self) -> None:
        profiles = self._configured_profiles()
        chosen = await self.push_screen_wait(ProviderSwitcher(profiles, current=self.provider_status.state.provider))
        if not chosen:
            return
        if chosen == "__setup__":
            await self.action_run_setup()
            return
        self._persist_choice(provider=chosen)
        self.provider_status.note_switch(provider=chosen)
        self.write_line(f"[bold]provider → {chosen}[/bold]")
        self._resolve_health()

    def _configured_profiles(self) -> list[Any]:
        from types import SimpleNamespace

        profiles = getattr(self.settings, "providers", {}) or {}
        rows = []
        for name, profile in (profiles.items() if isinstance(profiles, dict) else []):
            rows.append(
                SimpleNamespace(
                    name=name,
                    detail=getattr(profile, "model", "") or "",
                    badge=(getattr(profile, "auth_mode", "") or getattr(profile, "type", "") or ""),
                )
            )
        return rows

    async def action_switch_mode(self) -> None:
        chosen = await self.push_screen_wait(ModeSelectorPanel(current=self._reasoning_mode_name()))
        if not chosen:
            return
        with ErrorBoundary(self, region="mode switch"):
            reasoning = getattr(self.settings, "reasoning", None)
            if reasoning is not None:
                reasoning.default_mode = chosen  # type: ignore[assignment]
        self.agent_state["mode"] = chosen
        self.write_line(f"[bold]reasoning → {chosen}[/bold]")
        self._refresh_sidebar()

    def _reasoning_mode_name(self) -> str:
        return str(getattr(getattr(self.settings, "reasoning", None), "default_mode", "") or "default")

    async def action_run_setup(self) -> None:
        """Hand off to the setup panel and reload config when it returns.

        The panel is a Textual app of its own, so the shell is suspended for its
        duration (two apps cannot drive one terminal) and resumed afterwards. It runs
        on a worker thread with its own event loop; failures are contained.
        """

        self.write_line("[dim]opening provider setup…[/dim]")
        with ErrorBoundary(self, region="setup"):
            from unknown_agent.tui.quicksetup import run_setup_panel

            def _run() -> Any:
                return asyncio.run(run_setup_panel())

            with self.suspend():
                result = await asyncio.to_thread(_run)
            if result is not None and not getattr(result, "cancelled", True):
                self._reload_settings()
                self.provider_status.note_switch(
                    provider=getattr(result, "provider", ""), model=getattr(result, "model", "")
                )
                self.write_line(f"[bold]configured {getattr(result, 'provider', '')}[/bold]")
                self._resolve_health()
            else:
                self.write_line("[dim]setup cancelled[/dim]")

    def _reload_settings(self) -> None:
        with ErrorBoundary(self, region="config reload"):
            from unknown_agent.config.loader import load_settings

            self.settings = load_settings().settings
            self.provider_status.settings = self.settings

    def action_retry_provider(self) -> None:
        self.write_line("[dim]re-checking the provider…[/dim]")
        self._resolve_health()

    def action_show_status(self) -> None:
        """Print the full health report: every stage, with what failed and the fix."""

        report = self.provider_status.last_report
        state = self.provider_status.state
        palette = get_palette(None if self.palette_key == "auto" else self.palette_key)
        body = Text()
        body.append_text(status_dot(state.status, palette))
        body.append(f"  {state.summary()}\n", style=palette.color("text"))
        if state.message:
            body.append(f"{state.message}\n", style=palette.color("text_dim"))
        if report is not None:
            for stage in report.stages:
                glyph = {"ok": "✓", "fail": "✕", "warn": "▲", "skip": "–"}.get(stage.state, "·")
                colour = {"ok": "ok", "fail": "err", "warn": "warn"}.get(stage.state, "text_dim")
                body.append(f"  {glyph} {stage.name}: ", style=palette.color(colour))
                body.append(f"{stage.detail}\n", style=palette.color("text_dim"))
                if stage.fix:
                    body.append(f"      fix: {stage.fix}\n", style=palette.color("text_dim"))
        self.write_line(body)

    def action_show_usage(self) -> None:
        snapshot = self.usage.snapshot()
        body = Text()
        body.append("session usage\n", style="bold")
        body.append(f"  {snapshot.tokens_text()}  ·  {snapshot.cost_text()}  ·  {snapshot.calls} call(s)\n", style="dim")
        if snapshot.context_window:
            body.append(f"  context {snapshot.context_tokens:,}/{snapshot.context_window:,} ({snapshot.context_pct:.0f}%)\n", style="dim")
        for item in snapshot.by_model:
            body.append(f"  {item.model}: {item.total_tokens:,} tokens\n", style="dim")
        if not snapshot.fully_measured:
            body.append("  figures marked ~ are estimated from text length\n", style="dim")
        self.write_line(body)

    def action_clear(self) -> None:
        with ErrorBoundary(self, region="clear"):
            self.conversation.clear()
            self._welcome()

    def action_show_help(self) -> None:
        body = Text()
        body.append("keys\n", style="bold")
        for shortcut, label in (
            ("ctrl+p", "command palette"), ("f2", "switch model"), ("f3", "switch provider"),
            ("f4", "reasoning mode"), ("ctrl+r", "retry the provider"), ("ctrl+q", "quit"),
        ):
            body.append(f"  {shortcut:<10}{label}\n", style="dim")
        body.append("slash commands\n", style="bold")
        body.append("  " + "  ".join(f"/{item.slash}" for item in self.commands.all() if item.slash) + "\n", style="dim")
        self.write_line(body)

    action_show_help.__doc__ = "Keys and slash commands."

    # ------------------------------------------------------------------ #
    # input
    # ------------------------------------------------------------------ #
    @on(Button.Pressed, "#send-button")
    def _send_pressed(self) -> None:
        self._submit(self.query_one("#composer", Input).value)

    @on(Input.Submitted, "#composer")
    def _composer_submitted(self, event: Input.Submitted) -> None:
        self._submit(event.value)

    def _submit(self, raw: str) -> None:
        text = (raw or "").strip()
        if not text:
            return
        self.query_one("#composer", Input).value = ""
        if text.startswith("/"):
            self._run_slash(text)
            return
        self._history.append(text)
        self.run_task(text)

    def _run_slash(self, text: str) -> None:
        name = text[1:].split()[0] if len(text) > 1 else ""
        command = self.commands.by_slash(name)
        if command is None:
            self.write_line(f"[dim]unknown command /{name} — ctrl+p lists everything[/dim]")
            return
        if command.handler is not None:
            result = command.handler()
            if asyncio.iscoroutine(result):
                self.run_worker(result, group="command")

    @work(exclusive=True, group="task")
    async def run_task(self, task: str) -> None:
        """Run one task.

        Everything remote happens inside the boundary, so a provider that dies
        mid-run — including a 402 raised on the first token — becomes a status change
        and a transcript line, and the shell stays interactive.
        """

        palette = get_palette(None if self.palette_key == "auto" else self.palette_key)
        prompt = Text()
        prompt.append("you › ", style=f"bold {palette.color('brand')}")
        prompt.append(task, style=palette.color("text"))
        self.write_line(prompt)

        if self._runner is None:
            self.write_line("[dim]no runner attached (UI-only session)[/dim]")
            return

        state = self.provider_status.state
        if not state.can_send:
            self.write_line(f"[dim]{state.message or 'no provider configured'} — press f3 or ctrl+p → setup[/dim]")
            return

        self.busy = True
        self.agent_state["state"] = "running"
        self._refresh_sidebar()
        try:
            with ErrorBoundary(self, region="agent run"):
                result = await self._runner(task, self._hooks())
                self._record_usage(result)
                self.write_line(self._format_result(result))
        finally:
            self.busy = False
            self.agent_state["state"] = "idle"
            self._refresh_sidebar()

    def _hooks(self) -> Any:
        """Minimal hook object the runner can report progress through."""

        from types import SimpleNamespace

        def on_step(label: str = "", **_: Any) -> None:
            self.agent_state["steps"] = label or self.agent_state.get("steps", "")

        def on_message(text: str = "", **_: Any) -> None:
            self.write_line(Text(str(text)))

        return SimpleNamespace(on_step=on_step, on_message=on_message, on_tool=lambda *a, **k: None)

    def _record_usage(self, result: Any) -> None:
        """Book a run's tokens, measured if the provider told us, estimated if not."""

        with ErrorBoundary(self, region="usage accounting"):
            model = self.provider_status.state.model
            provider = self.provider_status.state.provider
            usage = getattr(result, "usage", None)
            if usage is not None:
                self.usage.record_from_payload(usage, model=model, provider=provider)
            else:
                self.usage.record_estimated(
                    model=model, provider=provider, prompt="", completion=str(getattr(result, "output", result) or "")
                )
            snapshot = self.usage.snapshot()
            self.usage.set_context_tokens(snapshot.total_tokens)

    def _format_result(self, result: Any) -> Any:
        palette = get_palette(None if self.palette_key == "auto" else self.palette_key)
        body = Text()
        body.append("agent › ", style=f"bold {palette.color('success')}")
        body.append(str(getattr(result, "output", None) or getattr(result, "summary", None) or result))
        return body

    # ------------------------------------------------------------------ #
    # config writes
    # ------------------------------------------------------------------ #
    def _profile(self) -> Any:
        profiles = getattr(self.settings, "providers", {}) or {}
        name = active_profile_name(self.settings)
        return profiles.get(name) if isinstance(profiles, dict) else None

    def _persist_choice(self, *, provider: str = "", model: str = "") -> None:
        """Write a switch to disk so it survives the session.

        A failed write is reported, not raised: the in-memory switch still stands for
        this session, which is better than refusing to switch at all.
        """

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
    """Launch the shell.  Never raises for provider reasons — that is the contract."""

    app = TUIAppShell(
        settings=settings, runner=runner, first_task=first_task, resolve_on_mount=resolve_on_mount
    )
    if headless:
        async with app.run_test(size=(150, 44)) as pilot:
            await pilot.pause()
        return app
    await app.run_async()
    return app
