"""The interactive ask-and-choose panel system.

One protocol, three renderings.  Planning code asks questions through
:class:`AskUI` and never knows whether a human is looking at a Textual panel, a
Rich inline chooser, or nothing at all:

* :class:`unknown_agent.tui.panels.TuiAskUI`      full-screen Textual panels
* :class:`RichAskUI`                              inline Rich chooser (this module)
* :class:`HeadlessAskUI`                          CI/tests: recommendations only

The Rich chooser is not a fallback afterthought — it is the version that works
over SSH on a 25-line terminal, inside ``script``, in a tmux split and on Windows
conhost, so it is written to the same standard: arrow-key navigation, a visible
recommendation with its reason, implications revealed before you commit, and a
custom-answer escape hatch on every question.

Interaction contract (identical in every rendering):

===========  ==========================================================
↑ / ↓        move between options
Enter        choose the highlighted option
Tab          jump to "describe it myself"
Esc          accept the recommendation and move on
?            reveal implications for the highlighted option
Ctrl-C       abort planning (never aborts the program silently)
===========  ==========================================================

Rendering note: column-level ``style=`` on :class:`rich.table.Table` is avoided
everywhere in this module.  Rich resolves those through ``Console.get_style``
while *measuring*, on a console that may not carry our theme, and raises
``MissingStyle``.  Styles live on cells (:class:`rich.text.Text`), which degrade
gracefully.  That is a hard-won rule, not a preference.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from unknown_agent.planning.clarify import PlannedQuestion, QuestionPlan, resolve_custom_answer
from unknown_agent.planning.models import Answer, Blueprint, DecisionOption
from unknown_agent.ui.console import output
from unknown_agent.ui.theme import glyph

__all__ = [
    "AskUI",
    "AskResult",
    "RichAskUI",
    "HeadlessAskUI",
    "question_panel",
    "blueprint_panel",
    "resolve_plan",
]


@dataclass(slots=True)
class AskResult:
    """What one question produced."""

    answer: Answer
    revealed: bool = False
    skipped: bool = False
    aborted: bool = False


@runtime_checkable
class AskUI(Protocol):
    """The surface planning code may use.  Implemented by every rendering."""

    def ask(self, question: PlannedQuestion, *, index: int = 1, total: int = 1) -> AskResult:
        """Ask one question and return the resolved answer."""

    def confirm_blueprint(self, blueprint: Blueprint, *, warnings: Sequence[str] = ()) -> bool:
        """Show the assembled decisions and ask for approval."""

    def notify(self, message: str, *, kind: str = "info") -> None:
        """A short, non-blocking message."""

    def progress(self, done: int, total: int, label: str) -> None:
        """Update the question rail."""


# --------------------------------------------------------------------------- #
# rendering primitives (shared by every renderer)
# --------------------------------------------------------------------------- #
def _option_line(
    option: DecisionOption,
    *,
    selected: bool,
    recommended: bool,
    unicode: bool = True,
    width: int = 78,
) -> Text:
    marker = glyph("diamond", unicode=unicode) if selected else glyph("hollow", unicode=unicode)
    line = Text()
    line.append(f" {marker} ", style="ua.brand" if selected else "ua.faint")
    line.append(option.label, style="bold ua.text" if selected else "ua.text")
    if recommended:
        line.append("  recommended", style="ua.ok")
    if option.effort >= 4:
        line.append("  · heavy", style="ua.warn")
    elif option.effort <= 1:
        line.append("  · light", style="ua.muted")
    if option.description:
        remaining = max(12, width - len(line.plain))
        description = option.description if len(option.description) <= remaining else option.description[: remaining - 1] + "…"
        line.append(f"  {description}", style="ua.muted")
    return line


def question_panel(
    question: PlannedQuestion,
    *,
    highlighted: int = 0,
    reveal: bool = False,
    index: int = 1,
    total: int = 1,
    unicode: bool = True,
    width: int = 96,
    show_hints: bool = True,
    max_options: int | None = None,
) -> Panel:
    """Render one question as a panel: options, recommendation, implications, hints."""

    options = list(question.options)
    overflow = 0
    if max_options and len(options) > max_options:
        # Keep the recommendation visible: window the list around it.
        rec_index = next((i for i, o in enumerate(options) if o.id == question.recommendation.option_id), 0)
        start = max(0, min(rec_index - 1, len(options) - max_options))
        overflow = len(options) - max_options
        options = options[start : start + max_options]
        highlighted = max(0, min(highlighted - start, len(options) - 1))
    highlighted = max(0, min(highlighted, len(options) - 1)) if options else 0
    rec_id = question.recommendation.option_id

    head = Text()
    head.append(f"question {index} of {total}", style="ua.muted")
    head.append("    ", "")
    head.append(question.axis.category.upper(), style="ua.accent")
    head.append("    ", "")
    head.append("impact ", style="ua.muted")
    head.append(glyph("circle", unicode=unicode) * question.axis.impact, style="ua.warn")

    body: list[Any] = [head, Text("")]
    body.append(Text(question.axis.question, style="bold ua.title"))
    if question.axis.blurb:
        body.append(Text(question.axis.blurb, style="ua.text.dim"))
    body.append(Text(""))

    if question.axis.prefers_text and rec_id in {"custom", "none"}:
        hint = Text()
        hint.append("  Better answered in your own words — ", style="ua.text.dim")
        hint.append("press Tab to describe it.", style="ua.info")
        body.append(hint)
        body.append(Text(""))

    for position, option in enumerate(options):
        body.append(
            _option_line(
                option,
                selected=position == highlighted,
                recommended=option.id == rec_id,
                unicode=unicode,
                width=width - 10,
            )
        )
        if reveal and position == highlighted:
            for implication in option.implications[:4]:
                detail = Text()
                detail.append("      ", "")
                detail.append(f"{glyph('arrow', unicode=unicode)} ", style="ua.muted")
                detail.append(implication, style="ua.text.dim")
                body.append(detail)
            if option.risk >= 3:
                warn = Text()
                warn.append("      ", "")
                warn.append(f"{glyph('warn', unicode=unicode)} ", style="ua.warn")
                warn.append(f"carries real risk ({option.risk}/5)", style="ua.warn")
                body.append(warn)

    if overflow:
        more = Text()
        more.append("      ", "")
        more.append(f"{glyph('ellipsis', unicode=unicode)} {overflow} more — keep scrolling", style="ua.faint")
        body.append(more)

    if question.allows_custom:
        body.append(Text())
        custom = Text()
        custom.append(f" {glyph('hollow', unicode=unicode)} ", style="ua.faint")
        custom.append("describe it myself", style="ua.info")
        custom.append("   (Tab)", style="ua.faint")
        body.append(custom)

    rationale = question.recommendation.rationale
    if rationale:
        body.append(Text(""))
        why = Text()
        why.append("  why this one: ", style="ua.muted")
        why.append(rationale, style="ua.think")
        body.append(why)

    if show_hints:
        body.append(Text(""))
        body.append(
            Text(
                "  ↑↓ choose · Enter confirm · ? implications · Tab describe · Esc take recommendation · Ctrl-C abort",
                style="ua.faint",
            )
        )

    return Panel(
        Group(*body),
        border_style="ua.panel.border",
        title="[ua.panel.title]planning[/ua.panel.title]",
        subtitle=f"[ua.muted]{question.axis.id}[/ua.muted]",
        padding=(0, 1),
        width=min(width, output.data.width or width),
    )


def blueprint_panel(
    blueprint: Blueprint,
    *,
    warnings: Sequence[str] = (),
    unicode: bool = True,
    width: int = 104,
) -> Panel:
    """The final review: every decision, where it came from, and what it implies."""

    from unknown_agent.planning.axes import AXES, AXIS_ORDER

    table = Table(box=None, show_header=True, pad_edge=False, expand=False)
    table.add_column(Text("decision", style="ua.muted"), no_wrap=True)
    table.add_column(Text("choice", style="ua.muted"), no_wrap=True)
    table.add_column(Text("source", style="ua.muted"), no_wrap=True)
    table.add_column(Text("why", style="ua.muted"), max_width=42)

    source_glyph = {
        "user": glyph("check", unicode=unicode),
        "default": glyph("hollow", unicode=unicode),
        "inferred": glyph("arrow", unicode=unicode),
        "preset": glyph("layers", unicode=unicode),
        "model": glyph("spark", unicode=unicode),
        "flag": glyph("wrench", unicode=unicode),
    }
    ordered = [axis_id for axis_id in AXIS_ORDER if axis_id in blueprint.answers]
    ordered += [axis_id for axis_id in blueprint.answers if axis_id not in AXIS_ORDER]
    for axis_id in ordered:
        answer = blueprint.answers[axis_id]
        axis = AXES.get(axis_id)
        label = axis.question.rstrip("?") if axis else axis_id.replace("_", " ")
        table.add_row(
            Text(label[:30], style="ua.text"),
            Text(answer.display[:34], style="bold ua.brand", overflow="ellipsis"),
            Text(f"{source_glyph.get(answer.source, '·')} {answer.source}", style="ua.faint"),
            Text((answer.rationale or "")[:58], style="ua.text.dim", overflow="ellipsis"),
        )

    body: list[Any] = [
        Text.assemble(("Goal  ", "ua.muted"), (blueprint.goal, "ua.title")),
        Text(""),
        table,
    ]
    if blueprint.constraints:
        body.append(Text(""))
        body.append(Text.assemble(("Constraints  ", "ua.muted"), ("; ".join(blueprint.constraints), "ua.text.dim")))
    if blueprint.non_goals:
        body.append(Text.assemble(("Not building  ", "ua.muted"), ("; ".join(blueprint.non_goals), "ua.text.dim")))
    if warnings:
        body.append(Text(""))
        for warning in warnings:
            body.append(Text.assemble((f"  {glyph('warn', unicode=unicode)} ", "ua.warn"), (warning, "ua.warn")))
    asked = len(blueprint.user_decided)
    total = max(1, len(blueprint.answers))
    body.append(Text(""))
    body.append(
        Text.assemble(
            (f"{asked}/{total}", "ua.ok"),
            (" decisions are yours; the rest are recommendations you can revisit with ", "ua.muted"),
            ("ua plan ask", "ua.info"),
            (".", "ua.muted"),
        )
    )
    return Panel(
        Group(*body),
        title="[ua.panel.title]blueprint[/ua.panel.title]",
        subtitle="[ua.muted]review before execution[/ua.muted]",
        border_style="ua.ok.border",
        padding=(1, 1),
        width=min(width, output.data.width or width),
    )


# --------------------------------------------------------------------------- #
# Rich implementation: a real keyboard chooser, not a numbered list
# --------------------------------------------------------------------------- #
def _raw_available() -> bool:
    """Raw arrow-key mode is POSIX-only; Windows gets the numbered chooser."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        return False
    return True


