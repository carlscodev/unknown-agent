"""The visual layer of onboarding: every panel, table and prompt.

Design rules (they are product rules, not taste):

* **Spacing is a feature.**  Each step gets a blank line, a titled rule, and never
  more than one idea per screen.
* **The user always knows where they are.**  ``step 2 of 5`` is always visible.
* **The safe default is the default.**  Enter accepts it; destructive choices are
  never preselected.
* **Secrets are never echoed**, never logged, and never rendered back - the input
  prompt is masked and only a fingerprint (last 4 characters) is ever displayed.
* **Machine mode is respected.**  With ``--json`` these screens are never drawn;
  the flow emits structured events instead.
"""

from __future__ import annotations

from typing import Any, Sequence

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from unknown_agent.providers.catalog import ProviderEntry
from unknown_agent.providers.discovery import ModelInfo
from unknown_agent.ui.console import output

TOTAL_STEPS = 5


def banner() -> Panel:
    """The first thing a new user sees."""

    body = Text()
    body.append("Unknown Agent", style="ua.brand")
    body.append("  v" + _version(), style="ua.muted")
    body.append("\nA CLI hyperagent that builds, fixes and ships real software.\n", style="dim")
    body.append("\nLet's connect a model provider.", style="ua.info")
    body.append("  Takes about 30 seconds - ", style="dim")
    body.append("you will paste an API key and pick a model.", style="dim")
    return Panel(body, border_style="ua.panel.border", padding=(1, 3), title="[ua.panel.title]setup[/ua.panel.title]")


def step_header(index: int, title: str, subtitle: str = "") -> None:
    output.blank()
    head = Text()
    head.append(f"step {index} of {TOTAL_STEPS}", style="ua.muted")
    head.append(f"  {title}", style="ua.panel.title")
    output.print(head)
    if subtitle:
        output.print(Text(f"  {subtitle}", style="dim"))
    output.print(Text("  " + "─" * 64, style="ua.muted"))


def provider_table(entries: Sequence[ProviderEntry], *, highlight: Sequence[str] = ()) -> Table:
    """Pick a provider: name, dialect, capability badges, key source."""

    table = Table(
        box=None,
        pad_edge=False,
        header_style="ua.muted",
        title="providers",
        title_style="ua.panel.title",
        expand=False,
    )
    table.add_column("#", style="ua.muted", justify="right", no_wrap=True)
    table.add_column("provider", style="ua.kv.key", no_wrap=True)
    table.add_column("protocol", style="ua.muted", no_wrap=True)
    table.add_column("tools", justify="center", no_wrap=True)
    table.add_column("models", justify="center", no_wrap=True)
    table.add_column("key from", style="dim", no_wrap=True)
    table.add_column("notes", style="ua.muted", overflow="fold")

    for index, entry in enumerate(entries, start=1):
        tools = "✔" if entry.supports_tools else "—"
        models = "live" if entry.model_listing != "none" else "list"
        key_from = "no key" if not entry.api_key_required else entry.env_var
        note = entry.notes.split(".")[0][:58]
        if entry.key in highlight:
            note = "configured · " + note
        table.add_row(str(index), entry.label, entry.protocol, tools, models, key_from, note)
    return table


def model_table(models: Sequence[ModelInfo], *, limit: int = 25, currency: bool = True) -> Table:
    """Model chooser: id, context, capabilities, price - the facts that matter."""

    table = Table(box=None, pad_edge=False, header_style="ua.muted", title="models", title_style="ua.panel.title")
    table.add_column("#", style="ua.muted", justify="right", no_wrap=True)
    table.add_column("model", style="ua.kv.key", overflow="fold", max_width=48)
    table.add_column("context", style="ua.muted", justify="right", no_wrap=True)
    table.add_column("tools", justify="center", no_wrap=True)
    table.add_column("reason", justify="center", no_wrap=True)
    table.add_column("vision", justify="center", no_wrap=True)
    if currency:
        table.add_column("$/Mtok in·out", style="ua.muted", justify="right", no_wrap=True)

    for index, model in enumerate(models[:limit], start=1):
        row = [
            str(index),
            model.label if model.label != model.id else model.id,
            _tokens(model.context_window),
            _flag(model.supports_tools),
            "✔" if model.supports_reasoning else "",
            "✔" if model.supports_vision else "",
        ]
        if currency:
            row.append(_price(model))
        table.add_row(*row)
    if len(models) > limit:
        table.caption = f"showing {limit} of {len(models)} - type a number, a name fragment, or 'all'"
    return table


