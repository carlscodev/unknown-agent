"""The main environment: a persistent agent shell, not a REPL with decoration.

``ua`` lands here once setup is valid.  The shell exists to make three things
visible that a scrolling transcript hides:

* **what the agent intends**  — the plan panel, with the definition of done;
* **what it is doing**        — the workers panel during an orchestrated run, with
  each worker's state, attempt count and dependencies;
* **what it proved**          — the gates panel, where every claim is either a
  passing check or an admission that it was not checked.

Layout::

    ┌──────────────┬───────────────────────────────────┬──────────────────────┐
    │ status rail  │ transcript                        │ plan │ workers │ gates │ tools │
    │              │                                   │                      │
    │ profile      │  you › build a task API           │  ▸ inspect repo      │
    │ model        │  agent › planning …               │  ▸ implement         │
    │ reasoning    │  tool  write_file src/api.py ✓    │  ▸ run gates         │
    │ budget       │                                   │                      │
    │              ├───────────────────────────────────┤                      │
    │              │ › compose                         │                      │
    └──────────────┴───────────────────────────────────┴──────────────────────┘

The core loop is not imported for its side effects: the shell drives it through
:class:`unknown_agent.core.loop.LoopHooks` and an injected ``runner`` callable, so
the same screen works with the real agent, with an orchestrated conductor, or with
a deterministic stub in tests.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, RichLog, Static, TabbedContent, TabPane
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from unknown_agent.core.loop import LoopHooks
from unknown_agent.observability.logging import get_logger
from unknown_agent.orchestration.conductor import OrchestrationOutcome, analyze_task
from unknown_agent.orchestration.ledger import Ledger
from unknown_agent.orchestration.worker import OfflineExecutor
from unknown_agent.tui.themes import apply_to_app, theme_name
from unknown_agent.tui.widgets import CapabilityBadges, KeyHints, OrchestrationBoard, PanelCard, StepRail
from unknown_agent.ui.theme_defs import get_palette

__all__ = ["AgentShell", "ShellBridge", "run_shell", "StatusSnapshot", "ComposerMessage"]


# --------------------------------------------------------------------------- #
# bridge: core hooks -> Textual messages
# --------------------------------------------------------------------------- #
class ComposerMessage(Message):
    """The user submitted a task."""

    def __init__(self, text: str, kind: str = "run") -> None:
        super().__init__()
        self.text = text
        self.kind = kind

    @property
    def control(self) -> Widget | None:
        return self._sender  # type: ignore[attr-defined,return-value]


@dataclass
class StatusSnapshot:
    """Everything the status rail shows, refreshed on change."""

    profile: str = "—"
    provider: str = "—"
    model: str = "—"
    reasoning: str = "—"
    planning: str = "—"
    tools: int = 0
    skills: int = 0
    budget_tokens: int = 0
    tokens_used: int = 0
    steps_used: int = 0
    steps_max: int = 0
    browser: bool = False
    orchestration: str = "auto"
    lines: list[str] = field(default_factory=list)

    def render(self) -> Text:
        text = Text()

        def row(label: str, value: Any, *, style: str = "") -> None:
            text.append(f"{label:<10}", style="dim")
            text.append(f"{value}", style=style or "")
            text.append("\n")

        row("profile", self.profile, style="bold")
        row("provider", self.provider)
        row("model", self.model, style="bold")
        row("reasoning", self.reasoning, style="magenta")
        row("planning", self.planning)
        row("tools", f"{self.tools} granted")
        row("skills", f"{self.skills} active")
        row("orchestration", self.orchestration)
        row("browser", "on" if self.browser else "off", style="green" if self.browser else "dim")
        if self.steps_max:
            text.append(f"\nsteps {self.steps_used}/{self.steps_max}\n", style="dim")
        if self.budget_tokens:
            share = min(1.0, self.tokens_used / self.budget_tokens) if self.budget_tokens else 0.0
            bar_width = 22
            filled = int(share * bar_width)
            style = "green" if share < 0.6 else ("yellow" if share < 0.85 else "red")
            text.append("tokens ", style="dim")
            text.append("█" * filled + "░" * (bar_width - filled), style=style)
            text.append(f"\n{self.tokens_used:,} / {self.budget_tokens:,}\n", style="dim")
        for line in self.lines:
            text.append(f"{line}\n", style="dim")
        return text


class ShellBridge:
    """Adapts :class:`LoopHooks` onto an app's message queue.

    The core loop runs in a worker thread/task and knows nothing about Textual;
    this object is the only place where the two meet.  Every hook posts a message
    rather than touching a widget, which keeps rendering on the app's own thread.
    """

    def __init__(self, app: "AgentShell") -> None:
        self.app = app

    def hooks(self) -> LoopHooks:
        return LoopHooks(
            on_event=self._on_event,
            on_thought=self._on_thought,
            on_tool_start=self._on_tool_start,
            on_tool_end=self._on_tool_end,
            on_verification=self._on_verification,
            on_message=self._on_message,
            should_stop=self.app.should_stop,
            request_plan_approval=self.app.request_plan_approval,
        )

    async def _on_event(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        name = getattr(event, "name", None) or getattr(event, "kind", "event")
        self.app.post_message(self.app.CoreEvent(str(name), payload if isinstance(payload, dict) else {}))

    def _on_thought(self, text: str) -> None:
        self.app.post_message(self.app.Thought(text))

    def _on_tool_start(self, call: Any) -> None:
        self.app.post_message(
            self.app.ToolStarted(getattr(call, "name", "?"), dict(getattr(call, "arguments", {}) or {}), getattr(call, "id", ""))
        )

    def _on_tool_end(self, call: Any, result: Any) -> None:
        self.app.post_message(
            self.app.ToolFinished(
                getattr(call, "name", "?"),
                bool(getattr(result, "ok", True)),
                str(getattr(result, "summary", "") or getattr(result, "output", "") or "")[:400],
                getattr(call, "id", ""),
            )
        )

    def _on_verification(self, report: Any) -> None:
        self.app.post_message(self.app.Verified(report))

    def _on_message(self, text: str) -> None:
        self.app.post_message(self.app.AgentSaid(text))


# --------------------------------------------------------------------------- #
# the shell
# --------------------------------------------------------------------------- #
RunnerCallable = Callable[[str, LoopHooks], Awaitable[Any]]


class AgentShell(App[None]):
    """The persistent workspace."""

    TITLE = "Unknown Agent"
    SUB_TITLE = "hyperagent"

    CSS = """
    Screen { layout: horizontal; }
    #status-rail {
        width: 34;
        height: 100%;
        padding: 1;
        background: $surface;
        border-right: round $panel-lighten-2 50%;
    }
    #status-rail .rail-title {
        color: $primary;
        text-style: bold;
        height: 1;
        width: 100%;
    }
    #status-rail .rail-body {
        height: auto;
        width: 100%;
        margin-top: 1;
    }
    #status-rail .rail-hint {
        height: auto;
        width: 100%;
        color: $text-muted;
        margin-top: 1;
    }
    #centre { width: 1fr; height: 100%; }
    #transcript {
        height: 1fr;
        width: 100%;
        padding: 0 1;
        background: $surface-darken-1;
        scrollbar-size: 1 1;
    }
    #composer-row {
        dock: bottom;
        height: 3;
        width: 100%;
        padding: 0 1;
        align: left middle;
        background: $surface;
        border-top: round $panel-lighten-2 60%;
    }
    #composer { width: 1fr; }
    #composer-row Button { margin-left: 1; }
    #side {
        width: 52;
        height: 100%;
        border-left: round $panel-lighten-2 50%;
        background: $surface;
    }
    #side TabbedContent { height: 1fr; }
    #side Tabs { background: $surface; }
    .pane { height: auto; width: 100%; padding: 0 1; }
    #run-banner {
        dock: top;
        height: 1;
        width: 100%;
        padding: 0 1;
        background: $primary 20%;
        color: $text;
        display: none;
    }
    #run-banner.visible { display: block; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        Binding("ctrl+l", "focus_composer", "Compose"),
        Binding("ctrl+k", "clear_transcript", "Clear"),
        Binding("ctrl+p", "toggle_plan", "Plan"),
        Binding("ctrl+b", "cycle_reasoning", "Reasoning"),
        Binding("ctrl+o", "toggle_orchestration", "Orchestrate"),
        Binding("escape", "interrupt", "Interrupt", priority=True),
        Binding("question_mark", "help", "Help", show=False),
    ]

    # -- messages ---------------------------------------------------------- #
    class CoreEvent(Message):
        def __init__(self, name: str, payload: dict[str, Any]) -> None:
            super().__init__()
            self.name = name
            self.payload = payload

        @property
        def control(self) -> Widget | None:
            return self._sender  # type: ignore[attr-defined,return-value]

    class Thought(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

        @property
        def control(self) -> Widget | None:
            return self._sender  # type: ignore[attr-defined,return-value]

    class AgentSaid(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

        @property
        def control(self) -> Widget | None:
            return self._sender  # type: ignore[attr-defined,return-value]

    class ToolStarted(Message):
        def __init__(self, name: str, arguments: dict[str, Any], call_id: str = "") -> None:
            super().__init__()
            self.name = name
            self.arguments = arguments
            self.call_id = call_id

        @property
        def control(self) -> Widget | None:
            return self._sender  # type: ignore[attr-defined,return-value]

    class ToolFinished(Message):
        def __init__(self, name: str, ok: bool, summary: str, call_id: str = "") -> None:
            super().__init__()
            self.name = name
            self.ok = ok
            self.summary = summary
            self.call_id = call_id

        @property
        def control(self) -> Widget | None:
            return self._sender  # type: ignore[attr-defined,return-value]

    class Verified(Message):
        def __init__(self, report: Any) -> None:
            super().__init__()
            self.report = report

        @property
        def control(self) -> Widget | None:
            return self._sender  # type: ignore[attr-defined,return-value]

    class RunFinished(Message):
        def __init__(self, result: Any) -> None:
            super().__init__()
            self.result = result

        @property
        def control(self) -> Widget | None:
            return self._sender  # type: ignore[attr-defined,return-value]

    # -- construction ------------------------------------------------------ #
    def __init__(
        self,
        *,
        settings: Any = None,
        runner: RunnerCallable | None = None,
        status: StatusSnapshot | None = None,
        palette: str | None = None,
        ledger: Ledger | None = None,
        first_task: str = "",
    ) -> None:
        super().__init__()
        self.settings = settings
        self._runner = runner
        self.status_snapshot = status or _status_from_settings(settings)
        self.palette_key = palette or getattr(getattr(settings, "appearance", None), "theme", None) or "auto"
        self.ledger = ledger
        self.first_task = first_task
        self.running = False
        self._stop = threading.Event()
        self._approval: threading.Event | None = None
        self._approval_decision = False
        self._pending_plan: Any = None
        self.tool_log: list[tuple[str, bool, str]] = []
        self.started_at = 0.0

    # -- layout ------------------------------------------------------------ #
    def on_mount(self) -> None:
        apply_to_app(self, None if self.palette_key == "auto" else self.palette_key)
        self.sub_title = self.status_snapshot.model or "no model"
        self._render_status()
        self._welcome()
        composer = self.query_one("#composer", Input)
        if self.first_task:
            composer.value = self.first_task
            composer.action_end()
        # The shell is a workspace: the cursor belongs in the composer, always.
        composer.focus()

    def compose(self) -> ComposeResult:
        with Vertical(id="status-rail"):
            yield Static("unknown agent", classes="rail-title")
            yield Static("", classes="rail-body", id="rail-body")
            yield Static("", classes="rail-hint", id="rail-hint")
        with Vertical(id="centre"):
            yield Static("", id="run-banner")
            yield RichLog(id="transcript", highlight=True, markup=True, wrap=True, min_width=20)
            with Horizontal(id="composer-row"):
                yield Input(placeholder="describe what to build — Enter to run, Shift+Enter for a newline", id="composer")
                yield Button("run", id="run-button", variant="primary")
                yield Button("plan", id="plan-button")
        with Vertical(id="side"):
            # ``initial`` and ``active`` take the *pane* id; the tab id Textual derives
            # from it is an implementation detail we should not hardcode.
            with TabbedContent(initial="tab-plan", id="side-tabs"):
                with TabPane("plan", id="tab-plan"):
                    yield Static("", classes="pane", id="plan-pane")
                with TabPane("workers", id="tab-workers"):
                    yield OrchestrationBoard(id="worker-pane")
                with TabPane("gates", id="tab-gates"):
                    yield Static("no gates run yet", classes="pane", id="gate-pane")
                with TabPane("tools", id="tab-tools"):
                    yield Static("no tool calls yet", classes="pane", id="tool-pane")
        yield Footer()

    # -- rendering --------------------------------------------------------- #
    @property
    def transcript(self) -> RichLog:
        return self.query_one("#transcript", RichLog)

    def _welcome(self) -> None:
        palette = get_palette(None if self.palette_key == "auto" else self.palette_key)
        head = Text()
        head.append("unknown agent", style=f"bold {palette.color('brand')}")
        head.append("  ·  ", style="dim")
        head.append(f"{self.status_snapshot.provider} / {self.status_snapshot.model}", style=palette.color("text_dim"))
        self.transcript.write(head)
        self.transcript.write(
            Text(
                "Describe the software you want. The agent inspects the repo, plans, implements, and proves the result "
                "with gates before it claims success.",
                style="dim",
            )
        )
        self.transcript.write(Text(""))

    def _render_status(self) -> None:
        self.query_one("#rail-body", Static).update(self.status_snapshot.render())
        hints = [
            ("Enter", "run"),
            ("ctrl+p", "plan"),
            ("ctrl+o", "orchestrate"),
            ("ctrl+b", "reasoning"),
            ("ctrl+k", "clear"),
            ("esc", "interrupt"),
            ("ctrl+q", "quit"),
        ]
        self.query_one("#rail-hint", Static).update("\n".join(f"[bold]{key}[/] {label}" for key, label in hints))

    def _banner(self, text: str = "", *, visible: bool | None = None) -> None:
        banner = self.query_one("#run-banner", Static)
        if text:
            banner.update(text)
        if visible is not None:
            banner.set_class(visible, "visible")

    def _say(self, who: str, body: Any, *, style: str = "") -> None:
        line = Text()
        line.append(f"{who} › ", style=style or "bold")
        if isinstance(body, Text):
            line.append_text(body)
        else:
            line.append(str(body))
        self.transcript.write(line)

    # -- plan rendering ---------------------------------------------------- #
    def render_plan(self, plan: Any) -> None:
        """Render a :class:`unknown_agent.core.models.Plan` into the plan pane."""

        pane = self.query_one("#plan-pane", Static)
        steps = list(getattr(plan, "steps", []) or [])
        if not steps:
            pane.update("[dim]no plan yet — the agent plans before it acts[/]")
            return
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(ratio=1)
        for index, step in enumerate(steps, start=1):
            title = getattr(step, "title", "") or getattr(step, "description", "") or f"step {index}"
            tools = ", ".join(getattr(step, "tools", []) or [])
            gate = getattr(step, "gate", "") or ""
            status = getattr(step, "status", "")
            mark = {"completed": "[green]✓[/]", "failed": "[red]✕[/]", "running": "[magenta]▶[/]"}.get(
                str(getattr(status, "value", status)), "[dim]○[/]"
            )
            detail = f"{mark} [bold]{index}. {title}[/]"
            extras = " · ".join(part for part in (tools, gate) if part)
            if extras:
                detail += f"\n   [dim]{extras}[/]"
            table.add_row(Text.from_markup(detail))
        definition = getattr(plan, "definition_of_done", None) or []
        if definition:
            table.add_row(Text(""))
            table.add_row(Text.from_markup("[bold]definition of done[/]"))
            for item in list(definition)[:8]:
                text = item if isinstance(item, str) else getattr(item, "description", str(item))
                table.add_row(Text.from_markup(f"  [dim]·[/] {text}"))
        pane.update(table)
        self.transcript.write(Panel(table, title="plan", border_style="cyan", expand=False))

    def render_gates(self, report: Any) -> None:
        pane = self.query_one("#gate-pane", Static)
        rows = getattr(report, "results", None) or getattr(report, "gates", None) or []
        table = Table(box=None, show_header=True, pad_edge=False)
        table.add_column("gate")
        table.add_column("status")
        table.add_column("detail", ratio=1)
        for row in rows:
            name = getattr(row, "name", None) or (row.get("name") if isinstance(row, dict) else "?")
            status = getattr(row, "status", None) or (row.get("status") if isinstance(row, dict) else "")
            detail = getattr(row, "detail", "") or getattr(row, "message", "") or (
                row.get("detail", "") if isinstance(row, dict) else ""
            )
            value = str(getattr(status, "value", status)).lower()
            mark = {"pass": "[green]✓ pass[/]", "passed": "[green]✓ pass[/]", "fail": "[red]✕ fail[/]", "failed": "[red]✕ fail[/]",
                    "skip": "[dim]– skip[/]", "skipped": "[dim]– skip[/]"}.get(value, f"[yellow]{value}[/]")
            table.add_row(str(name), Text.from_markup(mark), str(detail)[:60])
        pane.update(table if rows else Text("no gates reported", style="dim"))
        passed = getattr(report, "passed", None)
        self.transcript.write(
            Panel(
                table,
                title="gates",
                border_style="green" if passed else "red",
                expand=False,
            )
        )

    def render_tools(self) -> None:
        pane = self.query_one("#tool-pane", Static)
        if not self.tool_log:
            pane.update("[dim]no tool calls yet[/]")
            return
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(ratio=1)
        for name, ok, summary in self.tool_log[-40:]:
            mark = "[green]✓[/]" if ok else "[red]✕[/]"
            table.add_row(Text.from_markup(f"{mark} [bold]{name}[/] [dim]{summary[:52]}[/]"))
        pane.update(table)

    # -- run control ------------------------------------------------------- #
    def should_stop(self) -> bool:
        return self._stop.is_set()

    def request_plan_approval(self, plan: Any) -> bool:
        """Blocking bridge: the loop waits while the user reads the plan."""

        self._pending_plan = plan
        self._approval = threading.Event()
        self._approval_decision = True
        self.call_from_thread(self._show_plan_approval, plan)
        # The loop is on another thread; wait for the UI to answer.
        self._approval.wait(timeout=300)
        return self._approval_decision

    def _show_plan_approval(self, plan: Any) -> None:
        self.render_plan(plan)
        self.transcript.write(
            Text.from_markup(
                "[bold yellow]plan needs your approval[/]  ·  [bold]a[/] approve   [bold]r[/] reject   [bold]e[/] edit in the composer"
            )
        )
        self._banner("awaiting plan approval · a approve · r reject", visible=True)

    def answer_approval(self, approved: bool) -> None:
        if self._approval is None:
            return
        self._approval_decision = approved
        self._approval.set()
        self._approval = None
        self._banner("", visible=False)
        self.transcript.write(
            Text.from_markup("[green]plan approved[/]" if approved else "[yellow]plan rejected — the agent will replan[/]")
        )

    def action_interrupt(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self._banner("interrupt requested — finishing the current step", visible=True)
        self.notify("interrupt requested", severity="warning")

    def action_focus_composer(self) -> None:
        self.query_one("#composer", Input).focus()

    def action_clear_transcript(self) -> None:
        self.transcript.clear()
        self._welcome()

    def action_toggle_plan(self) -> None:
        self._show_tab("tab-plan")

    def action_toggle_orchestration(self) -> None:
        self._show_tab("tab-workers")
        self._orchestrate_preview()

    def _show_tab(self, pane_id: str) -> None:
        """Select a side tab by its pane id.

        ``TabbedContent.active`` wants the *tab* id, which Textual generates as
        ``--content-tab-<pane id>``; setting the pane id raises.
        """

        tabs = self.query_one("#side-tabs", TabbedContent)
        try:
            tabs.active = pane_id
        except ValueError:  # pragma: no cover - pane id changed
            pass

    def action_cycle_reasoning(self) -> None:
        from unknown_agent.reasoning import MODE_ORDER

        current = self.status_snapshot.reasoning
        try:
            index = MODE_ORDER.index(current)
        except ValueError:
            index = 2
        next_mode = MODE_ORDER[(index + 1) % len(MODE_ORDER)]
        self.status_snapshot.reasoning = next_mode
        if self.settings is not None and getattr(self.settings, "reasoning", None) is not None:
            self.settings.reasoning.default_mode = next_mode  # type: ignore[assignment]
        self._render_status()
        self._say("shell", f"reasoning depth → {next_mode}", style="bold magenta")
        self.notify(f"reasoning: {next_mode}")

    def action_help(self) -> None:
        self.notify(
            "Enter run · ctrl+p plan · ctrl+o orchestrate · ctrl+b reasoning · ctrl+k clear · esc interrupt · ctrl+q quit",
            title="keys",
            timeout=10,
        )

    # -- running a task ---------------------------------------------------- #
    @on(Input.Submitted, "#composer")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.post_message(ComposerMessage(text))

    @on(Button.Pressed, "#run-button")
    def _run_clicked(self, event: Button.Pressed) -> None:
        field = self.query_one("#composer", Input)
        text = field.value.strip()
        if text:
            field.value = ""
            self.post_message(ComposerMessage(text))

    @on(Button.Pressed, "#plan-button")
    def _plan_clicked(self, event: Button.Pressed) -> None:
        field = self.query_one("#composer", Input)
        text = field.value.strip()
        if text:
            field.value = ""
            self.post_message(ComposerMessage(text, kind="plan"))

    @on(ComposerMessage)
    def _compose(self, event: ComposerMessage) -> None:
        if self.running:
            self.notify("a run is already in progress — press esc to interrupt", severity="warning")
            return
        kind = "ask" if event.text.rstrip().endswith("?") and len(event.text.split()) < 14 else event.kind
        self._say("you", event.text, style="bold cyan")
        analysis = analyze_task(event.text)
        self._preview_analysis(analysis)
        if self._runner is None:
            self._say("shell", "no runner attached — this shell is in preview mode", style="bold yellow")
            return
        self._start_run(event.text, kind)

    def _preview_analysis(self, analysis: Any) -> None:
        pane = self.query_one("#plan-pane", Static)
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(ratio=1)
        table.add_row(Text.from_markup(f"[bold]complexity[/] {analysis.complexity}/10"))
        if analysis.surfaces:
            table.add_row(Text.from_markup(f"[bold]surfaces[/] {', '.join(analysis.surfaces)}"))
        for flag, label in (
            (analysis.is_question, "question (read-only)"),
            (analysis.is_bug, "defect → escalate"),
            (analysis.is_refactor, "refactor → sequential"),
            (analysis.is_greenfield, "greenfield → parallel groups"),
        ):
            if flag:
                table.add_row(Text.from_markup(f"[bold]shape[/] {label}"))
        if analysis.underspecification:
            table.add_row(
                Text.from_markup(
                    f"[bold]underspecified[/] {analysis.underspecification:.0%}"
                    + ("  [yellow]the planner will ask questions[/]" if analysis.underspecification >= 0.6 else "")
                )
            )
        for reason in analysis.reasons[:5]:
            table.add_row(Text.from_markup(f"  [dim]· {reason}[/]"))
        pane.update(table)

    @work(thread=False, exclusive=True)
    async def _start_run(self, task: str, kind: str) -> None:
        self.running = True
        self._stop.clear()
        self.started_at = time.monotonic()
        self._banner(f"running · {kind} · esc to interrupt", visible=True)
        self.status_snapshot.steps_used = 0
        bridge = ShellBridge(self)
        try:
            result = await self._runner(task, bridge.hooks())  # type: ignore[misc]
        except asyncio.CancelledError:  # pragma: no cover - user interrupt
            self._say("shell", "cancelled", style="bold yellow")
            result = None
        except Exception as exc:
            self._say("shell", f"the run failed: {exc}", style="bold red")
            log.exception("shell run failed")
            result = None
        self.running = False
        self._banner("", visible=False)
        self.post_message(self.RunFinished(result))

    @on(RunFinished)
    def _finished(self, event: RunFinished) -> None:
        result = event.result
        elapsed = time.monotonic() - self.started_at if self.started_at else 0.0
        if result is None:
            return
        outcome = getattr(result, "outcome", None)
        summary = getattr(result, "summary", "") or getattr(outcome, "summary", "") or ""
        status = getattr(result, "status", None)
        status_value = str(getattr(status, "value", status or "")).lower()
        style = "bold green" if status_value in {"completed", "ok", "success"} else "bold yellow"
        if summary:
            self._say("agent", summary, style=style)
        plan = getattr(outcome, "plan", None) or getattr(result, "plan", None)
        if plan is not None:
            self.render_plan(plan)
        verification = getattr(result, "verification", None)
        if verification is not None:
            self.render_gates(verification)
        files = list(getattr(result, "files_changed", []) or [])
        if files:
            self._say("changed", ", ".join(files[:8]) + (" …" if len(files) > 8 else ""), style="dim")
        blockers = list(getattr(result, "blockers", []) or [])
        if blockers:
            self._say("blocked by", "; ".join(blockers[:4]), style="bold red")
        self.transcript.write(Text.from_markup(f"[dim]finished in {elapsed:.1f}s[/]"))
        self.transcript.write(Text(""))
        self.query_one("#composer", Input).focus()

    # -- core hook messages ------------------------------------------------ #
    @on(Thought)
    def _thought(self, event: Thought) -> None:
        self.transcript.write(Text(event.text, style="italic dim"))

    @on(AgentSaid)
    def _agent_said(self, event: AgentSaid) -> None:
        text = event.text.strip()
        if not text:
            return
        if len(text) > 240 or "\n" in text:
            self.transcript.write(Panel(Markdown(text), border_style="cyan", expand=False))
        else:
            self._say("agent", text, style="bold")

    @on(ToolStarted)
    def _tool_started(self, event: ToolStarted) -> None:
        args = ", ".join(f"{key}={_short(value)}" for key, value in list(event.arguments.items())[:3])
        self.transcript.write(Text.from_markup(f"[dim]→[/] [bold]{event.name}[/] [dim]{args}[/]"))

    @on(ToolFinished)
    def _tool_finished(self, event: ToolFinished) -> None:
        self.tool_log.append((event.name, event.ok, event.summary))
        self.render_tools()
        mark = "[green]✓[/]" if event.ok else "[red]✕[/]"
        self.transcript.write(Text.from_markup(f"  {mark} [dim]{event.summary[:110]}[/]"))

    @on(Verified)
    def _verified(self, event: Verified) -> None:
        self.render_gates(event.report)

    @on(CoreEvent)
    def _core_event(self, event: CoreEvent) -> None:
        if event.name in {"plan_ready", "plan_updated"} and isinstance(event.payload.get("plan"), object):
            plan = event.payload.get("plan")
            if hasattr(plan, "steps"):
                self.render_plan(plan)
        if event.name in {"worker_state", "worker_started", "worker_finished"}:
            board = self.query_one("#worker-pane", OrchestrationBoard)
            board.update_state(
                str(event.payload.get("work_id", "")),
                str(event.payload.get("state", event.payload.get("status", "running"))),
                note=str(event.payload.get("summary", ""))[:60],
            )

    # -- orchestration preview --------------------------------------------- #
    def _orchestrate_preview(self) -> None:
        """Show the graph the conductor would run for the current task."""

        task = self.query_one("#composer", Input).value.strip() or self.first_task
        board = self.query_one("#worker-pane", OrchestrationBoard)
        if not task:
            self._say("shell", "type a task first — the orchestration plan is derived from it", style="bold yellow")
            return
        from unknown_agent.orchestration.conductor import Router

        analysis = analyze_task(task)
        config = getattr(self.settings, "orchestration", None)
        graph = Router(config=config).build(task, analysis=analysis)
        board.set_graph(graph)
        self.transcript.write(
            Text.from_markup(
                f"[bold]orchestration[/] [cyan]{graph.pattern}[/] · {len(graph.items)} workers · "
                f"[dim]{graph.rationale}[/]"
            )
        )
        for warning in graph.warnings[:3]:
            self.transcript.write(Text.from_markup(f"  [yellow]! {warning}[/]"))


def _short(value: Any, limit: int = 28) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _status_from_settings(settings: Any) -> StatusSnapshot:
    snapshot = StatusSnapshot()
    if settings is None:
        return snapshot
    providers = getattr(settings, "providers", {}) or {}
    name = getattr(settings, "provider", "") or "default"
    profile = providers.get(name) or (next(iter(providers.values())) if providers else None)
    snapshot.profile = name
    if profile is not None:
        snapshot.provider = getattr(profile, "type", "—")
        snapshot.model = getattr(profile, "model", "—")
    appearance = getattr(settings, "appearance", None)
    reasoning = getattr(settings, "reasoning", None)
    planning = getattr(settings, "planning", None)
    orchestration = getattr(settings, "orchestration", None)
    browser = getattr(settings, "browser", None)
    agent = getattr(settings, "agent", None)
    snapshot.reasoning = getattr(reasoning, "default_mode", "high") if reasoning else "high"
    snapshot.planning = getattr(planning, "depth", "standard") if planning else "standard"
    snapshot.orchestration = getattr(orchestration, "mode", "auto") if orchestration else "auto"
    snapshot.browser = bool(getattr(browser, "enabled", False)) if browser else False
    snapshot.budget_tokens = int(getattr(agent, "max_tokens", 0) or 0) if agent else 0
    snapshot.steps_max = int(getattr(agent, "max_steps", 0) or 0) if agent else 0
    return snapshot


log = get_logger("tui.app")


async def run_shell(
    settings: Any = None,
    *,
    runner: RunnerCallable | None = None,
    first_task: str = "",
    headless: bool = False,
) -> AgentShell:
    """Launch the shell.  ``headless`` runs it inside Textual's test harness."""

    app = AgentShell(settings=settings, runner=runner, first_task=first_task)
    if headless:
        async with app.run_test(size=(150, 44)) as pilot:
            await pilot.pause()
        return app
    await app.run_async()
    return app