class _RawChooser:
    """Minimal raw-mode key reader.  No dependency, no surprises, always restores."""

    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._old: Any = None

    def __enter__(self) -> "_RawChooser":
        import termios
        import tty

        self._old = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        return self

    def __exit__(self, *exc: Any) -> None:
        import termios

        if self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            sys.stdout.write("\r\n")
            sys.stdout.flush()

    def read_key(self) -> str:
        ch = sys.stdin.read(1)
        if not ch:
            return "esc"
        if ch == "\x03":
            return "ctrl-c"
        if ch in {"\r", "\n"}:
            return "enter"
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(seq, "esc")
        if ch == "\t":
            return "tab"
        if ch == "\x7f":
            return "backspace"
        return ch


class RichAskUI:
    """Inline interactive chooser for terminals without (or without wanting) a TUI.

    Uses raw terminal mode when available so arrow keys work without Enter, and a
    numbered prompt when it is not.  Redraws in place through ``rich.live.Live`` so
    the screen never fills up with one question per keystroke.
    """

    def __init__(self, *, assume_yes: bool = False, non_interactive: bool = False, unicode: bool | None = None) -> None:
        self.assume_yes = assume_yes
        self.non_interactive = non_interactive or not sys.stdin.isatty()
        self.unicode = unicode if unicode is not None else output.unicode

    # -- AskUI ------------------------------------------------------------- #
    def notify(self, message: str, *, kind: str = "info") -> None:
        style = {"info": "ua.info", "warn": "ua.warn", "error": "ua.err", "ok": "ua.ok"}.get(kind, "ua.info")
        output.diag_print(Text(f"{glyph('bullet', unicode=self.unicode)} {message}", style=style))

    def progress(self, done: int, total: int, label: str) -> None:
        if self.non_interactive:
            return
        output.diag_print(Text(f"  {done}/{total} · {label}", style="ua.muted"))

    def ask(self, question: PlannedQuestion, *, index: int = 1, total: int = 1) -> AskResult:
        if self.non_interactive or self.assume_yes:
            return AskResult(answer=self._recommended(question), skipped=True)
        if not question.options:
            text = self._read_text(question.axis.question)
            return AskResult(answer=resolve_custom_answer(question.axis, text))
        if not _raw_available():
            return AskResult(answer=self._numbered(question, index, total))

        from rich.live import Live

        chooser = _RawChooser()
        state = {"highlight": self._initial_highlight(question), "reveal": False}

        def panel() -> Panel:
            return question_panel(
                question,
                highlighted=state["highlight"],
                reveal=state["reveal"],
                index=index,
                total=total,
                unicode=self.unicode,
            )

        with Live(panel(), console=output.data, refresh_per_second=20, transient=True) as live:
            with chooser:
                while True:
                    key = chooser.read_key()
                    if key in {"q", "ctrl-c"}:
                        raise KeyboardInterrupt
                    count = len(question.options)
                    if key in {"up", "k"}:
                        state["highlight"] = (state["highlight"] - 1) % count
                        state["reveal"] = False
                    elif key in {"down", "j"}:
                        state["highlight"] = (state["highlight"] + 1) % count
                        state["reveal"] = False
                    elif key == "?":
                        state["reveal"] = not state["reveal"]
                    elif key == "tab":
                        live.stop()
                        text = self._read_text(question.axis.question, prompt="describe it: ")
                        if text.strip():
                            return AskResult(answer=resolve_custom_answer(question.axis, text))
                        live.start(refresh=True)
                        continue
                    elif key in {"enter", " "}:
                        option = question.options[state["highlight"]]
                        is_rec = option.id == question.recommendation.option_id
                        return AskResult(
                            answer=Answer(
                                axis=question.axis.id,
                                value=option.id,
                                label=option.label,
                                source="user",
                                asked=True,
                                rationale=question.recommendation.rationale if is_rec else "",
                                confidence=question.recommendation.confidence if is_rec else 1.0,
                            ),
                            revealed=state["reveal"],
                        )
                    elif key == "esc":
                        return AskResult(answer=self._recommended(question), skipped=True)
                    elif key.isdigit():
                        position = int(key) - 1
                        if 0 <= position < count:
                            state["highlight"] = position
                    else:
                        continue
                    live.update(panel())

    def confirm_blueprint(self, blueprint: Blueprint, *, warnings: Sequence[str] = ()) -> bool:
        if self.non_interactive:
            return True
        output.print(blueprint_panel(blueprint, warnings=warnings, unicode=self.unicode))
        if self.assume_yes:
            return True
        from unknown_agent.ui.prompts import Prompter

        return Prompter(assume_yes=False, non_interactive=False).confirm(
            "Build this?",
            default=True,
            preview="  Enter to continue · Ctrl-C to abort · `ua plan ask <axis>` re-opens any decision later",
        )

    # -- internals --------------------------------------------------------- #
    def _recommended(self, question: PlannedQuestion) -> Answer:
        option = question.axis.option(question.recommendation.option_id) or (
            question.options[0] if question.options else None
        )
        if option is None:
            return Answer(axis=question.axis.id, value="custom", label="", source="default")
        return Answer(
            axis=question.axis.id,
            value=option.id,
            label=option.label,
            source="default",
            rationale=question.recommendation.rationale,
            confidence=question.recommendation.confidence,
        )

    def _initial_highlight(self, question: PlannedQuestion) -> int:
        for position, option in enumerate(question.options):
            if option.id == question.recommendation.option_id:
                return position
        return 0

    def _numbered(self, question: PlannedQuestion, index: int, total: int) -> Answer:
        """No raw mode (Windows, dumb terminals): print the list, read a number."""

        output.print(
            question_panel(
                question,
                highlighted=self._initial_highlight(question),
                reveal=True,
                index=index,
                total=total,
                unicode=self.unicode,
                show_hints=False,
            )
        )
        lines = Text()
        for position, option in enumerate(question.options, start=1):
            lines.append(f"  {position}", style="ua.brand")
            lines.append(f"  {option.label}", style="ua.text")
            if option.id == question.recommendation.option_id:
                lines.append("  (recommended)", style="ua.ok")
            lines.append("\n")
        lines.append("  0", style="ua.brand")
        lines.append("  describe it myself", style="ua.info")
        output.print(lines)
        default = str(self._initial_highlight(question) + 1)
        raw = self._read_text(f"choose [{default}]", default=default)
        if raw.strip() in {"0", "c", "custom"}:
            return resolve_custom_answer(question.axis, self._read_text("describe it"))
        try:
            position = int(raw) - 1
        except ValueError:
            return resolve_custom_answer(question.axis, raw)
        if 0 <= position < len(question.options):
            option = question.options[position]
            return Answer(axis=question.axis.id, value=option.id, label=option.label, source="user", asked=True)
        return self._recommended(question)

    def _read_text(self, question: str, *, prompt: str | None = None, default: str = "") -> str:
        from unknown_agent.ui.prompts import Prompter

        return Prompter(assume_yes=False, non_interactive=self.non_interactive).text(
            prompt or question, default=default or None, allow_empty=True
        )


