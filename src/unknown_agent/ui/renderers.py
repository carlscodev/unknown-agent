"""Reusable renderables: banner, plans, tool cards, diffs, gate tables.

Commands compose these instead of building Rich markup inline, so the product
stays visually consistent and a redesign happens in one file.
"""

from __future__ import annotations

import difflib
import json
import time
from collections.abc import Iterable, Sequence
from typing import Any

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unknown_agent.ui.console import output
from unknown_agent.ui.theme import glyph
from unknown_agent.version import __version__

ASCII_BANNER = r"""
 _   _ _   _ _  ___   __   ___  _   _ _____
| | | | \ | | |/ / \ \ / /  / _ \| | | |___ |
| |_| |  \| | ' /   \ V /  | |_| | |_| |  _|
 \___/|_|\__|_|\_\    \_/    \___/ \___/|_|
"""


def banner(*, subtitle: str | None = None, compact: bool = False) -> Panel:
    if compact:
        title = Text()
        title.append("Unknown Agent", style="ua.brand")
        title.append(f"  v{__version__}", style="ua.muted")
        if subtitle:
            title.append(f"\n{subtitle}", style="ua.subtitle")
        return Panel(title, border_style="ua.panel.border", padding=(0, 2))
    art = Text(ASCII_BANNER.strip("\n"), style="ua.brand")
    meta = Text()
    meta.append("hyperagent for building real software", style="ua.subtitle")
    meta.append(f"\nv{__version__}", style="ua.muted")
    if subtitle:
        meta.append(f"\n{subtitle}", style="ua.muted")
    return Panel(Group(art, Text(), meta), border_style="ua.panel.border", padding=(1, 3))


def header(title: str, subtitle: str | None = None, *, icon: str | None = None) -> None:
    text = Text()
    if icon:
        text.append(f"{icon} ")
    text.append(title, style="ua.title")
    if subtitle:
        text.append(f"  {subtitle}", style="ua.subtitle")
    output.print(text)


def kv_table(rows: Sequence[tuple[str, Any]], *, title: str | None = None, show_header: bool = False) -> Table:
    table = Table(title=title, show_header=show_header, box=None, pad_edge=False, title_style="ua.panel.title")
    table.add_column(style="ua.kv.key", no_wrap=True)
    table.add_column(style="ua.kv.value", overflow="fold")
    for key, value in rows:
        table.add_row(str(key), _stringify(value))
    return table


def data_table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str | None = None,
    styles: Sequence[str] | None = None,
    footer: str | None = None,
) -> Table:
    table = Table(title=title, title_style="ua.panel.title", header_style="ua.muted", box=None, pad_edge=False)
    for index, column in enumerate(columns):
        table.add_column(column, style=(styles[index] if styles and index < len(styles) else None))
    for row in rows:
        table.add_row(*[_stringify(cell) for cell in row])
    if footer:
        table.caption = footer
        table.caption_style = "ua.muted"
    return table


def status_label(ok: bool | None, *, pass_text: str = "pass", fail_text: str = "fail", skip_text: str = "skip") -> Text:
    if ok is True:
        return Text(f"{glyph('check', unicode=output.unicode)} {pass_text}", style="ua.gate.pass")
    if ok is False:
        return Text(f"{glyph('cross', unicode=output.unicode)} {fail_text}", style="ua.gate.fail")
    return Text(f"{glyph('bullet', unicode=output.unicode)} {skip_text}", style="ua.gate.skip")


_STATUS_MAP = {"passed": True, "failed": False, "error": False, "skipped": None, "pending": None, "running": None}


def gate_table(results: Sequence[Any]) -> Table:
    """Render validation results (objects exposing name/status/duration/required)."""

    table = Table(box=None, pad_edge=False, header_style="ua.muted", title="Quality gates", title_style="ua.panel.title")
    table.add_column("gate", style="ua.tool", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("time", justify="right", style="ua.muted", no_wrap=True)
    table.add_column("notes", style="ua.obs", overflow="fold")
    for result in results:
        status = str(getattr(result, "status", "skipped"))
        if "." in status and status.split(".")[0].isidentifier():  # GateStatus.SKIPPED -> skipped
            status = status.split(".")[-1].lower()
        advisory = "" if getattr(result, "required", False) else " [ua.dim](advisory)[/ua.dim]"
        notes = (getattr(result, "summary", "") or "").strip().splitlines()
        table.add_row(
            f"{getattr(result, 'name', '?')}{advisory}",
            status_label(_STATUS_MAP.get(status), skip_text=status),
            f"{getattr(result, 'duration_s', 0.0):.1f}s",
            notes[0][:120] if notes else "",
        )
    return table


def tool_card(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    result: str | None = None,
    ok: bool | None = None,
    duration_s: float | None = None,
    risk: str | None = None,
    title: str | None = None,
) -> Panel:
    header_text = Text()
    header_text.append("tool ", style="ua.muted")
    header_text.append(name, style="ua.tool.name")
    if risk:
        header_text.append(f"  [{risk}]", style="ua.muted")
    if duration_s is not None:
        header_text.append(f"  {duration_s:.2f}s", style="ua.muted")
    if ok is True:
        header_text.append(f"  {glyph('check', unicode=output.unicode)}", style="ua.ok.dim")
    elif ok is False:
        header_text.append(f"  {glyph('cross', unicode=output.unicode)}", style="ua.err")

    body: list[RenderableType] = [header_text]
    if args:
        body.extend(["", _args_renderable(args)])
    if result:
        body.extend(["", Text(result[:4000], style="ua.obs")])
    border = "ua.panel.border" if ok is None else ("ua.success.border" if ok else "ua.error.border")
    return Panel(Group(*body), border_style=border, padding=(0, 2), title=title)


def _args_renderable(args: dict[str, Any]) -> RenderableType:
    if any(isinstance(value, str) and "\n" in value for value in args.values()):
        text = Text()
        for key, value in args.items():
            text.append(f"{key}: ", style="ua.tool.arg")
            text.append(f"{_stringify(value)}\n", style="ua.obs")
        return text
    return Text(json.dumps(args, ensure_ascii=False, default=str)[:600], style="ua.tool.arg")


def unified_diff(diff_text: str, *, context_lines: int = 3) -> RenderableType:
    if not diff_text.strip():
        return Text("no changes", style="ua.muted")
    return Syntax(diff_text, "diff", theme="ansi_dark", line_numbers=False, word_wrap=False, background_color="default")


def file_diff(before: str, after: str, path: str, *, context_lines: int = 3) -> RenderableType:
    diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context_lines,
            lineterm="",
        )
    )
    return unified_diff(diff, context_lines=context_lines)