def key_prompt_panel(entry: ProviderEntry, *, existing_hint: str = "") -> Panel:
    """Explain exactly what is wanted before the (masked) input is requested."""

    body = Text()
    body.append("Paste your ", style="dim")
    body.append(entry.label, style="ua.brand")
    body.append(" API key and press Enter.", style="dim")
    body.append("\n\n  where to get one: ", style="ua.muted")
    body.append(entry.docs_url, style="ua.info")
    body.append("\n  stored as: ", style="ua.muted")
    body.append("~/.config/unknown-agent/credentials.toml (mode 0600), ", style="ua.muted")
    body.append("never in the project, never in logs", style="ua.muted")
    if not entry.api_key_required:
        body.append("\n\n  this provider needs no key - just press Enter", style="ua.ok")
    if existing_hint:
        body.append(f"\n\n  found existing key: {existing_hint}", style="ua.muted")
    body.append("\n\n  the key is masked as you type; input is validated immediately.", style="dim")
    return Panel(body, border_style="ua.panel.border", padding=(1, 2), title=f"[ua.panel.title]{entry.key}[/ua.panel.title]")


def success_panel(provider_label: str, model: str, *, models: int, config_path: str, next_steps: Sequence[str]) -> Panel:
    body = Text()
    body.append("✔ ", style="ua.ok")
    body.append("Ready to build.\n\n", style="ua.ok")
    body.append("  provider  ", style="ua.muted")
    body.append(f"{provider_label}\n", style="ua.kv.value")
    body.append("  model     ", style="ua.muted")
    body.append(f"{model}\n", style="ua.kv.value")
    body.append("  models    ", style="ua.muted")
    body.append(f"{models} available\n", style="ua.kv.value")
    body.append("  config    ", style="ua.muted")
    body.append(f"{config_path}\n", style="ua.kv.value")
    body.append("\n  next\n", style="ua.muted")
    for step in next_steps:
        body.append(f"    {step}\n", style="dim")
    return Panel(body, border_style="ua.ok.border", padding=(1, 3), title="[ua.panel.title]setup complete[/ua.panel.title]")


def failure_panel(title: str, message: str, *, hint: str = "", options: Sequence[str] = ()) -> Panel:
    body = Text()
    body.append(message, style="ua.err")
    if hint:
        body.append("\n\n  ", style="dim")
        body.append(hint, style="ua.info")
    if options:
        body.append("\n\n", style="dim")
        for option in options:
            body.append(f"  • {option}\n", style="dim")
    return Panel(body, border_style="ua.error.border", padding=(1, 2), title=f"[ua.err]{title}[/ua.err]")


def info_panel(title: str, message: str | Sequence[str], *, hint: str = "") -> Panel:
    """Neutral panel for outcomes that are neither success nor failure (e.g. cancel).

    ``message`` accepts a single string or pre-split lines; ``hint`` renders as a
    dimmed follow-up line, matching :func:`failure_panel`.
    """

    lines = [message] if isinstance(message, str) else list(message)
    body = Text()
    for line in lines:
        body.append(line + "\n", style="dim")
    if hint:
        body.append(hint, style="ua.muted")
    return Panel(body, border_style="ua.panel.border", padding=(0, 2), title=f"[ua.panel.title]{title}[/ua.panel.title]")


def capability_line(capabilities: dict[str, Any]) -> Text:
    """One-line truth about what this provider can do."""

    text = Text("  capabilities: ", style="ua.muted")
    for label, key in (("tools", "tools"), ("streaming", "streaming"), ("json", "json_mode"), ("model list", "model_listing"), ("reasoning", "reasoning"), ("vision", "vision")):
        value = capabilities.get(key)
        if value is None:
            continue
        text.append(f"{label} ", style="dim")
        text.append(("✔" if value else "—") + "  ", style="ua.ok" if value else "ua.muted")
    return text


def _flag(value: bool | None) -> str:
    if value is None:
        return "?"
    return "✔" if value else "—"


def _tokens(value: int | None) -> str:
    if not value:
        return "-"
    if value >= 1000:
        return f"{value // 1000}k"
    return str(value)


def _price(model: ModelInfo) -> str:
    if model.input_cost_per_mtok is None and model.output_cost_per_mtok is None:
        return "-"
    incoming = model.input_cost_per_mtok or 0.0
    outgoing = model.output_cost_per_mtok or 0.0
    return f"{incoming:.3g}·{outgoing:.3g}"


def _version() -> str:
    from unknown_agent.version import __version__

    return __version__


__all__ = [
    "TOTAL_STEPS",
    "banner",
    "capability_line",
    "failure_panel",
    "info_panel",
    "key_prompt_panel",
    "model_table",
    "provider_table",
    "step_header",
    "success_panel",
]
