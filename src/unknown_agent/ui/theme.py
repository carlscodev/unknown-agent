"""The Unknown Agent visual language.

One place defines every colour and style, so the CLI looks like a product rather
than a pile of Rich calls and so themes can be swapped without touching commands.

The palette data lives in :mod:`unknown_agent.ui.theme_defs` (dependency-free, so
config validation and the Textual TUI can share it).  This module turns a palette
into a :class:`rich.theme.Theme` and owns the *active* theme for the process:

    apply_theme("paper")            # from [appearance] theme, at CLI start
    output.print("[ua.brand]…")     # every renderer keeps working, recoloured

Style names are a **contract**.  ``ua.brand``, ``ua.ok``, ``ua.panel.border`` and
friends are referenced by dozens of renderers, so the token table below is
versioned: adding a token is fine, renaming one is a breaking change.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from unknown_agent.ui.theme_defs import (
    DEFAULT_THEME,
    GLYPH_SETS,
    PALETTE_ORDER,
    SEMANTIC_TOKENS,
    TOKEN_ORDER,
    Palette,
    contrast_ratio,
    detect_color_depth,
    detect_preferred_mode,
    get_palette,
    theme_names,
)

# --------------------------------------------------------------------------- #
# token table: semantic style names used across the whole product
# --------------------------------------------------------------------------- #
#: The style names renderers use.  Values are ``palette token`` + optional prefix.
STYLE_TABLE: dict[str, tuple[str, str]] = {
    # identity
    "ua.brand": ("bold {brand}", "brand"),
    "ua.brand.dim": ("{brand}", "brand"),
    "ua.brand.soft": ("on {brand_soft}", "brand_soft"),
    "ua.accent": ("bold {accent}", "accent"),
    "ua.accent.dim": ("{accent}", "accent"),
    "ua.accent.soft": ("on {accent_soft}", "accent_soft"),
    # status
    "ua.ok": ("bold {ok}", "ok"),
    "ua.ok.dim": ("{ok}", "ok"),
    "ua.warn": ("bold {warn}", "warn"),
    "ua.warn.dim": ("{warn}", "warn"),
    "ua.err": ("bold {err}", "err"),
    "ua.err.dim": ("{err}", "err"),
    "ua.info": ("{info}", "info"),
    "ua.link": ("underline {link}", "link"),
    # text hierarchy
    "ua.title": ("bold {text}", "text"),
    "ua.subtitle": ("{text_muted}", "text_muted"),
    "ua.text": ("{text}", "text"),
    "ua.text.dim": ("{text_dim}", "text_dim"),
    "ua.muted": ("{text_muted}", "text_muted"),
    "ua.faint": ("dim {text_faint}", "text_faint"),
    "ua.dim": ("dim {text_muted}", "text_muted"),
    # agent voice
    "ua.plan": ("bold {brand}", "brand"),
    "ua.think": ("italic {accent}", "accent"),
    "ua.tool": ("bold {text}", "text"),
    "ua.tool.name": ("bold {brand}", "brand"),
    "ua.tool.arg": ("{text_muted}", "text_muted"),
    "ua.obs": ("{text}", "text"),
    "ua.step": ("bold {brand}", "brand"),
    "ua.cost": ("{text_muted}", "text_muted"),
    # gates
    "ua.gate.pass": ("bold {ok}", "ok"),
    "ua.gate.fail": ("bold {err}", "err"),
    "ua.gate.skip": ("dim {text_muted}", "text_muted"),
    "ua.gate.warn": ("bold {warn}", "warn"),
    # diffs and code
    "ua.diff.add": ("{diff_add}", "diff_add"),
    "ua.diff.del": ("{diff_del}", "diff_del"),
    "ua.diff.hunk": ("bold {diff_hunk}", "diff_hunk"),
    "ua.diff.meta": ("{text_muted}", "text_muted"),
    "ua.code": ("{code}", "code"),
    "ua.path": ("underline {text_muted}", "text_muted"),
    # key/value
    "ua.kv.key": ("{text_muted}", "text_muted"),
    "ua.kv.value": ("{text}", "text"),
    # surfaces and borders
    "ua.panel.border": ("{border}", "border"),
    "ua.panel.border.dim": ("{border_dim}", "border_dim"),
    "ua.panel.title": ("bold {brand}", "brand"),
    "ua.ok.border": ("{ok}", "ok"),
    "ua.warn.border": ("{warn}", "warn"),
    "ua.err.border": ("{err}", "err"),
    "ua.error.border": ("{err}", "err"),
    "ua.success.border": ("{ok}", "ok"),
    "ua.surface": ("on {surface_raised}", "surface_raised"),
    "ua.selection": ("on {selection}", "selection"),
    # orchestration roles (each worker gets a distinguishable hue)
    "ua.role.planner": ("bold {brand}", "brand"),
    "ua.role.coder": ("bold {ok}", "ok"),
    "ua.role.inspector": ("bold {info}", "info"),
    "ua.role.refactor": ("bold {accent}", "accent"),
    "ua.role.ux": ("bold {accent}", "accent"),
    "ua.role.browser": ("bold {brand_deep}", "brand_deep"),
    "ua.role.docs": ("bold {text_dim}", "text_dim"),
    "ua.role.release": ("bold {warn}", "warn"),
    "ua.role.tooling": ("bold {text_muted}", "text_muted"),
    # reasoning depth ladder
    "ua.reason.low": ("{text_muted}", "text_muted"),
    "ua.reason.medium": ("{info}", "info"),
    "ua.reason.high": ("bold {brand}", "brand"),
    "ua.reason.deep": ("bold {accent}", "accent"),
    "ua.reason.ultra": ("bold {warn}", "warn"),
}

#: Backwards/forwards compatibility aliases so third-party renderers and older
#: saved configs keep working.
STYLE_ALIASES: dict[str, str] = {
    "ua.brand_deep": "ua.panel.border",
    "ua.heading": "ua.title",
    "ua.error": "ua.err",
    "ua.success": "ua.ok",
    "ua.warning": "ua.warn",
    "ua.hint": "ua.info",
    "ua.meta": "ua.muted",
    "ua.separator": "ua.faint",
    "ua.cursor": "ua.selection",
}


def build_styles(palette: Palette, *, depth: str = "truecolor") -> dict[str, str]:
    """Resolve the style table against a palette and colour depth."""

    colors = palette.colors_for_depth(depth)
    styles: dict[str, str] = {}
    for name, (template, token) in STYLE_TABLE.items():
        value = colors.get(token) or palette.ansi_color(token)
        styles[name] = template.format(**{token: value})
    for alias, target in STYLE_ALIASES.items():
        styles[alias] = styles[target]
    return styles


def rich_theme(palette: Palette | str | None = None, *, depth: str = "truecolor") -> Theme:
    """A :class:`rich.theme.Theme` for a palette (default: the active one)."""

    resolved = palette if isinstance(palette, Palette) else get_palette(palette or active_theme_key())
    return Theme(build_styles(resolved, depth=depth))


# --------------------------------------------------------------------------- #
# process-wide active theme
# --------------------------------------------------------------------------- #
class _ActiveTheme:
    """Holds the theme the whole process renders with.

    Consoles are built once per process, so the active theme is applied at CLI
    start (``Runtime.load``) and by ``ua theme use`` (which rebinds the consoles
    live).  Everything else reads styles by name and stays theme-agnostic.
    """

    def __init__(self) -> None:
        self.key: str = DEFAULT_THEME
        self.depth: str = "truecolor"
        self.glyphs: str = "unicode"
        self._theme: Theme | None = None
        self._palette: Palette | None = None

    @property
    def palette(self) -> Palette:
        if self._palette is None or self._palette.key != self.key:
            self._palette = get_palette(self.key)
        return self._palette

    @property
    def theme(self) -> Theme:
        if self._theme is None:
            self._theme = rich_theme(self.palette, depth=self.depth)
        return self._theme

    def set(self, key: str | None, *, depth: str | None = None, glyphs: str | None = None) -> Palette:
        palette = get_palette(key or DEFAULT_THEME)
        changed = palette.key != self.key
        self.key = palette.key
        self.depth = depth or self.depth
        self.glyphs = glyphs or self.glyphs
        if changed or depth is not None:
            self._theme = None
            self._palette = palette
        return palette

    def styles(self) -> dict[str, str]:
        return build_styles(self.palette, depth=self.depth)


_ACTIVE = _ActiveTheme()

#: Kept for compatibility with code and tests that import the constant directly.
#: It is rebuilt in place whenever the active theme changes, so a module that
#: captured it at import time still renders with the chosen palette.
UNKNOWN_AGENT_THEME = _ACTIVE.theme


def active_theme_key() -> str:
    return _ACTIVE.key


def active_palette() -> Palette:
    return _ACTIVE.palette


def active_glyph_set() -> str:
    return _ACTIVE.glyphs


def apply_theme(
    key: str | None = None,
    *,
    depth: str | None = None,
    glyphs: str | None = None,
    rebind_consoles: bool = True,
) -> Palette:
    """Install a palette as the process theme.

    ``rebind_consoles`` rewrites the theme of the already-built Rich consoles so
    ``ua theme use paper`` recolours the session immediately instead of on next
    start.
    """

    global UNKNOWN_AGENT_THEME
    resolved_depth = depth or detect_color_depth()
    palette = _ACTIVE.set(key, depth=resolved_depth, glyphs=glyphs)
    UNKNOWN_AGENT_THEME = _ACTIVE.theme
    if rebind_consoles:
        _rebind()
    return palette


def resolve_theme_key(choice: str | None, *, mode: str | None = None) -> str | None:
    """Turn an ``[appearance] theme`` value into a palette key.

    ``auto`` is the interesting case: it means "match my terminal", so the mode is
    pinned by ``[appearance] mode`` when the user set one and otherwise guessed from
    the environment (:func:`detect_preferred_mode`).  ``dark``/``light`` are aliases
    for the product's two reference palettes, and a named palette is used as-is.
    An unknown key is *not* swallowed here — :func:`get_palette` raises, and a
    config typo should say so rather than silently rendering the default theme.
    """

    raw = (choice or "").strip().lower()
    if raw in {"", "auto"}:
        pinned = (mode or "").strip().lower()
        return pinned if pinned in {"dark", "light"} else detect_preferred_mode()
    return raw or None


def apply_appearance(
    appearance: Any,
    *,
    rebind_consoles: bool = True,
    force_glyphs: str | None = None,
    force_depth: str | None = None,
) -> Palette:
    """Apply an ``[appearance]`` config block (duck-typed to avoid an import cycle).

    ``force_glyphs`` / ``force_depth`` are the CLI flags (``--ascii``,
    ``--no-color``): an explicit flag on the command line beats a config file,
    because it is the more recent and more specific statement of intent.

    Every field of :class:`~unknown_agent.config.models.AppearanceConfig` that
    changes *rendering* is honoured here — theme, mode, glyphs and color_depth.
    An earlier version read ``appearance.color``, a field that does not exist, so
    ``color_depth = "none"`` was silently ignored and a user who asked for no
    colour still got colour.
    """

    configured_depth = str(getattr(appearance, "color_depth", "auto") or "auto").lower()
    depth = force_depth or (detect_color_depth() if configured_depth == "auto" else configured_depth)
    glyphs = str(getattr(appearance, "glyphs", "unicode") or "unicode")
    if force_glyphs:
        glyphs = force_glyphs
    if depth == "none":
        # No colour means no box drawing either: the ascii set is the one that still
        # reads correctly in a log file or a dumb terminal.
        glyphs = "ascii"
    key = resolve_theme_key(
        str(getattr(appearance, "theme", None) or ""),
        mode=str(getattr(appearance, "mode", None) or "") or None,
    )
    forced = os.environ.get("UNKNOWN_AGENT_THEME")
    return apply_theme(
        forced or key,
        depth=depth,
        glyphs=glyphs,
        rebind_consoles=rebind_consoles,
    )


def _rebind() -> None:
    """Point the live consoles at the freshly built theme."""

    try:
        from unknown_agent.ui.console import output
    except Exception:  # pragma: no cover - console import is safe, but stay defensive
        return
    for console in (getattr(output, "_data", None), getattr(output, "_diag", None)):
        if isinstance(console, Console):
            console.push_theme(_ACTIVE.theme)


# --------------------------------------------------------------------------- #
# glyphs
# --------------------------------------------------------------------------- #
GLYPHS: dict[str, tuple[str, str]] = {
    name: (GLYPH_SETS["unicode"][name], GLYPH_SETS["ascii"][name]) for name in GLYPH_SETS["unicode"]
}


def glyph(name: str, *, unicode: bool = True) -> str:
    """A glyph from the active set, falling back to ASCII when asked/needed."""

    table = GLYPH_SETS.get(_ACTIVE.glyphs, GLYPH_SETS["unicode"])
    if not unicode or _ACTIVE.depth == "none":
        table = GLYPH_SETS["ascii"]
    if name in table:
        return table[name]
    unicode_form, ascii_form = GLYPHS.get(name, ("?", "?"))
    return unicode_form if unicode else ascii_form


def glyph_set(name: str) -> dict[str, str]:
    return dict(GLYPH_SETS.get(name, GLYPH_SETS["unicode"]))


# --------------------------------------------------------------------------- #
# previews and audits (used by `ua theme`)
# --------------------------------------------------------------------------- #
def swatch_row(palette: Palette) -> Table:
    """One row of the palette's colours, labelled — the core of a theme preview."""

    table = Table(box=None, show_header=False, pad_edge=False, collapse_padding=True)
    for token in TOKEN_ORDER:
        color = palette.color(token)
        cell = Text()
        cell.append("  ", style=f"on {color}")
        cell.append(f"\n{token}", style="ua.muted")
        cell.append(f"\n{color}", style="ua.faint")
        table.add_column(justify="center", width=11)
        table.add_row(cell)
    return table


