"""Sidebar cards: provider health, context pressure, session metrics, agent state.

Presentation rules, applied uniformly
-------------------------------------
* **Section labels** are small, muted, letter-spaced-by-convention caps. They name a
  region without competing with its contents.
* **Values** are the only primary-weight text in the sidebar, so the eye lands on data.
* **Colour appears only for state.** A healthy provider is *not* green-on-green; it is
  a single small marker plus neutral text. Colour spent on "everything is fine" is
  colour unavailable for "something is wrong".
* **Fixed label columns** via :func:`~unknown_agent.tui.design.kv` keep every row
  aligned, which is what makes a stack of numbers scannable.

Each card is a pure function of a snapshot: no provider handles, no I/O, so no card
can raise a provider error while painting.
"""

from __future__ import annotations

from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from unknown_agent.providers.health import ProviderStatus
from unknown_agent.tui.design import (
    GLYPH,
    Tokens,
    compact_number,
    kv,
    meter,
    truncate,
)
from unknown_agent.tui.provider_status import ProviderState
from unknown_agent.tui.usage import UsageSnapshot

__all__ = [
    "SidebarCard",
    "ProviderCard",
    "ContextCard",
    "UsageCard",
    "SessionCard",
    "status_marker",
    "status_line",
]


#: Status → (glyph, semantic token). Deliberately sparse: most states are neutral,
#: so the few that are coloured genuinely stand out.
_STATUS_VISUAL: dict[str, tuple[str, str]] = {
    ProviderStatus.READY: (GLYPH["ready"], "ok"),
    ProviderStatus.CONNECTED: (GLYPH["ready"], "ok"),
    ProviderStatus.VALIDATING: (GLYPH["busy"], "accent"),
    ProviderStatus.RECOVERING: (GLYPH["busy"], "accent"),
    ProviderStatus.DEGRADED: (GLYPH["warn"], "warn"),
    ProviderStatus.AUTH_CONFLICT: (GLYPH["warn"], "warn"),
    ProviderStatus.RATE_LIMITED: (GLYPH["warn"], "warn"),
    ProviderStatus.NOT_CONFIGURED: (GLYPH["idle"], "muted"),
    ProviderStatus.SETUP_REQUIRED: (GLYPH["idle"], "warn"),
    ProviderStatus.LOGIN_REQUIRED: (GLYPH["idle"], "warn"),
    ProviderStatus.INVALID_CREDENTIALS: (GLYPH["error"], "err"),
    ProviderStatus.CREDITS_EXHAUSTED: (GLYPH["error"], "err"),
    ProviderStatus.OFFLINE: (GLYPH["error"], "err"),
}


def status_marker(status: str, tokens: Tokens) -> tuple[str, str]:
    """``(glyph, colour)`` for a provider status."""

    glyph, token = _STATUS_VISUAL.get(status, (GLYPH["dot"], "muted"))
    return glyph, tokens.color(token)


def status_line(status: str, tokens: Tokens, *, bold: bool = True) -> Text:
    """Marker plus label — the canonical way a status is written anywhere."""

    glyph, colour = status_marker(status, tokens)
    text = Text()
    text.append(f"{glyph} ", style=colour)
    text.append(ProviderStatus.label(status), style=f"bold {colour}" if bold else colour)
    return text


