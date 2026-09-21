"""Status and metrics widgets for the shell sidebar.

Three cards, each answering one question the old rail could not:

* :class:`ProviderStatusCard` — *can I reach a model right now, and if not, what do I
  press?*  Provider problems live here as first-class UI, with the remedies attached,
  rather than as a crash during startup.
* :class:`SessionMetricsPanel` — *what has this session cost me?*  Input/output split,
  total, cost (labelled estimate or measured), and per-model breakdown.
* :class:`ContextGauge` — *how close am I to the context wall?*  A bar, because a
  percentage alone does not convey pressure.

All three are pure renderers over a snapshot dataclass: they hold no provider handles,
do no I/O, and cannot raise a provider error.
"""

from __future__ import annotations

from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from unknown_agent.providers.health import ProviderStatus
from unknown_agent.tui.provider_status import ProviderState
from unknown_agent.tui.usage import UsageSnapshot
from unknown_agent.ui.theme_defs import get_palette

__all__ = ["ProviderStatusCard", "SessionMetricsPanel", "ContextGauge", "AgentStateCard", "status_dot"]


#: A glyph per status, so health is readable before the text is.
_DOTS: dict[str, str] = {
    ProviderStatus.READY: "●",
    ProviderStatus.CONNECTED: "●",
    ProviderStatus.VALIDATING: "◐",
    ProviderStatus.RECOVERING: "◐",
    ProviderStatus.DEGRADED: "◐",
    ProviderStatus.AUTH_CONFLICT: "▲",
    ProviderStatus.RATE_LIMITED: "▲",
    ProviderStatus.NOT_CONFIGURED: "○",
    ProviderStatus.SETUP_REQUIRED: "○",
    ProviderStatus.LOGIN_REQUIRED: "○",
    ProviderStatus.INVALID_CREDENTIALS: "✕",
    ProviderStatus.CREDITS_EXHAUSTED: "✕",
    ProviderStatus.OFFLINE: "✕",
}

#: Status style token → palette colour role.
_ROLE = {"success": "ok", "warning": "warn", "error": "err", "accent": "brand", "muted": "text_dim"}


def status_dot(status: str, palette: Any) -> Text:
    """The coloured glyph + label pair used everywhere a status appears."""

    glyph = _DOTS.get(status, "·")
    colour = palette.color(_ROLE.get(ProviderStatus.style(status), "text_dim"))
    text = Text()
    text.append(f"{glyph} ", style=colour)
    text.append(ProviderStatus.label(status), style=f"bold {colour}")
    return text