def preview(palette: Palette | str | None = None) -> Panel:
    """Render a palette the way the product actually looks.

    A theme is judged by real UI, not by colour chips alone: this draws the
    banner, a plan step, a tool call, gate statuses and an error panel in the
    candidate palette so the choice is made on evidence.

    The result is a self-contained renderable: the candidate palette is applied
    to a private console, captured as ANSI and re-embedded as text.  That is what
    makes a live theme chooser possible — the preview shows the *candidate*
    colours while the surrounding chrome stays in the *active* theme, so a user
    comparing eight palettes is never looking at a lie.
    """

    resolved = palette if isinstance(palette, Palette) else get_palette(palette or active_theme_key())
    buffer = io.StringIO()
    probe = Console(
        file=buffer,
        width=96,
        force_terminal=True,
        color_system="truecolor",
        emoji=False,
        theme=rich_theme(resolved),
    )
    probe.print(preview_renderable(resolved))
    captured = Text.from_ansi(buffer.getvalue().rstrip("\n"))
    return Panel(
        captured,
        border_style="ua.panel.border",
        title=f"[ua.panel.title]{resolved.label}[/ua.panel.title] · {resolved.mode}",
        subtitle=f"[ua.muted]{resolved.key}[/ua.muted] · live preview[/ua.muted]",
        padding=(0, 1),
    )


