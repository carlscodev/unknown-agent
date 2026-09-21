"""Textual themes generated from the same palettes the CLI uses.

One palette, two renderers.  :mod:`unknown_agent.ui.theme_defs` owns the colours;
this module maps them onto Textual's theme variables so the TUI and the CLI are
visibly the same product.  ``ua theme use paper`` recolours both.

The mapping is not arbitrary — Textual derives a large variable set
(``$primary-darken-1``, ``$surface-lighten-2``, ``$panel``, ``$boost`` …) from a
small number of anchors, so choosing the anchors well is what makes a generated
theme look designed rather than translated:

================  ==========================================================
``primary``       brand           — focus, selection, primary buttons
``secondary``     brand_deep      — panel borders, secondary emphasis
``accent``        accent          — the agent's voice, reasoning traces
``success``       ok              ``warning`` warn    ``error`` err
``foreground``    text            ``background`` surface
``surface``       surface_raised  ``panel`` surface_raised (cards, dialogs)
``boost``         brand_soft      — the tint behind a highlighted row
================  ==========================================================

Extra variables the product needs (muted text, faint rules, diff colours) are
supplied through ``variables`` so CSS can reference ``$ua-muted`` directly.
"""

from __future__ import annotations

from typing import Any

from textual.theme import Theme

from unknown_agent.ui.theme_defs import PALETTE_ORDER, Palette, get_palette

__all__ = ["to_textual_theme", "register_all", "theme_name", "available_theme_names"]

#: Prefix so our themes can never collide with Textual's built-ins.
PREFIX = "ua-"


def theme_name(key: str) -> str:
    """The Textual theme name for a palette key (``midnight`` → ``ua-midnight``)."""

    raw = (key or "").strip().lower()
    return raw if raw.startswith(PREFIX) else f"{PREFIX}{raw}"


def available_theme_names() -> list[str]:
    return [theme_name(key) for key in PALETTE_ORDER]


def to_textual_theme(palette: Palette | str | None = None) -> Theme:
    """Build a Textual :class:`Theme` from a palette."""

    resolved = palette if isinstance(palette, Palette) else get_palette(palette)
    colors = resolved.colors
    tui = dict(resolved.tui or {})
    background = tui.get("background") or colors["surface"]
    panel = tui.get("panel") or colors["surface_raised"]
    variables = {
        # product-specific tokens, available to CSS as $ua-*
        "ua-brand": colors["brand"],
        "ua-brand-soft": colors["brand_soft"],
        "ua-accent-soft": colors["accent_soft"],
        "ua-text-dim": colors["text_dim"],
        "ua-muted": colors["text_muted"],
        "ua-faint": colors["text_faint"],
        "ua-sunken": colors["surface_sunken"],
        "ua-border-dim": colors["border_dim"],
        "ua-selection": colors["selection"],
        "ua-info": colors["info"],
        "ua-diff-add": colors["diff_add"],
        "ua-diff-del": colors["diff_del"],
        "ua-code": colors["code"],
        # Textual derives these from the anchors, but the dark/light balance of a
        # terminal is not the same as a browser's: pin the ones that matter.
        "block-cursor-foreground": background,
        "input-selection-background": colors["selection"],
        "link": colors["link"],
        "scrollbar": colors["border_dim"],
        "scrollbar-hover": colors["brand"],
        "footer-key-foreground": colors["brand"],
        "block-hover-background": colors["brand_soft"],
        "markdown-code-background": colors["surface_sunken"],
    }
    return Theme(
        name=theme_name(resolved.key),
        primary=colors["brand"],
        secondary=colors["brand_deep"],
        warning=colors["warn"],
        error=colors["err"],
        success=colors["ok"],
        accent=tui.get("cursor") or colors["accent"],
        foreground=colors["text"],
        background=background,
        surface=colors["surface_raised"],
        panel=panel,
        boost=colors["brand_soft"],
        dark=resolved.is_dark,
        luminosity_spread=0.18 if resolved.is_dark else 0.12,
        text_alpha=0.96,
        variables=variables,
    )


def register_all(app: Any) -> list[str]:
    """Register every palette with a Textual app; returns the theme names."""

    names: list[str] = []
    for key in PALETTE_ORDER:
        theme = to_textual_theme(key)
        app.register_theme(theme)
        names.append(theme.name)
    return names


def apply_to_app(app: Any, palette_key: str | None) -> str:
    """Register the palettes and switch the app to the requested one.

    Falls back to the default palette when the requested key is unknown, because a
    typo in a config file must never make the TUI unstartable.
    """

    register_all(app)
    try:
        resolved = get_palette(palette_key)
    except KeyError:
        resolved = get_palette(None)
    name = theme_name(resolved.key)
    app.theme = name
    return name