class HeadlessAskUI:
    """Never blocks: every question resolves to its recommendation.

    Used by tests, CI, ``--non-interactive`` and ``--surprise-me``.  It records
    what it decided so a run can be audited afterwards.
    """

    def __init__(self, *, overrides: dict[str, str] | None = None) -> None:
        self.overrides = dict(overrides or {})
        self.decisions: list[Answer] = []
        self.notifications: list[str] = []

    def notify(self, message: str, *, kind: str = "info") -> None:
        self.notifications.append(f"{kind}: {message}")

    def progress(self, done: int, total: int, label: str) -> None:
        self.notifications.append(f"progress: {done}/{total} {label}")

    def ask(self, question: PlannedQuestion, *, index: int = 1, total: int = 1) -> AskResult:
        forced = self.overrides.get(question.axis.id)
        if forced:
            answer = resolve_custom_answer(question.axis, forced)
            answer.source = "flag"
            self.decisions.append(answer)
            return AskResult(answer=answer, skipped=True)
        option = question.axis.option(question.recommendation.option_id)
        answer = Answer(
            axis=question.axis.id,
            value=option.id if option else question.recommendation.option_id,
            label=option.label if option else question.recommendation.label,
            source="default",
            rationale=question.recommendation.rationale,
            confidence=question.recommendation.confidence,
        )
        self.decisions.append(answer)
        return AskResult(answer=answer, skipped=True)

    def confirm_blueprint(self, blueprint: Blueprint, *, warnings: Sequence[str] = ()) -> bool:
        return True