def preview_renderable(palette: Palette | str | None = None) -> Panel:
    """A Rich-renderable preview that respects the *live* console (no capture hacks)."""

    resolved = palette if isinstance(palette, Palette) else get_palette(palette or active_theme_key())
    body = Group(
        Text.assemble(("Unknown Agent", "ua.brand"), ("  v2.0.0", "ua.muted"), ("    ", ""), (resolved.label, "ua.accent")),
        Text(resolved.tagline, style="ua.text.dim"),
        Text(""),
        Text.assemble(("● ", "ua.step"), ("plan", "ua.step"), ("  scaffold the checkout flow", "ua.text")),
        Text.assemble(("  ✦ ", "ua.think"), ("reasoning: high — three candidate architectures under review", "ua.think")),
        Text.assemble(("  → ", "ua.tool.name"), ("write_file", "ua.tool.name"), (" checkout/session.py", "ua.tool.arg")),
        Text.assemble(("  ✔ ", "ua.ok"), ("tests", "ua.ok"), ("   42 passed", "ua.muted")),
        Text.assemble(("  ✖ ", "ua.err"), ("lint", "ua.err"), ("    3 findings — self-healing", "ua.muted")),
        Text.assemble(("  ⚠ ", "ua.warn"), ("browser", "ua.warn"), (" console budget exceeded (1)", "ua.muted")),
    )
    return Panel(
        body,
        title=f"[ua.panel.title]{resolved.label}[/ua.panel.title] · {resolved.mode}",
        subtitle=f"[ua.muted]{resolved.key}[/ua.muted]",
        border_style="ua.panel.border",
        padding=(1, 2),
    )