class SidebarCard(Static):
    """Base card: owns the tokens and renders a consistent section label."""

    DEFAULT_CSS = """
    SidebarCard { height: auto; padding: 0; margin: 0 0 1 0; }
    """

    def __init__(self, *, tokens: Tokens, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.tokens = tokens

    def set_tokens(self, tokens: Tokens) -> None:
        """Re-theme in place, then repaint through the normal path."""

        self.tokens = tokens
        self.refresh_content()

    def refresh_content(self) -> None:  # pragma: no cover - overridden
        pass

    def section(self, label: str) -> Text:
        # Spaced caps: quiet, but unmistakably a heading.
        return Text(" ".join(label.upper()), style=f"bold {self.tokens.muted}")


class ProviderCard(SidebarCard):
    """Provider, model, auth mode and health — plus what to press when it is unwell."""

    state: reactive[ProviderState | None] = reactive(None, layout=True)

    def watch_state(self, state: ProviderState | None) -> None:
        self.update(self.render_state(state))

    def refresh_content(self) -> None:
        self.update(self.render_state(self.state))

    def render_state(self, state: ProviderState | None) -> RenderableType:
        tokens = self.tokens
        if state is None:
            return Group(self.section("provider"), Text("not initialised", style=tokens.faint))

        blocks: list[RenderableType] = [self.section("provider"), Text("")]
        blocks.append(status_line(state.status, tokens))

        provider = state.provider_label or (state.provider if state.provider != "—" else "")
        if provider:
            blocks.append(kv("name", truncate(provider, 20), tokens))
        if state.model and state.model != "—":
            blocks.append(kv("model", truncate(state.model, 20), tokens))
        if state.auth_mode_label:
            blocks.append(kv("auth", truncate(state.auth_mode_label, 20), tokens, value_style=tokens.dim))
        if state.model_count:
            blocks.append(kv("catalog", f"{state.model_count:,} models", tokens, value_style=tokens.dim))

        # Problems: stated plainly, with the fix and the keys that apply it.
        if not state.ok and state.message:
            blocks.append(Text(""))
            _, colour = status_marker(state.status, tokens)
            blocks.append(Text(state.message, style=colour))
        if not state.ok and state.hint:
            blocks.append(Text(state.hint, style=tokens.muted))

        for conflict in state.conflicts:
            blocks.append(Text(""))
            colour = tokens.err if conflict.severity == "error" else tokens.warn
            glyph = GLYPH["error"] if conflict.severity == "error" else GLYPH["warn"]
            blocks.append(Text(f"{glyph} {conflict.title}", style=colour))
            blocks.append(Text(conflict.detail, style=tokens.muted))
            if conflict.fix:
                blocks.append(Text(conflict.fix, style=tokens.faint))

        return Group(*blocks)


class ContextCard(SidebarCard):
    """How much of the context window the conversation currently occupies."""

    usage: reactive[UsageSnapshot | None] = reactive(None, layout=True)

    def watch_usage(self, usage: UsageSnapshot | None) -> None:
        self.update(self.render_usage(usage))

    def refresh_content(self) -> None:
        self.update(self.render_usage(self.usage))

    def render_usage(self, usage: UsageSnapshot | None) -> RenderableType:
        tokens = self.tokens
        blocks: list[RenderableType] = [self.section("context"), Text("")]
        if usage is None or usage.context_window <= 0:
            blocks.append(Text("window unknown", style=tokens.faint))
            return Group(*blocks)

        fraction = usage.context_tokens / usage.context_window
        blocks.append(meter(fraction, 22, tokens))
        detail = Text()
        detail.append(f"{compact_number(usage.context_tokens)}", style=tokens.text)
        detail.append(f" / {compact_number(usage.context_window)}", style=tokens.muted)
        detail.append(f"   {usage.context_pct:.0f}%", style=tokens.muted)
        blocks.append(detail)
        return Group(*blocks)


class UsageCard(SidebarCard):
    """Tokens and money — measured where the provider told us, marked where inferred."""

    usage: reactive[UsageSnapshot | None] = reactive(None, layout=True)

    def watch_usage(self, usage: UsageSnapshot | None) -> None:
        self.update(self.render_usage(usage))

    def refresh_content(self) -> None:
        self.update(self.render_usage(self.usage))

    def render_usage(self, usage: UsageSnapshot | None) -> RenderableType:
        tokens = self.tokens
        blocks: list[RenderableType] = [self.section("usage"), Text("")]
        if usage is None or usage.calls == 0:
            blocks.append(Text("no model calls yet", style=tokens.faint))
            return Group(*blocks)

        tilde = "" if usage.fully_measured else "~"
        blocks.append(kv("input", f"{tilde}{usage.input_tokens:,}", tokens))
        blocks.append(kv("output", f"{tilde}{usage.output_tokens:,}", tokens))
        blocks.append(kv("total", f"{tilde}{usage.total_tokens:,}", tokens, value_style=f"bold {tokens.text}"))
        blocks.append(kv("cost", usage.cost_text(), tokens, value_style=f"bold {tokens.text}"))
        blocks.append(kv("calls", f"{usage.calls:,}", tokens, value_style=tokens.dim))

        # Provenance, stated rather than implied. A silent estimate is a lie.
        notes = []
        if not usage.fully_measured:
            notes.append("~ estimated from text length")
        if not usage.fully_priced:
            notes.append("+ some models are unpriced")
        if notes:
            blocks.append(Text(""))
            for note in notes:
                blocks.append(Text(note, style=tokens.faint))

        if len(usage.by_model) > 1:
            blocks.append(Text(""))
            blocks.append(Text("by model", style=tokens.muted))
            for item in usage.by_model[:4]:
                row = Text()
                row.append(f"{truncate(item.model, 16):<17}", style=tokens.dim)
                row.append(f"{compact_number(item.total_tokens):>6}", style=tokens.muted)
                blocks.append(row)

        return Group(*blocks)


class SessionCard(SidebarCard):
    """What the agent is doing, under which reasoning profile, for how long."""

    info: reactive[dict[str, Any]] = reactive(dict, layout=True)

    def watch_info(self, info: dict[str, Any]) -> None:
        self.update(self.render_info(info or {}))

    def refresh_content(self) -> None:
        self.update(self.render_info(self.info or {}))

    def render_info(self, info: dict[str, Any]) -> RenderableType:
        tokens = self.tokens
        blocks: list[RenderableType] = [self.section("session"), Text("")]

        state = str(info.get("state", "idle"))
        busy = state not in {"idle", "ready", ""}
        marker = Text()
        marker.append(f"{GLYPH['busy'] if busy else GLYPH['idle']} ", style=tokens.accent if busy else tokens.muted)
        marker.append(state, style=f"bold {tokens.text}" if busy else tokens.dim)
        blocks.append(marker)

        for label in ("mode", "reasoning", "elapsed", "turns"):
            value = info.get(label)
            if value:
                blocks.append(kv(label, str(value), tokens, value_style=tokens.dim))
        if info.get("step"):
            blocks.append(Text(""))
            blocks.append(Text(truncate(str(info["step"]), 28), style=tokens.muted))
        return Group(*blocks)