class _Card(Static):
    """Shared palette plumbing for the sidebar cards."""

    def __init__(self, *, palette_key: str | None = None, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.palette_key = palette_key

    @property
    def palette(self) -> Any:
        return get_palette(None if self.palette_key in (None, "auto") else self.palette_key)

    def _title(self, label: str) -> Text:
        # Padded to a fixed width so the label column never collides with its value.
        return Text(f"{label.upper():<9}", style=f"bold {self.palette.color('text_dim')}")


class ProviderStatusCard(_Card):
    """Provider health, plus what the user can do about it."""

    state: reactive[ProviderState | None] = reactive(None, layout=True)

    def watch_state(self, state: ProviderState | None) -> None:
        self.update(self.render_state(state))

    def render_state(self, state: ProviderState | None) -> RenderableType:
        palette = self.palette
        if state is None:
            return Text("provider: unknown", style=palette.color("text_dim"))

        rows = Table.grid(padding=(0, 1))
        rows.add_column(justify="left", no_wrap=True, width=9)
        rows.add_column(justify="left", overflow="fold")

        rows.add_row(self._title("provider"), status_dot(state.status, palette))
        rows.add_row("", Text(state.provider_label or state.provider, style=palette.color("text")))
        if state.model and state.model != "—":
            rows.add_row(self._title("model"), Text(state.model, style=palette.color("text")))
        if state.auth_mode_label:
            rows.add_row(self._title("auth"), Text(state.auth_mode_label, style=palette.color("text_dim")))
        if state.model_count:
            rows.add_row(self._title("models"), Text(f"{state.model_count} available", style=palette.color("text_dim")))

        blocks: list[RenderableType] = [rows]

        if state.message and not state.ok:
            blocks.append(Text(""))
            blocks.append(Text(state.message, style=palette.color(_ROLE.get(ProviderStatus.style(state.status), "text_dim"))))
        if state.hint and not state.ok:
            blocks.append(Text(state.hint, style=palette.color("text_dim")))

        for conflict in state.conflicts:
            blocks.append(Text(""))
            marker = "✕" if conflict.severity == "error" else "▲"
            colour = palette.color("err" if conflict.severity == "error" else "warn")
            blocks.append(Text(f"{marker} {conflict.title}", style=f"bold {colour}"))
            blocks.append(Text(conflict.detail, style=palette.color("text_dim")))
            if conflict.fix:
                blocks.append(Text(f"  fix: {conflict.fix}", style=palette.color("text_dim")))

        if state.remedies:
            blocks.append(Text(""))
            blocks.append(Text(" · ".join(state.remedies), style=palette.color("brand")))

        return Group(*blocks)


class ContextGauge(_Card):
    """Context-window pressure as a bar, because a number alone does not warn."""

    usage: reactive[UsageSnapshot | None] = reactive(None, layout=True)

    def watch_usage(self, usage: UsageSnapshot | None) -> None:
        self.update(self.render_usage(usage))

    def render_usage(self, usage: UsageSnapshot | None) -> RenderableType:
        palette = self.palette
        if usage is None or usage.context_window <= 0:
            return Group(self._title("context"), Text("window unknown", style=palette.color("text_dim")))

        pct = usage.context_pct
        width = 18
        filled = max(0, min(width, int(round(pct / 100.0 * width))))
        # Colour tracks urgency: green until half, amber at three-quarters, red past 90%.
        colour = palette.color("ok" if pct < 50 else "warn" if pct < 90 else "err")
        bar = Text()
        bar.append("█" * filled, style=colour)
        bar.append("░" * (width - filled), style=palette.color("text_dim"))

        detail = Text(
            f"{usage.context_tokens:,} / {usage.context_window:,} ({pct:.0f}%)",
            style=palette.color("text_dim"),
        )
        return Group(self._title("context"), bar, detail)


class SessionMetricsPanel(_Card):
    """Tokens and money for this session — measured where possible, labelled where not."""

    usage: reactive[UsageSnapshot | None] = reactive(None, layout=True)

    def watch_usage(self, usage: UsageSnapshot | None) -> None:
        self.update(self.render_usage(usage))

    def render_usage(self, usage: UsageSnapshot | None) -> RenderableType:
        palette = self.palette
        if usage is None or usage.calls == 0:
            return Group(self._title("session"), Text("no model calls yet", style=palette.color("text_dim")))

        grid = Table.grid(padding=(0, 1))
        grid.add_column(justify="left", no_wrap=True)
        grid.add_column(justify="right", no_wrap=True)

        tilde = "" if usage.fully_measured else "~"
        grid.add_row(Text("input", style=palette.color("text_dim")), Text(f"{tilde}{usage.input_tokens:,}", style=palette.color("text")))
        grid.add_row(Text("output", style=palette.color("text_dim")), Text(f"{tilde}{usage.output_tokens:,}", style=palette.color("text")))
        grid.add_row(Text("total", style=palette.color("text_dim")), Text(f"{tilde}{usage.total_tokens:,}", style=f"bold {palette.color('text')}"))
        grid.add_row(
            Text("cost", style=palette.color("text_dim")),
            Text(usage.cost_text(), style=f"bold {palette.color('brand')}"),
        )
        grid.add_row(Text("calls", style=palette.color("text_dim")), Text(str(usage.calls), style=palette.color("text")))

        blocks: list[RenderableType] = [self._title("session"), grid]

        # Say plainly where the numbers come from; a silent estimate is a lie.
        if not usage.fully_measured:
            blocks.append(Text("~ estimated from text length", style=palette.color("text_dim")))
        if not usage.fully_priced:
            blocks.append(Text("+ some models have no published price", style=palette.color("text_dim")))

        if len(usage.by_model) > 1:
            blocks.append(Text(""))
            blocks.append(self._title("by model"))
            for item in usage.by_model[:4]:
                line = Text()
                line.append(f"{item.model[:22]:<22}", style=palette.color("text"))
                line.append(f"{item.total_tokens:>8,}", style=palette.color("text_dim"))
                blocks.append(line)

        return Group(*blocks)


class AgentStateCard(_Card):
    """What the agent is doing right now, and under which reasoning profile."""

    info: reactive[dict[str, Any]] = reactive(dict, layout=True)

    def watch_info(self, info: dict[str, Any]) -> None:
        self.update(self.render_info(info or {}))

    def render_info(self, info: dict[str, Any]) -> RenderableType:
        palette = self.palette
        grid = Table.grid(padding=(0, 1))
        grid.add_column(justify="left", no_wrap=True, width=9)
        grid.add_column(justify="left", overflow="ellipsis")

        state = str(info.get("state", "idle"))
        colour = palette.color("brand" if state not in {"idle", "ready"} else "text_dim")
        grid.add_row(self._title("agent"), Text(state, style=f"bold {colour}"))
        for label in ("mode", "reasoning", "profile", "steps", "elapsed"):
            value = info.get(label)
            if value:
                grid.add_row(
                    Text(f"{label:<9}", style=palette.color("text_dim")),
                    Text(str(value), style=palette.color("text")),
                )
        return grid