def swatches(palette: Palette | str | None = None) -> Table:
    resolved = palette if isinstance(palette, Palette) else get_palette(palette or active_theme_key())
    table = Table(
        box=None,
        show_header=True,
        header_style="ua.muted",
        pad_edge=False,
        title=f"{resolved.label} · {resolved.mode}",
        title_style="ua.panel.title",
    )
    table.add_column("token")
    table.add_column("colour")
    table.add_column("hex")
    table.add_column("used for", style="ua.text.dim", max_width=52)
    for token in TOKEN_ORDER:
        color = resolved.color(token)
        table.add_row(
            Text(token, style="ua.muted"),
            Text("████", style=color),
            Text(color, style="ua.faint"),
            SEMANTIC_TOKENS.get(token, ""),
        )
    return table


def audit_contrast(palette: Palette | str | None = None, *, minimum: float = 4.5) -> list[dict[str, Any]]:
    """Measure text-token contrast against the palette's own surface.

    Returns one row per text token with the measured ratio and a pass flag, so
    ``ua theme check`` can be honest about a palette that looks nice but reads badly.
    """

    resolved = palette if isinstance(palette, Palette) else get_palette(palette or active_theme_key())
    surfaces = {
        "surface": resolved.color("surface"),
        "raised": resolved.color("surface_raised"),
        "sunken": resolved.color("surface_sunken"),
    }
    rows: list[dict[str, Any]] = []
    text_tokens = ("text", "text_dim", "text_muted", "text_faint", "brand", "accent", "ok", "warn", "err", "info")
    for token in text_tokens:
        fg = resolved.color(token)
        entry: dict[str, Any] = {"token": token, "hex": fg, "ratios": {}, "passes": {}}
        for name, bg in surfaces.items():
            try:
                ratio = contrast_ratio(fg, bg)
            except ValueError:
                continue
            entry["ratios"][name] = round(ratio, 2)
            entry["passes"][name] = ratio >= minimum
        entry["ok"] = bool(entry["ratios"].get("surface", 0) >= minimum)
        rows.append(entry)
    return rows


