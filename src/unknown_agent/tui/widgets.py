"""The premium widget set.

These are the building blocks every Unknown Agent screen is made of.  Each one
exists because a stock widget could not carry the information density or the
feedback the product needs:

* :class:`StepRail`          where you are in a sequence, at a glance
* :class:`ApiKeyInput`       paste a secret safely, see it validated live
* :class:`CapabilityBadges`  what a provider/model can actually do
* :class:`OptionGrid`        choose one, with the consequence shown up front
* :class:`DecisionPanel`     a planning question with a recommendation and a reason
* :class:`ModelTable`        models with context, cost and capability columns
* :class:`SwatchRow`         a palette's colours as chips
* :class:`SwitchGrid`        permission toggles with a risk label each
* :class:`ReasoningLadder`   the depth ladder with what each rung costs
* :class:`OrchestrationBoard` live worker states during a run

Design rules they all follow:

1. **Never a bare control.**  Every widget carries its own explanation — a
   permission switch says what it risks, a model row says what it costs.
2. **Keyboard first, mouse welcome.**  Everything is reachable without a pointer.
3. **Degrade to the terminal you have.**  No widget depends on truecolor, a
   specific font, or a minimum width above 80 columns.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from textual import events, on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive, var
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Label, OptionList, Static, Switch
from textual.widgets.option_list import Option

from unknown_agent.providers.catalog import ProviderEntry
from unknown_agent.providers.discovery import ModelInfo
from unknown_agent.ui.theme_defs import TOKEN_ORDER, Palette, get_palette

__all__ = [
    "PRODUCT_CSS",
    "StepRail",
    "ApiKeyInput",
    "CapabilityBadges",
    "OptionGrid",
    "GridChoice",
    "DecisionPanel",
    "ModelTable",
    "SwatchRow",
    "SwitchGrid",
    "SwitchItem",
    "ReasoningLadder",
    "OrchestrationBoard",
    "PanelCard",
    "KeyHints",
]


#: App-level CSS that uses the product tokens (``$ua-*``).  It is only valid once
#: :func:`unknown_agent.tui.themes.apply_to_app` has registered the palettes, so
#: screens include it in their own ``CSS`` rather than in a widget ``DEFAULT_CSS``.
PRODUCT_CSS = """
.product-muted { color: $ua-muted; }
.product-faint { color: $ua-faint; }
.product-accent { color: $ua-accent; }
.product-card {
    border: round $ua-border-dim 50%;
    background: $panel;
}
"""

# --------------------------------------------------------------------------- #
# layout primitives
# --------------------------------------------------------------------------- #
class PanelCard(Vertical):
    """A titled card: the visual unit of every screen."""

    DEFAULT_CSS = """
    PanelCard {
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        border: round $primary 60%;
        background: $surface;
    }
    PanelCard > .panel-card-title {
        color: $primary;
        text-style: bold;
        width: 100%;
        height: 1;
        margin-bottom: 1;
    }
    PanelCard > .panel-card-note {
        color: $text-muted;
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }
    """

    def __init__(self, title: str = "", note: Any = "", *children: Widget, **kwargs: Any) -> None:
        super().__init__(*children, **kwargs)
        self.card_title = title
        #: A short explanation under the title.  Accepts a string (wrapped in a
        #: ``Static``) or a widget, because ``PanelCard("t", Static(...))`` — the
        #: natural way to give a card a single body — must not silently turn the
        #: body into the note.
        self.card_note = note

    def compose(self):  # type: ignore[no-untyped-def]
        # Children passed to ``__init__`` are appended by Textual *after* what
        # ``compose`` yields, so this only has to produce the header.
        if self.card_title:
            yield Static(self.card_title, classes="panel-card-title")
        if isinstance(self.card_note, Widget):
            yield self.card_note
        elif self.card_note:
            yield Static(str(self.card_note), classes="panel-card-note")


class KeyHints(Static):
    """The persistent hint line at the bottom of a screen."""

    DEFAULT_CSS = """
    KeyHints {
        dock: bottom;
        height: 1;
        color: $text-disabled;
        padding: 0 1;
        width: 100%;
    }
    """

    def set_hints(self, hints: Sequence[tuple[str, str]]) -> None:
        self.update("   ".join(f"[bold]{key}[/] {label}" for key, label in hints))


# --------------------------------------------------------------------------- #
# step rail
# --------------------------------------------------------------------------- #
class StepRail(Widget):
    """A vertical progress rail: done / current / remaining, with a jump affordance."""

    DEFAULT_CSS = """
    StepRail {
        width: 34;
        height: 100%;
        padding: 1 0 1 1;
        background: $surface;
        border-right: round $panel-lighten-2 40%;
    }
    StepRail .rail-row {
        height: 1;
        width: 100%;
        color: $text-muted;
    }
    StepRail .rail-row.done { color: $success; }
    StepRail .rail-row.current { color: $primary; text-style: bold; }
    StepRail .rail-row.skipped { color: $text-disabled; text-style: italic; }
    StepRail .rail-head {
        color: $text-disabled;
        height: 2;
        margin-bottom: 1;
    }
    """

    class Jumped(Message):
        """The user asked to revisit a step."""

        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    def __init__(self, steps: Sequence[str], *, current: int = 0, name: str | None = None, id: str | None = None) -> None:
        super().__init__(name=name, id=id)
        self._steps = list(steps)
        self._current = current
        self._done: set[int] = set()
        self._skipped: set[int] = set()

    def compose(self):  # type: ignore[no-untyped-def]
        yield Static("setup", classes="rail-head")
        for index, label in enumerate(self._steps):
            yield Static(f"{index + 1:>2}. {label}", classes="rail-row", id=f"rail-{index}")

    def set_current(self, index: int, *, done: bool = True) -> None:
        self._current = index
        if done:
            self._done.update(range(index))
        self._refresh_rows()

    def mark(self, index: int, *, done: bool = True, skipped: bool = False) -> None:
        if skipped:
            self._skipped.add(index)
            self._done.discard(index)
        elif done:
            self._done.add(index)
            self._skipped.discard(index)
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        for index in range(len(self._steps)):
            try:
                row = self.query_one(f"#rail-{index}", Static)
            except Exception:  # pragma: no cover - before compose
                return
            classes = {"rail-row"}
            marker = " "
            if index in self._done:
                classes.add("done")
                marker = "✓"
            elif index in self._skipped:
                classes.add("skipped")
                marker = "–"
            if index == self._current:
                classes.add("current")
                marker = "›" if index not in self._done else "✓"
            # Textual's signature is ``set_class(add: bool, *class_names)`` — the
            # toggle comes first, which is easy to get backwards.
            row.set_class("done" in classes, "done")
            row.set_class("current" in classes, "current")
            row.set_class("skipped" in classes, "skipped")
            row.update(f"{marker} {index + 1:>2}. {self._steps[index]}")

    def on_mount(self) -> None:
        self._refresh_rows()

    async def on_click(self, event: events.Click) -> None:
        target = event.widget if hasattr(event, "widget") else None
        widget_id = getattr(target, "id", "") or ""
        match = re.match(r"rail-(\d+)", widget_id)
        if match:
            self.post_message(self.Jumped(int(match.group(1))))


# --------------------------------------------------------------------------- #
# API key capture
# --------------------------------------------------------------------------- #
_KEY_SHAPES: dict[str, re.Pattern[str]] = {
    "openai": re.compile(r"^sk-[A-Za-z0-9_-]{16,}"),
    "anthropic": re.compile(r"^sk-ant-[A-Za-z0-9_-]{16,}"),
    "groq": re.compile(r"^gsk_[A-Za-z0-9]{16,}"),
    "mistral": re.compile(r"^[A-Za-z0-9]{24,}"),
    "openrouter": re.compile(r"^sk-or-(?:v1-)?[A-Za-z0-9_-]{16,}"),
    "deepseek": re.compile(r"^sk-[A-Za-z0-9]{16,}"),
    "xai": re.compile(r"^xai-[A-Za-z0-9_-]{16,}"),
    "google": re.compile(r"^AIza[A-Za-z0-9_-]{20,}"),
    "cohere": re.compile(r"^[A-Za-z0-9._-]{20,}"),
    "together": re.compile(r"^t?[a-f0-9]{32,}"),
    "huggingface": re.compile(r"^hf_[A-Za-z0-9]{20,}"),
}


class ApiKeyInput(Vertical):
    """Paste a secret without ever seeing it echoed in full.

    Three pieces of feedback, all local and all before any network call:

    * a **fingerprint** (first 3 + last 4 characters) so you can confirm you pasted
      the right key without printing it;
    * a **shape check** against the vendor's published key format, which catches
      the most common mistakes — pasting the org id, a URL, or a key from another
      vendor — instantly and offline;
    * a **leak warning** if the value looks like a shell command, a path or
      already contains ``export``, which is what happens when someone pastes the
      line from their notes instead of the key.

    The value never enters the log, never enters config, and is only ever written
    to the credential store (0600) or the OS keyring.
    """

    DEFAULT_CSS = """
    ApiKeyInput {
        height: auto;
        width: 100%;
    }
    ApiKeyInput Input {
        width: 100%;
        margin: 0 0 1 0;
    }
    ApiKeyInput .key-meta {
        height: auto;
        width: 100%;
        color: $text-muted;
    }
    ApiKeyInput .key-meta.error { color: $error; }
    ApiKeyInput .key-meta.ok { color: $success; }
    ApiKeyInput .key-meta.warn { color: $warning; }
    """

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    class Changed(Message):
        def __init__(self, value: str, verdict: str, detail: str) -> None:
            super().__init__()
            self.value = value
            self.verdict = verdict
            self.detail = detail

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    def __init__(
        self,
        *,
        vendor: str = "",
        env_var: str = "",
        existing_hint: str = "",
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self.vendor = vendor
        self.env_var = env_var
        self.existing_hint = existing_hint
        self.verdict = "empty"
        self.detail = ""

    def compose(self):  # type: ignore[no-untyped-def]
        placeholder = f"paste your {self.vendor or 'provider'} API key"
        if self.env_var:
            placeholder += f"  (or set ${self.env_var})"
        yield Input(placeholder=placeholder, password=True, id="key-input", type="text")
        hint = self.existing_hint or "masked — only a fingerprint is ever shown"
        yield Static(hint, classes="key-meta", id="key-meta")

    def _field(self) -> Input | None:
        try:
            return self.query_one("#key-input", Input)
        except Exception:
            return None

    @property
    def value(self) -> str:
        field = self._field()
        return field.value.strip() if field is not None else ""

    def clear(self) -> None:
        field = self._field()
        if field is not None:
            field.value = ""
        self.set_state("empty", "masked — only a fingerprint is ever shown")

    def focus_input(self) -> None:
        field = self._field()
        if field is not None:
            field.focus()

    def set_state(self, verdict: str, detail: str) -> None:
        self.verdict = verdict
        self.detail = detail
        try:
            meta = self.query_one("#key-meta", Static)
        except Exception:  # pragma: no cover - called before compose
            return
        meta.set_class(verdict == "error", "error")
        meta.set_class(verdict == "ok", "ok")
        meta.set_class(verdict == "warn", "warn")
        meta.update(detail)

    def fingerprint(self, value: str | None = None) -> str:
        raw = value if value is not None else self.value
        if not raw:
            return ""
        if len(raw) <= 8:
            return "•" * len(raw)
        return f"{raw[:3]}{'•' * 8}{raw[-4:]}  ({len(raw)} chars)"

    def inspect(self, value: str | None = None) -> tuple[str, str]:
        """Offline sanity check: ``(verdict, detail)``."""

        raw = (value if value is not None else self.value).strip()
        if not raw:
            return "empty", "masked — only a fingerprint is ever shown"
        if raw.startswith("export ") or " " in raw.split("=")[0] and "=" in raw:
            return "error", "that looks like a shell line, not a key — paste only the value after the ="
        if raw.startswith(("http://", "https://", "/", "~/", "file:")):
            return "error", "that looks like a URL or path — the key is the token itself"
        if raw.startswith(("env:", "store:", "keyring:")):
            return "warn", "that is a secret reference; it will be stored as-is and resolved at call time"
        if raw.startswith("$"):
            return "warn", "that is an environment variable name; did you mean the value it holds?"
        if len(raw) < 12:
            return "warn", f"unusually short for an API key ({len(raw)} characters)"
        if any(char in raw for char in "\"'`"):
            return "warn", "the value contains quotes — they are usually part of the shell line, not the key"
        shape = _KEY_SHAPES.get((self.vendor or "").lower())
        if shape is not None and not shape.match(raw):
            return "warn", f"does not look like a {self.vendor} key shape ({self.fingerprint(raw)}) — continue anyway?"
        if self.vendor:
            for other, pattern in _KEY_SHAPES.items():
                if other != self.vendor.lower() and pattern.match(raw):
                    return "warn", f"this looks like a {other} key, but you selected {self.vendor}"
        return "ok", f"looks right · {self.fingerprint(raw)}"

    @on(Input.Changed, "#key-input")
    def _changed(self, event: Input.Changed) -> None:
        verdict, detail = self.inspect(event.value)
        self.set_state(verdict, detail)
        self.post_message(self.Changed(event.value.strip(), verdict, detail))

    @on(Input.Submitted, "#key-input")
    def _submitted(self, event: Input.Submitted) -> None:
        self.post_message(self.Submitted(event.value.strip()))


# --------------------------------------------------------------------------- #
# capability badges
# --------------------------------------------------------------------------- #
class CapabilityBadges(Static):
    """What a provider or model can actually do, as inline badges."""

    DEFAULT_CSS = """
    CapabilityBadges {
        height: auto;
        width: 100%;
        color: $text-muted;
    }
    """

    @staticmethod
    def render(
        *,
        tools: bool | None = None,
        streaming: bool | None = None,
        reasoning: bool | None = None,
        vision: bool | None = None,
        json_mode: bool | None = None,
        listing: bool | None = None,
        local: bool = False,
        context_window: int | None = None,
        cost_in: float | None = None,
        cost_out: float | None = None,
    ) -> str:
        parts: list[str] = []

        def badge(symbol: str, label: str, state: bool | None, *, good: str = "green", bad: str = "dim") -> None:
            if state is None:
                return
            color = good if state else bad
            mark = "✓" if state else "✕"
            parts.append(f"[{color}]{mark}[/] {label}")

        badge("✓", "tools", tools)
        badge("✓", "stream", streaming)
        badge("✓", "reasoning", reasoning, good="magenta")
        badge("✓", "vision", vision, good="magenta")
        badge("✓", "json", json_mode)
        badge("✓", "lists models", listing)
        if local:
            parts.append("[bold green]◆[/] local · no key")
        if context_window:
            parts.append(f"[bold]▤[/] {format_context(context_window)}")
        if cost_in is not None or cost_out is not None:
            parts.append(f"[dim]$ {format_cost(cost_in, cost_out)}[/]")
        return "   ".join(parts)

    @staticmethod
    def from_entry(entry: ProviderEntry) -> str:
        return CapabilityBadges.render(
            tools=entry.supports_tools,
            streaming=entry.supports_streaming,
            reasoning=entry.supports_reasoning,
            vision=entry.supports_vision,
            json_mode=entry.supports_json_mode,
            listing=entry.model_listing != "none",
            local=entry.local,
        )

    @staticmethod
    def from_model(info: ModelInfo) -> str:
        return CapabilityBadges.render(
            tools=info.supports_tools,
            streaming=None,
            reasoning=info.supports_reasoning,
            vision=info.supports_vision,
            context_window=info.context_window,
            cost_in=info.input_cost_per_mtok,
            cost_out=info.output_cost_per_mtok,
        )


def format_context(window: int | None) -> str:
    if not window:
        return "—"
    if window >= 1_000_000:
        return f"{window / 1_000_000:.1f}M ctx".replace(".0M", "M")
    if window >= 1000:
        return f"{window // 1000}k ctx"
    return f"{window} ctx"


def format_cost(cost_in: float | None, cost_out: float | None) -> str:
    if cost_in is None and cost_out is None:
        return "—"
    left = "0" if cost_in is None else (f"{cost_in:g}" if cost_in < 10 else f"{cost_in:.0f}")
    right = "0" if cost_out is None else (f"{cost_out:g}" if cost_out < 10 else f"{cost_out:.0f}")
    if left in {"0", "0.0"} and right in {"0", "0.0"}:
        return "free"
    return f"{left}/{right} per Mtok"


# --------------------------------------------------------------------------- #
# generic chooser with consequences
# --------------------------------------------------------------------------- #
class GridChoice:
    """One row in an :class:`OptionGrid`."""

    def __init__(
        self,
        value: str,
        label: str,
        detail: str = "",
        *,
        badge: str = "",
        implies: Sequence[str] = (),
        prompt: str | None = None,
        selected: bool = False,
    ) -> None:
        self.value = value
        self.label = label
        self.detail = detail
        self.badge = badge
        self.implies = list(implies)
        #: When set, choosing this option opens a free-text field (custom answer).
        self.prompt = prompt
        self.selected = selected

    @property
    def title(self) -> str:
        parts = [self.label]
        if self.badge:
            parts.append(f"  [{self.badge}]")
        return "".join(parts)


class OptionGrid(Vertical):
    """A rich single-select list with a detail pane and optional free-text escape.

    ``OptionList`` gives the keyboard behaviour; this adds the part that makes a
    choice informed: the highlighted row's detail, what it implies, and a
    ``describe it myself`` entry whenever the caller allows custom answers.
    """

    DEFAULT_CSS = """
    OptionGrid {
        height: auto;
        width: 100%;
    }
    OptionGrid OptionList {
        height: auto;
        max-height: 16;
        width: 100%;
        background: $surface;
        border: round $panel-lighten-2 50%;
    }
    OptionGrid .grid-detail {
        height: auto;
        width: 100%;
        margin: 1 0 0 0;
        padding: 0 1;
        color: $text-muted;
        border-left: thick $primary 60%;
    }
    OptionGrid Input {
        width: 100%;
        margin-top: 1;
        display: none;
    }
    OptionGrid Input.visible {
        display: block;
    }
    """

    class Chosen(Message):
        def __init__(self, value: str, label: str, custom: bool = False) -> None:
            super().__init__()
            self.value = value
            self.label = label
            self.custom = custom

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    class Highlighted(Message):
        def __init__(self, choice: GridChoice | None) -> None:
            super().__init__()
            self.choice = choice

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    CUSTOM_VALUE = "__custom__"

    def __init__(
        self,
        choices: Sequence[GridChoice],
        *,
        allow_custom: bool = False,
        custom_prompt: str = "describe it in your own words",
        title: str = "",
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self.choices = list(choices)
        self.allow_custom = allow_custom
        self.custom_prompt = custom_prompt
        self.title_text = title
        self.custom_value = ""

    def compose(self):  # type: ignore[no-untyped-def]
        if self.title_text:
            yield Static(f"[bold]{self.title_text}[/]")
        options = [Option(choice.title, id=choice.value) for choice in self.choices]
        if self.allow_custom:
            options.append(Option("✎  describe it myself", id=self.CUSTOM_VALUE))
        yield OptionList(*options, id="grid-list")
        yield Static("", classes="grid-detail", id="grid-detail")
        yield Input(placeholder=self.custom_prompt, id="grid-custom")

    def on_mount(self) -> None:
        listing = self.query_one("#grid-list", OptionList)
        initial = next((index for index, choice in enumerate(self.choices) if choice.selected), 0)
        listing.highlighted = min(initial, max(0, len(self.choices) - 1))
        self._show_detail()

    def _listing(self) -> OptionList | None:
        try:
            return self.query_one("#grid-list", OptionList)
        except Exception:
            return None

    def _custom_field(self) -> Input | None:
        try:
            return self.query_one("#grid-custom", Input)
        except Exception:
            return None

    @property
    def value(self) -> str:
        listing = self._listing()
        option = listing.highlighted_option if listing is not None else None
        if option is None:
            # Before compose (or with nothing highlighted) the honest answer is the
            # pre-selected choice, which is what the user would get by pressing Enter.
            selected = next((choice for choice in self.choices if choice.selected), None)
            return (selected or (self.choices[0] if self.choices else None)).value if (selected or self.choices) else ""
        if option.id == self.CUSTOM_VALUE:
            field = self._custom_field()
            return (field.value.strip() if field is not None else "") or self.CUSTOM_VALUE
        return str(option.id or "")

    @property
    def is_custom(self) -> bool:
        listing = self._listing()
        option = listing.highlighted_option if listing is not None else None
        return bool(option and option.id == self.CUSTOM_VALUE)

    def _choice(self, value: str) -> GridChoice | None:
        return next((choice for choice in self.choices if choice.value == value), None)

    def _show_detail(self) -> None:
        listing = self._listing()
        detail = self.query_one("#grid-detail", Static)
        custom = self._custom_field()
        if listing is None or custom is None:
            return
        option = listing.highlighted_option
        if option is None:
            detail.update("")
            return
        if option.id == self.CUSTOM_VALUE:
            custom.add_class("visible")
            custom.focus(scroll_visible=False)
            detail.update("[italic dim]Your words become the requirement. Be concrete: the agent plans against exactly this.[/]")
            self.post_message(self.Highlighted(None))
            return
        custom.remove_class("visible")
        choice = self._choice(str(option.id or ""))
        if choice is None:
            detail.update("")
            return
        lines = [choice.detail] if choice.detail else []
        for implication in choice.implies[:4]:
            lines.append(f"[dim]→[/] {implication}")
        detail.update("\n".join(line for line in lines if line))
        self.post_message(self.Highlighted(choice))

    @on(OptionList.OptionHighlighted, "#grid-list")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._show_detail()

    @on(OptionList.OptionSelected, "#grid-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self._commit()

    @on(Input.Submitted, "#grid-custom")
    def _custom_submitted(self, event: Input.Submitted) -> None:
        self.custom_value = event.value.strip()
        self._commit()

    def _commit(self) -> None:
        listing = self._listing()
        option = listing.highlighted_option if listing is not None else None
        if option is None:
            return
        if option.id == self.CUSTOM_VALUE:
            field = self._custom_field()
            value = field.value.strip() if field is not None else ""
            if not value:
                if field is not None:
                    field.focus()
                return
            self.post_message(self.Chosen(value, value, custom=True))
            return
        choice = self._choice(str(option.id or ""))
        if choice is None:
            return
        self.post_message(self.Chosen(choice.value, choice.label))


# --------------------------------------------------------------------------- #
# planning decision panel
# --------------------------------------------------------------------------- #
class DecisionPanel(PanelCard):
    """One planning question rendered as a premium panel.

    Shows the question, its category and impact, the recommendation *with the
    reason*, and the implications of the highlighted option — the same content the
    Rich chooser shows, so planning looks like the same product in both surfaces.
    """

    DEFAULT_CSS = """
    DecisionPanel {
        height: auto;
        border: round $primary 70%;
    }
    DecisionPanel .decision-head {
        color: $text-muted;
        height: 1;
        width: 100%;
    }
    DecisionPanel .decision-why {
        height: auto;
        width: 100%;
        color: $accent;
        margin-top: 1;
    }
    """

    class Decided(Message):
        def __init__(self, axis: str, value: str, label: str, custom: bool = False) -> None:
            super().__init__()
            self.axis = axis
            self.value = value
            self.label = label
            self.custom = custom

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    def __init__(self, question: Any, *, index: int = 1, total: int = 1, id: str | None = None) -> None:
        self.question = question
        self.index = index
        self.total = total
        super().__init__(id=id or f"decision-{question.axis.id}")

    def compose(self):  # type: ignore[no-untyped-def]
        axis = self.question.axis
        rec = self.question.recommendation
        yield Static(
            f"question {self.index} of {self.total}   [bold magenta]{axis.category.upper()}[/]   "
            f"[bold yellow]impact {'●' * axis.impact}[/]",
            classes="decision-head",
        )
        yield Static(f"[b]{axis.question}[/]")
        if axis.blurb:
            yield Static(f"[dim]{axis.blurb}[/]", classes="panel-card-note")
        choices: list[GridChoice] = []
        for option in self.question.options:
            badge = ""
            if option.id == rec.option_id:
                badge = "bold green]✓ recommended"
            elif option.effort >= 4:
                badge = "yellow]heavy"
            elif option.effort <= 1:
                badge = "dim]light"
            choices.append(
                GridChoice(
                    option.id,
                    option.label,
                    option.description,
                    badge=f"{badge}" if badge else "",
                    implies=list(option.implications),
                    selected=option.id == rec.option_id,
                )
            )
        yield OptionGrid(
            choices,
            allow_custom=axis.allow_custom,
            custom_prompt="describe it in your own words",
            id="decision-grid",
        )
        yield Static(f"[italic]why {rec.label}: {rec.rationale}[/]", classes="decision-why")

    @on(OptionGrid.Chosen, "#decision-grid")
    def _chosen(self, event: OptionGrid.Chosen) -> None:
        self.post_message(self.Decided(self.question.axis.id, event.value, event.label, event.custom))


# --------------------------------------------------------------------------- #
# model table
# --------------------------------------------------------------------------- #
class ModelTable(DataTable):
    """Models with the metadata that makes a choice informed.

    Columns are chosen for decisions, not for completeness: context window (can it
    hold my repo?), tool support (can the agent drive it?), reasoning (can it think
    harder?), and cost (what will this run cost?).  Rows are ranked before they are
    shown, so the sensible default is at the top rather than alphabetically buried.
    """

    DEFAULT_CSS = """
    ModelTable {
        height: auto;
        max-height: 20;
        width: 100%;
        background: $surface;
        border: round $panel-lighten-2 50%;
    }
    """

    class ModelChosen(Message):
        def __init__(self, model_id: str) -> None:
            super().__init__()
            self.model_id = model_id

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    def __init__(self, *, id: str | None = None, name: str | None = None, cursor_type: str = "row") -> None:
        super().__init__(id=id, name=name, cursor_type=cursor_type, zebra_stripes=True)
        self._models: list[ModelInfo] = []

    def load(self, models: Sequence[ModelInfo], *, selected: str = "") -> None:
        self.clear(columns=True)
        self._models = list(models)
        self.add_columns(
            "model",
            "provider",
            "context",
            "tools",
            "reason",
            "vision",
            "$ in / out (per Mtok)",
        )
        for info in self._models:
            self.add_row(
                info.id,
                info.provider or "—",
                format_context(info.context_window),
                _yes_no(info.supports_tools),
                _yes_no(info.supports_reasoning),
                _yes_no(info.supports_vision),
                format_cost(info.input_cost_per_mtok, info.output_cost_per_mtok),
                key=info.id,
            )
        if selected:
            try:
                self.move_cursor(row=max(0, self._row_of(selected)))
            except Exception:  # pragma: no cover - cursor moves are best effort
                pass
        elif self.row_count:
            self.move_cursor(row=0)

    def _row_of(self, model_id: str) -> int:
        for index, info in enumerate(self._models):
            if info.id == model_id:
                return index
        return 0

    @property
    def value(self) -> str:
        if not self._models:
            return ""
        try:
            return self._models[self.cursor_row].id
        except (IndexError, Exception):  # pragma: no cover - uncomposed table
            return self._models[0].id

    def on_data_table_row_selected(self, event: Any) -> None:  # noqa: N802 - Textual hook name
        row = getattr(event, "row_key", None)
        if row is not None:
            self.post_message(self.ModelChosen(str(row.value)))


def _yes_no(state: bool | None) -> str:
    if state is None:
        return "[dim]?[/]"
    return "[bold green]✓[/]" if state else "[dim]✕[/]"


def rank_models(models: Iterable[ModelInfo], *, query: str = "", prefer_tools: bool = True) -> list[ModelInfo]:
    """Rank models for display: capable first, cheap and large-context early."""

    items = list(models)
    lowered = query.strip().lower()
    if lowered:
        items = [info for info in items if lowered in info.id.lower() or lowered in (info.name or "").lower()]

    def key(info: ModelInfo) -> tuple[Any, ...]:
        identifier = info.id.lower()
        embeddings = 1 if ("embed" in identifier or "whisper" in identifier or "tts" in identifier) else 0
        batch = 1 if identifier.endswith(":batch") or "-batch" in identifier else 0
        tools = 0 if (prefer_tools and info.supports_tools) else 1
        cost = (info.input_cost_per_mtok or 0.0) + (info.output_cost_per_mtok or 0.0)
        context = -(info.context_window or 0)
        return (embeddings, batch, tools, cost, context, identifier)

    items.sort(key=key)
    return items


# --------------------------------------------------------------------------- #
# palette swatches
# --------------------------------------------------------------------------- #
def _resolve_palette(palette: Palette | str | None) -> Palette:
    """``auto`` and empty both mean "the palette this terminal gets"."""

    if isinstance(palette, Palette):
        return palette
    key = (palette or "").strip().lower()
    if key in {"", "auto", "default"}:
        return get_palette(None)
    return get_palette(key)


class SwatchRow(Widget):
    """A palette rendered as colour chips with their token names."""

    DEFAULT_CSS = """
    SwatchRow {
        height: 4;
        width: 100%;
        padding: 0 1;
    }
    """

    TOKENS = ("brand", "brand_deep", "accent", "ok", "warn", "err", "info", "text", "text_muted", "border", "surface_raised")

    def __init__(self, palette: Palette | str | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.palette = _resolve_palette(palette)

    def set_palette(self, palette: Palette | str | None) -> None:
        self.palette = _resolve_palette(palette)
        self.refresh()

    def render(self) -> Any:
        from rich.text import Text

        text = Text()
        text.append(f"{self.palette.label}  ", style=f"bold {self.palette.color('brand')}")
        text.append(f"({self.palette.mode})", style=self.palette.color("text_muted"))
        text.append("\n")
        for token in self.TOKENS:
            color = self.palette.color(token)
            text.append("  ", style=f"on {color}")
            text.append(f" {token} ", style=f"on {color}")
            text.append("  ", style="")
            text.append(" ")
        text.append("\n")
        sample = Text.assemble(
            ("ok ", self.palette.color("ok")),
            ("warn ", self.palette.color("warn")),
            ("err ", self.palette.color("err")),
            ("info ", self.palette.color("info")),
            ("accent ", self.palette.color("accent")),
            ("muted ", self.palette.color("text_muted")),
        )
        text.append(sample)
        return text


class ThemePreviewCard(PanelCard):
    """Palette metadata plus its swatch row — the unit of the theme chooser."""

    DEFAULT_CSS = """
    ThemePreviewCard {
        height: auto;
        border: round $panel-lighten-2 50%;
    }
    ThemePreviewCard.selected {
        border: round $primary;
    }
    """


# --------------------------------------------------------------------------- #
# switch grid (permissions, flags)
# --------------------------------------------------------------------------- #
class SwitchItem:
    """One toggle with its explanation and risk label."""

    def __init__(
        self,
        key: str,
        label: str,
        detail: str = "",
        *,
        value: bool = False,
        risk: str = "",
        group: str = "general",
    ) -> None:
        self.key = key
        self.label = label
        self.detail = detail
        self.value = value
        self.risk = risk
        self.group = group


class SwitchGrid(Vertical):
    """A list of toggles where every row states what it costs you.

    Permission screens are where a product either builds trust or quietly takes
    rights away.  Each row therefore carries a risk label, and dangerous defaults
    are never pre-enabled.
    """

    DEFAULT_CSS = """
    SwitchGrid {
        height: auto;
        width: 100%;
    }
    SwitchGrid .switch-row {
        height: auto;
        width: 100%;
        padding: 0 1;
    }
    SwitchGrid .switch-row Switch {
        width: 8;
    }
    SwitchGrid .switch-label {
        width: 1fr;
        height: auto;
        padding: 0 1;
    }
    SwitchGrid .switch-label .detail {
        color: $text-muted;
    }
    """

    class Toggled(Message):
        def __init__(self, key: str, value: bool) -> None:
            super().__init__()
            self.key = key
            self.value = value

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    def __init__(self, items: Sequence[SwitchItem], *, id: str | None = None, name: str | None = None) -> None:
        super().__init__(id=id, name=name)
        self.items = list(items)

    def compose(self):  # type: ignore[no-untyped-def]
        for item in self.items:
            with Horizontal(classes="switch-row"):
                yield Switch(value=item.value, id=f"sw-{item.key}")
                risk = f"  [bold yellow]{item.risk}[/]" if item.risk else ""
                detail = f"\n[dim]{item.detail}[/]" if item.detail else ""
                yield Static(f"[b]{item.label}[/]{risk}{detail}", classes="switch-label")

    def values(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for item in self.items:
            try:
                out[item.key] = self.query_one(f"#sw-{item.key}", Switch).value
            except Exception:
                # Not composed yet: report the declared value, which is what the
                # switch will show once it is.
                out[item.key] = item.value
        return out

    def set_value(self, key: str, value: bool) -> None:
        try:
            self.query_one(f"#sw-{key}", Switch).value = value
        except Exception:  # pragma: no cover - unknown key
            pass

    @on(Switch.Changed)
    def _changed(self, event: Switch.Changed) -> None:
        key = str(event.switch.id or "").removeprefix("sw-")
        if key:
            self.post_message(self.Toggled(key, event.value))


# --------------------------------------------------------------------------- #
# reasoning ladder
# --------------------------------------------------------------------------- #
class ReasoningLadder(Vertical):
    """The depth ladder with what each rung actually costs and changes.

    Choosing a depth without seeing its consequences is how people end up paying
    for thinking they did not want, or getting shallow plans they did.  Each rung
    shows its thinking budget, its planning depth and its verification strictness.
    """

    DEFAULT_CSS = """
    ReasoningLadder {
        height: auto;
        width: 100%;
    }
    ReasoningLadder OptionList {
        height: auto;
        max-height: 12;
        border: round $panel-lighten-2 50%;
        background: $surface;
    }
    ReasoningLadder .ladder-detail {
        height: auto;
        color: $text-muted;
        margin-top: 1;
        padding: 0 1;
        border-left: thick $accent 60%;
    }
    """

    class Chosen(Message):
        def __init__(self, mode: str) -> None:
            super().__init__()
            self.mode = mode

        @property
        def control(self) -> Widget | None:
            # ``_sender`` is filled in by ``Message.__post_init__`` from the active
            # message pump, i.e. it is already set by the time the ``@on`` selector
            # is evaluated during dispatch.
            return self._sender  # type: ignore[attr-defined,return-value]

    def __init__(self, depths: Sequence[Any], *, selected: str = "high", budget: int = 24000, id: str | None = None) -> None:
        super().__init__(id=id)
        self.depths = list(depths)
        self.selected_mode = selected
        self.budget = budget

    def compose(self):  # type: ignore[no-untyped-def]
        options: list[Option] = []
        for depth in self.depths:
            tokens = depth.budget_from(self.budget)
            label = f"{depth.badge}  {depth.label:<12} {tokens:>7,} thinking tok · planning {depth.planning_depth}"
            options.append(Option(label, id=depth.mode))
        yield OptionList(*options, id="ladder-list")
        yield Static("", classes="ladder-detail", id="ladder-detail")

    def on_mount(self) -> None:
        listing = self.query_one("#ladder-list", OptionList)
        index = next((i for i, depth in enumerate(self.depths) if depth.mode == self.selected_mode), 0)
        listing.highlighted = index
        self._detail()

    @property
    def value(self) -> str:
        try:
            option = self.query_one("#ladder-list", OptionList).highlighted_option
        except Exception:
            option = None
        return str(option.id) if option and option.id else (self.selected_mode or "high")

    def _detail(self) -> None:
        depth = next((item for item in self.depths if item.mode == self.value), None)
        try:
            detail = self.query_one("#ladder-detail", Static)
        except Exception:  # pragma: no cover - before compose
            return
        if depth is None:
            detail.update("")
            return
        lines = [
            f"[b]{depth.tagline}[/]",
            f"[dim]alternatives considered: {depth.alternatives} · verification: {depth.verification} · "
            f"self-heal budget: +{depth.heal_budget} · adversarial review: {'yes' if depth.adversarial_review else 'no'}[/]",
        ]
        detail.update("\n".join(lines))

    @on(OptionList.OptionHighlighted, "#ladder-list")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._detail()

    @on(OptionList.OptionSelected, "#ladder-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self._detail()
        self.post_message(self.Chosen(self.value))


# --------------------------------------------------------------------------- #
# orchestration board
# --------------------------------------------------------------------------- #
class OrchestrationBoard(Widget):
    """Live view of a conducted run: one row per worker, with its state."""

    DEFAULT_CSS = """
    OrchestrationBoard {
        height: auto;
        width: 100%;
        padding: 0 1;
    }
    """

    STATE_STYLE = {
        "pending": ("dim", "○"),
        "queued": ("cyan", "◌"),
        "running": ("bold magenta", "▶"),
        "succeeded": ("bold green", "✓"),
        "failed": ("bold red", "✕"),
        "blocked": ("bold yellow", "⛔"),
        "cancelled": ("dim", "—"),
        "skipped": ("dim", "·"),
    }

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._rows: list[dict[str, Any]] = []

    def set_graph(self, graph: Any) -> None:
        self._rows = [
            {
                "id": item.id,
                "role": item.role,
                "label": item.role,
                "task": getattr(item, "task", ""),
                "state": getattr(item.state, "value", str(item.state)),
                "deps": list(item.depends_on or ()),
                "attempt": int(getattr(item, "attempts", 0) or 0),
            }
            for item in graph.items
        ]
        self.refresh()

    def update_state(self, work_id: str, state: str, *, note: str = "", attempt: int | None = None) -> None:
        for row in self._rows:
            if row["id"] == work_id:
                row["state"] = state
                if note:
                    row["note"] = note
                if attempt is not None:
                    row["attempt"] = attempt
                break
        self.refresh()

    def render(self) -> Any:
        from rich.text import Text

        out = Text()
        if not self._rows:
            out.append("no orchestration running", style="dim")
            return out
        for index, row in enumerate(self._rows):
            color, mark = self.STATE_STYLE.get(row["state"], ("dim", "·"))
            out.append(f"{mark} ", style=color)
            out.append(f"{row['role']:<18}", style="bold")
            task = (row.get("note") or row["task"])[:56]
            out.append(task, style="dim")
            if row.get("attempt", 0) > 1:
                out.append(f"  ↻{row['attempt']}", style="yellow")
            deps = row.get("deps") or []
            if deps:
                out.append(f"  ← {len(deps)}", style="dim")
            if index < len(self._rows) - 1:
                out.append("\n")
        return out