# --------------------------------------------------------------------------- #
# the driver: run a question plan through any AskUI
# --------------------------------------------------------------------------- #
def resolve_plan(
    plan: QuestionPlan,
    blueprint: Blueprint,
    ui: AskUI,
    *,
    confirm: bool = True,
    warnings: Sequence[str] = (),
) -> Blueprint:
    """Ask every planned question, apply consequences, then confirm.

    Consequences are applied as answers arrive, which means later questions see
    earlier decisions: choosing "static only" really does remove the auth question
    from the list instead of asking it and ignoring the answer.
    """

    from unknown_agent.planning.clarify import apply_answer

    pending = list(plan.ask)
    position = 0
    while position < len(pending):
        question = pending[position]
        position += 1
        if not question.axis.applies(blueprint.answers):
            blueprint.answers.pop(question.axis.id, None)
            continue
        if question.already_answered and question.existing is not None:
            continue
        ui.progress(position - 1, len(pending), question.axis.id)
        result = ui.ask(question, index=position, total=len(pending))
        if result.aborted:
            break
        applied = apply_answer(blueprint, result.answer)
        if applied:
            ui.notify("also decided: " + ", ".join(applied[:3]))

    if confirm:
        ui.confirm_blueprint(blueprint, warnings=warnings)
    return blueprint