def catalog() -> list[dict[str, Any]]:
    """Palette metadata for ``--json`` consumers and the TUI chooser."""

    return [get_palette(key).to_dict() for key in PALETTE_ORDER]


def resolve_theme_choice(choice: str | None, *, prefer: str | None = None) -> str:
    """Map ``auto``/``dark``/``light``/explicit keys onto a concrete palette."""

    raw = (choice or "").strip().lower()
    if raw and raw not in {"auto", "dark", "light", "system"}:
        return get_palette(raw).key
    mode = raw if raw in {"dark", "light"} else (prefer or detect_preferred_mode())
    candidates = theme_names(mode=mode)
    return candidates[0] if candidates else DEFAULT_THEME


__all__ = [
    "DEFAULT_THEME",
    "GLYPHS",
    "GLYPH_SETS",
    "PALETTE_ORDER",
    "SEMANTIC_TOKENS",
    "STYLE_ALIASES",
    "STYLE_TABLE",
    "TOKEN_ORDER",
    "UNKNOWN_AGENT_THEME",
    "active_glyph_set",
    "active_palette",
    "active_theme_key",
    "apply_appearance",
    "resolve_theme_key",
    "apply_theme",
    "audit_contrast",
    "build_styles",
    "catalog",
    "detect_color_depth",
    "detect_preferred_mode",
    "get_palette",
    "glyph",
    "glyph_set",
    "preview",
    "preview_renderable",
    "resolve_theme_choice",
    "rich_theme",
    "swatches",
    "theme_names",
]
