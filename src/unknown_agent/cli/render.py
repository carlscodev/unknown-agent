"""Live rendering of the agent loop.

Turns the structured event stream into the terminal experience: step headers,
thought blocks, tool cards, gate tables, budget warnings and the final summary.
It is deliberately the *only* place that knows how a run looks, so the same
events can drive a TUI later without touching the core.

``--ndjson`` switches the renderer into raw event mode: one JSON object per line
on stdout, which is what CI dashboards and log aggregators consume.
"""

from __future__ import annotations

import json
import time
from typing import Any

from unknown_agent.observability.events import Event
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import (
    bullets,
    elapsed,
    gate_table,
    status_label,
    summary_panel,
    thought as render_thought,
    tool_card,
)
from rich.text import Text


class RunRenderer:
    """Stateful renderer for one run."""

    def __init__(self, *, stream_json: bool = False, show_tools: bool = True, show_thoughts: bool = True) -> None:
        self.stream_json = stream_json
        self.show_tools = show_tools
        self.show_thoughts = show_thoughts
        self.started_at = time.time()
        self.tool_started: dict[str, float] = {}
        self.tool_args: dict[str, dict[str, Any]] = {}
        self.steps_seen = 0
        self._last_step_title = ""

    # ------------------------------------------------------------------ #
    def start(self, task: str) -> None:
        self.started_at = time.time()
        if self.stream_json or output.is_machine:
            return
        output.blank()
        title = Text()
        title.append("unknown agent", style="ua.brand")
        title.append("  ", style="")
        title.append(task[:160], style="ua.obs")
        output.print(title)

    def stop(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    def handle_event(self, event: Event) -> None:
        if self.stream_json:
            output.raw(event.to_json())
            return
        if output.is_machine:
            return
        handler = getattr(self, f"_on_{event.type.replace('.', '_')}", None)
        if handler is not None:
            handler(event)

    # -- event handlers ---------------------------------------------------
    def _on_step_start(self, event: Event) -> None:
        if output.quiet:
            return
        self.steps_seen += 1
        title = str(event.data.get("step_title") or event.message)
        self._last_step_title = title
        text = Text()
        text.append(f"step {self.steps_seen}", style="ua.step")
        text.append("  ", style="")
        text.append(title[:110], style="ua.obs")
        output.rule(characters="─" if output.unicode else "-")
        output.print(text)

    def _on_step_end(self, event: Event) -> None:
        if output.quiet:
            return
        output.print(status_label(True, pass_text=f"step complete: {event.data.get('step_title', '')[:90]}"))

    def _on_thought(self, event: Event) -> None:
        pass  # rendered via the on_thought hook with full text

    def _on_tool_requested(self, event: Event) -> None:
        if not self.show_tools or output.quiet:
            return
        name = str(event.data.get("tool", event.message))
        self.tool_args[name] = event.data.get("arguments") or {}
        output.print(tool_card(name, self.tool_args[name]))

    def _on_tool_finished(self, event: Event) -> None:
        if not self.show_tools or output.quiet:
            return
        name = str(event.data.get("tool", "tool"))
        duration = float(event.data.get("duration_ms", 0)) / 1000
        ok = bool(event.data.get("ok", True))
        if ok:
            output.print(Text(f"  {name} ok ({duration:.2f}s)", style="ua.ok.dim"))
        else:
            output.print(Text(f"  {name} failed: {str(event.data.get('error', ''))[:200]}", style="ua.err"))

    def _on_tool_failed(self, event: Event) -> None:
        if output.quiet or self.stream_json:
            return
        name = str(event.data.get("tool", "tool"))
        reason = event.data.get("error") or event.data.get("reason") or "unknown error"
        output.print(Text(f"  {name} failed: {str(reason)[:220]}", style="ua.err"))

    def _on_tool_denied(self, event: Event) -> None:
        if output.quiet:
            return
        reason = event.data.get("reason") or "denied by policy"
        output.print(Text(f"  denied: {event.data.get('tool', '')} - {str(reason)[:200]}", style="ua.warn"))

    def _on_plan_created(self, event: Event) -> None:
        if output.quiet:
            return
        steps = event.data.get("steps") or []
        source = event.data.get("source", "model")
        output.blank()
        output.print(Text(f"plan ({source}, {len(steps)} steps)", style="ua.plan"))
        output.print(bullets([f"{index}. {step}" for index, step in enumerate(steps, start=1)]))

    def _on_plan_revised(self, event: Event) -> None:
        if output.quiet:
            return
        output.print(Text(f"plan revised: {event.message}", style="ua.warn"))

    def _on_gate_started(self, event: Event) -> None:
        if output.quiet:
            return
        if event.data.get("update"):
            output.print(Text(f"  {event.message}", style="ua.muted"))

    def _on_gate_finished(self, event: Event) -> None:
        if output.quiet:
            return
        status = str(event.data.get("status", "passed"))
        mapping = {"passed": True, "failed": False, "error": False, "skipped": None}
        label = status_label(mapping.get(status), pass_text="passed", fail_text="failed", skip_text="skipped")
        text = Text("  ")
        text.append(str(event.data.get("gate", "")), style="ua.tool")
        text.append(" ")
        text.append_text(label)
        summary = str(event.data.get("summary", ""))[:120]
        if summary:
            text.append(f"  {summary}", style="ua.muted")
        output.print(text)

    def _on_budget_warning(self, event: Event) -> None:
        output.print(Text(f"  budget: {event.message}", style="ua.warn"))

    def _on_run_blocked(self, event: Event) -> None:
        output.print(Text(f"  blocked: {event.message}", style="ua.warn"))

    def _on_provider_request(self, event: Event) -> None:
        if output.verbose < 2:
            return
        output.diag_print(f"[ua.muted]provider: {event.message}[/ua.muted]")

    def _on_run_end(self, event: Event) -> None:
        if output.quiet:
            return
        status = str(event.data.get("status", "completed"))
        style = "ua.ok" if status == "completed" else "ua.warn"
        output.blank()
        output.print(Text(f"run {status} in {elapsed(time.time() - self.started_at)}", style=style))

    # -- hook callbacks ---------------------------------------------------
    def thought(self, text: str) -> None:
        if not self.show_thoughts or output.is_machine or output.quiet:
            return
        cleaned = text.strip()
        if not cleaned:
            return
        render_thought(cleaned[:1200])

    def tool_start(self, call: Any) -> None:
        return None

    def tool_end(self, call: Any, result: Any) -> None:
        return None

    def verification(self, report: Any) -> None:
        if output.is_machine or output.quiet:
            return
        output.blank()
        output.print(gate_table(report.results))


def render_run_summary(result: Any, *, show_diff: bool = False, diff_lines: int = 3) -> None:
    """Final, evidence-first summary printed by agent commands."""

    state = result.state
    rows: list[tuple[str, Any]] = [
        ("run id", state.id),
        ("status", state.status.value),
        ("model", f"{state.provider}/{state.model}" if state.model else state.provider),
        ("duration", elapsed(state.duration_s)),
        ("steps", f"{state.budget.steps_used}/{state.budget.max_steps}"),
        ("tokens", f"{state.budget.tokens_used:,}"),
        ("files changed", len(state.files_changed) or "none"),
    ]
    if state.files_changed:
        rows.append(("files", state.files_changed[:8]))
    if state.verification is not None:
        passed = len([item for item in state.verification.results if item.passed])
        rows.append(("gates", f"{passed}/{len(state.verification.results)} passed"))
    if state.blockers:
        rows.append(("blockers", state.blockers[:3]))
    border = "ua.success.border" if state.status.value == "completed" else "ua.error.border"
    output.blank()
    output.print(summary_panel("run summary", rows, style=border))
    if state.summary:
        output.print(Text(state.summary, style="ua.obs"))
    if show_diff and result.diff:
        output.blank()
        output.print(Text("working tree diff", style="ua.muted"))
        output.print(_diff_renderable(result.diff))
    output.blank()
    output.print(Text(f"journal: {result.run_dir}", style="ua.muted"))


def _diff_renderable(diff: str) -> Any:
    from unknown_agent.ui.renderers import unified_diff

    return unified_diff(diff[:20_000])


__all__ = ["RunRenderer", "render_run_summary"]