def plan_tree(steps: Sequence[Any], *, title: str = "Plan") -> Tree:
    tree = Tree(Text(title, style="ua.plan"))
    for index, step in enumerate(steps, start=1):
        label = getattr(step, "title", None) or getattr(step, "description", str(step))
        status = str(getattr(step, "status", "pending"))
        icon = {
            "completed": (glyph("check", unicode=output.unicode), "ua.ok.dim"),
            "in_progress": (glyph("arrow", unicode=output.unicode), "ua.accent"),
            "failed": (glyph("cross", unicode=output.unicode), "ua.err"),
            "blocked": (glyph("warn", unicode=output.unicode), "ua.warn"),
        }.get(status, (glyph("bullet", unicode=output.unicode), "ua.muted"))
        node_text = Text()
        node_text.append(f"{icon[0]} ", style=icon[1])
        node_text.append(f"{index}. ", style="ua.muted")
        node_text.append(label, style="ua.obs")
        tools = getattr(step, "tools", None) or []
        if tools:
            node_text.append(f"  ({', '.join(tools)})", style="ua.dim")
        node = tree.add(node_text)
        for criterion in getattr(step, "acceptance", None) or []:
            node.add(Text(f"{glyph('bullet', unicode=output.unicode)} {criterion}", style="ua.muted"))
    return tree


def step_header(index: int, total: int | None, description: str, *, elapsed_s: float | None = None) -> None:
    text = Text()
    text.append(f"step {index}", style="ua.step")
    if total:
        text.append(f"/{total}", style="ua.muted")
    text.append("  ")
    text.append(description, style="ua.obs")
    if elapsed_s is not None and output.show_timings:
        text.append(f"  {elapsed_s:.1f}s", style="ua.muted")
    output.rule(characters="─" if output.unicode else "-")
    output.print(text)


def thought(text: str) -> None:
    if output.quiet:
        return
    output.print(Panel(Text(text.strip(), style="ua.think"), border_style="ua.dim", padding=(0, 2), title="[ua.muted]thinking[/ua.muted]"))


def observation(text: str, *, truncated: bool = False) -> None:
    text = text.rstrip()
    if truncated:
        text += "\n[ua.muted]... output truncated[/ua.muted]"
    output.print(Text(text, style="ua.obs"))


def markdown(text: str) -> Markdown:
    return Markdown(text, code_theme="ansi_dark", inline_code_theme="ansi_dark")


def bullets(items: Sequence[str], *, style: str = "ua.obs") -> Text:
    text = Text()
    for item in items:
        text.append(f"{glyph('bullet', unicode=output.unicode)} ", style="ua.muted")
        text.append(f"{item}\n", style=style)
    return text


def summary_panel(title: str, rows: Sequence[tuple[str, Any]], *, style: str = "ua.panel.border") -> Panel:
    return Panel(kv_table(rows), title=f"[ua.panel.title]{title}[/ua.panel.title]", border_style=style, padding=(0, 2))


def elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def token_usage(prompt_tokens: int, completion_tokens: int, *, cost_usd: float | None = None) -> Text:
    text = Text()
    text.append(f"{prompt_tokens:,} in", style="ua.cost")
    text.append(" / ", style="ua.muted")
    text.append(f"{completion_tokens:,} out", style="ua.cost")
    if cost_usd is not None:
        text.append(f"  ${cost_usd:.4f}", style="ua.cost")
    return text


def columns(items: Sequence[RenderableType], *, equal: bool = True) -> Columns:
    return Columns(list(items), equal=equal, expand=True)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_stringify(item) for item in value) or "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def timestamp(when: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when or time.time()))
