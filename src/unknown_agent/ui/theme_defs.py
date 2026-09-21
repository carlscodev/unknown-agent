"""Palette data and semantic tokens — the substrate of the visual language.

This module is deliberately **dependency-free** (standard library only).  Three
consumers need the same colours and none of them may import each other:

* :mod:`unknown_agent.ui.theme`      → builds a ``rich.theme.Theme`` for the CLI,
* :mod:`unknown_agent.tui.themes`    → builds a ``textual.theme.Theme`` for the TUI,
* :mod:`unknown_agent.config.models` → validates ``[appearance] theme = "..."``.

Keeping the data here means a palette is defined exactly once and the CLI, the
TUI and the config schema can never drift apart.  ``rich`` is *not* imported at
module scope on purpose: config validation would then pay for Rich on every
startup, and ``ua --json`` would carry a rendering library it never uses.

Design rules for palettes
-------------------------
1. **Tokens, not colours.**  Code styles ``ua.brand``; a palette decides what
   ``ua.brand`` looks like.  Nothing in the product hardcodes a hex value.
2. **Both modes are first-class.**  A light theme is not an inverted dark theme:
   surface stacking, border contrast and accent saturation are tuned per mode
   because terminals render them differently (background bleed, bold-as-bright).
3. **ANSI-safe.**  Every palette also ships ``ansi`` fallbacks so a 16-colour
   terminal (``TERM=xterm``, Windows conhost) still gets a coherent hierarchy.
4. **Accessible.**  Text tokens clear WCAG AA (4.5:1) against their surface for
   the default palettes; ``ua theme check`` reports the measured ratios.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "Palette",
    "PALETTES",
    "PALETTE_ORDER",
    "DEFAULT_THEME",
    "LIGHT_THEMES",
    "DARK_THEMES",
    "TOKEN_ORDER",
    "SEMANTIC_TOKENS",
    "GLYPH_SETS",
    "theme_names",
    "get_palette",
    "has_palette",
    "describe_palette",
    "relative_luminance",
    "contrast_ratio",
    "detect_color_depth",
    "detect_preferred_mode",
]


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #
#: Every semantic token the product may style.  Order matters: it is the order
#: used by ``ua theme preview`` swatches so comparisons line up across palettes.
TOKEN_ORDER: tuple[str, ...] = (
    "brand",
    "brand_deep",
    "brand_soft",
    "accent",
    "accent_soft",
    "ok",
    "warn",
    "err",
    "info",
    "text",
    "text_dim",
    "text_muted",
    "text_faint",
    "surface",
    "surface_raised",
    "surface_sunken",
    "border",
    "border_dim",
    "selection",
    "diff_add",
    "diff_del",
    "diff_hunk",
    "code",
    "link",
)

#: Token → what it is *for*.  Shown by ``ua theme preview --tokens`` so a user
#: choosing a theme understands the vocabulary instead of guessing from swatches.
SEMANTIC_TOKENS: dict[str, str] = {
    "brand": "identity: banner, active step, primary actions",
    "brand_deep": "panel borders, emphasis behind the brand",
    "brand_soft": "tinted backgrounds for brand surfaces",
    "accent": "reasoning, thinking, agent voice",
    "accent_soft": "tinted background for reasoning traces",
    "ok": "passing gates, completed steps, success",
    "warn": "degradation, advisories, confirmations",
    "err": "failures, destructive actions, blocked gates",
    "info": "hints, links, secondary calls to action",
    "text": "primary body copy",
    "text_dim": "secondary body copy",
    "text_muted": "metadata: ids, timings, counts",
    "text_faint": "rules, separators, disabled affordances",
    "surface": "default terminal background reference",
    "surface_raised": "panels, cards, raised rows",
    "surface_sunken": "wells, input fields, code blocks",
    "border": "panel and table borders",
    "border_dim": "inner separators",
    "selection": "cursor / selected row highlight",
    "diff_add": "added lines",
    "diff_del": "removed lines",
    "diff_hunk": "hunk headers",
    "code": "inline code and literals",
    "link": "hyperlinks and file paths",
}


# --------------------------------------------------------------------------- #
# palette
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Palette:
    """One complete visual identity.

    ``colors`` maps semantic token → hex.  ``ansi`` maps the same tokens to the
    16-colour names Rich understands (``bright_cyan``, ``red`` …) and is used
    automatically when the terminal cannot do 256/truecolor.
    """

    key: str
    label: str
    mode: str  # "dark" | "light"
    tagline: str
    colors: Mapping[str, str]
    ansi: Mapping[str, str] = field(default_factory=dict)
    #: Extra hints for the TUI: which token paints the app background, etc.
    tui: Mapping[str, str] = field(default_factory=dict)
    #: Human credits, shown in ``ua theme show``.
    inspired_by: str = ""

    def __post_init__(self) -> None:
        missing = [token for token in TOKEN_ORDER if token not in self.colors]
        if missing:
            raise ValueError(f"palette {self.key!r} is missing tokens: {', '.join(missing)}")
        if self.mode not in {"dark", "light"}:
            raise ValueError(f"palette {self.key!r} has unknown mode {self.mode!r}")

    @property
    def is_dark(self) -> bool:
        return self.mode == "dark"

    def color(self, token: str) -> str:
        return self.colors[token]

    def ansi_color(self, token: str) -> str:
        """ANSI fallback for a token, degrading to a sensible default."""

        if token in self.ansi:
            return self.ansi[token]
        defaults = {
            "brand": "bright_cyan",
            "brand_deep": "magenta",
            "brand_soft": "cyan",
            "accent": "bright_magenta",
            "accent_soft": "magenta",
            "ok": "green",
            "warn": "yellow",
            "err": "red",
            "info": "cyan",
            "text": "white",
            "text_dim": "bright_black",
            "text_muted": "bright_black",
            "text_faint": "bright_black",
            "surface": "default",
            "surface_raised": "default",
            "surface_sunken": "default",
            "border": "magenta",
            "border_dim": "bright_black",
            "selection": "cyan",
            "diff_add": "green",
            "diff_del": "red",
            "diff_hunk": "bright_cyan",
            "code": "white",
            "link": "bright_blue",
        }
        return defaults.get(token, "default")

    def colors_for_depth(self, depth: str) -> Mapping[str, str]:
        """Resolve token → colour string for a terminal colour depth."""

        if depth in {"truecolor", "256", "standard"} and self.colors:
            return self.colors
        return {token: self.ansi_color(token) for token in TOKEN_ORDER}

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "mode": self.mode,
            "tagline": self.tagline,
            "inspired_by": self.inspired_by,
            "colors": dict(self.colors),
            "ansi": dict(self.ansi),
            "tui": dict(self.tui),
        }


def _p(
    key: str,
    label: str,
    mode: str,
    tagline: str,
    *,
    inspired_by: str = "",
    tui: Mapping[str, str] | None = None,
    **colors: str,
) -> Palette:
    ansi = {
        "brand": "bright_cyan",
        "brand_deep": "magenta",
        "brand_soft": "cyan",
        "accent": "bright_magenta",
        "accent_soft": "magenta",
        "ok": "green",
        "warn": "yellow",
        "err": "red",
        "info": "cyan",
        "text": "white",
        "text_dim": "bright_black",
        "text_muted": "bright_black",
        "text_faint": "bright_black",
        "border": "magenta",
        "border_dim": "bright_black",
        "selection": "cyan",
        "diff_add": "green",
        "diff_del": "red",
        "diff_hunk": "bright_cyan",
        "code": "white",
        "link": "bright_blue",
    }
    if mode == "light":
        ansi.update(
            {
                "text": "black",
                "text_dim": "bright_black",
                "code": "black",
                "brand": "cyan",
                "accent": "magenta",
                "border": "magenta",
                "selection": "bright_cyan",
            }
        )
    return Palette(
        key=key,
        label=label,
        mode=mode,
        tagline=tagline,
        inspired_by=inspired_by,
        colors=colors,
        ansi=ansi,
        tui=dict(tui or {}),
    )


# --------------------------------------------------------------------------- #
# the shipped palettes
# --------------------------------------------------------------------------- #
PALETTES: dict[str, Palette] = {}


def _register(palette: Palette) -> Palette:
    PALETTES[palette.key] = palette
    return palette


# -- dark ------------------------------------------------------------------ #
_register(
    _p(
        "midnight",
        "Midnight",
        "dark",
        "The default. Cyan-on-slate with a violet rule — calm, high contrast, no neon fatigue.",
        inspired_by="Unknown Agent 1.0 identity",
        tui={"background": "#0b1020", "panel": "#111a2e", "cursor": "#22d3ee"},
        brand="#22d3ee",
        brand_deep="#7c3aed",
        brand_soft="#164e63",
        accent="#f472b6",
        accent_soft="#4c1d95",
        ok="#34d399",
        warn="#fbbf24",
        err="#f87171",
        info="#38bdf8",
        text="#e2e8f0",
        text_dim="#cbd5e1",
        text_muted="#94a3b8",
        text_faint="#64748b",
        surface="#0b1020",
        surface_raised="#151b2e",
        surface_sunken="#070b16",
        border="#7c3aed",
        border_dim="#334155",
        selection="#164e63",
        diff_add="#4ade80",
        diff_del="#fb7185",
        diff_hunk="#22d3ee",
        code="#e2e8f0",
        link="#60a5fa",
    )
)

_register(
    _p(
        "abyss",
        "Abyss",
        "dark",
        "Near-black OLED surfaces with a single teal signal. For long nights and low glare.",
        inspired_by="deep-space terminals",
        tui={"background": "#05070a", "panel": "#0b1016", "cursor": "#2dd4bf"},
        brand="#2dd4bf",
        brand_deep="#0f766e",
        brand_soft="#11322f",
        accent="#a78bfa",
        accent_soft="#2e1f5e",
        ok="#4ade80",
        warn="#f5c451",
        err="#ff6b6b",
        info="#5eead4",
        text="#e6edf3",
        text_dim="#c3ced9",
        text_muted="#8b98a5",
        text_faint="#5b6672",
        surface="#05070a",
        surface_raised="#0b1016",
        surface_sunken="#020406",
        border="#0f766e",
        border_dim="#22303c",
        selection="#11322f",
        diff_add="#56d364",
        diff_del="#ff7b72",
        diff_hunk="#2dd4bf",
        code="#d8e2ea",
        link="#79c0ff",
    )
)

_register(
    _p(
        "ember",
        "Ember",
        "dark",
        "Warm charcoal with amber and coral. Easier on the eyes than cold blue darks.",
        inspired_by="incandescent workshops",
        tui={"background": "#16110e", "panel": "#1f1815", "cursor": "#fb923c"},
        brand="#fb923c",
        brand_deep="#b45309",
        brand_soft="#3f2412",
        accent="#f472b6",
        accent_soft="#4a2137",
        ok="#a3e635",
        warn="#fde047",
        err="#fb7185",
        info="#fdba74",
        text="#f5ebe0",
        text_dim="#e7d8c9",
        text_muted="#b3a08d",
        text_faint="#8a7a6a",
        surface="#16110e",
        surface_raised="#1f1815",
        surface_sunken="#0f0b09",
        border="#b45309",
        border_dim="#453729",
        selection="#3f2412",
        diff_add="#bef264",
        diff_del="#fda4af",
        diff_hunk="#fb923c",
        code="#f0e2d3",
        link="#fcd34d",
    )
)

_register(
    _p(
        "aurora",
        "Aurora",
        "dark",
        "Violet, mint and rose over deep indigo. The expressive choice for demos and screencasts.",
        inspired_by="polar light",
        tui={"background": "#0d0b1e", "panel": "#151233", "cursor": "#c084fc"},
        brand="#c084fc",
        brand_deep="#6d28d9",
        brand_soft="#2e1065",
        accent="#5eead4",
        accent_soft="#134e4a",
        ok="#86efac",
        warn="#fcd34d",
        err="#fda4af",
        info="#93c5fd",
        text="#ede9fe",
        text_dim="#ddd6fe",
        text_muted="#a5a3c7",
        text_faint="#7a78a0",
        surface="#0d0b1e",
        surface_raised="#151233",
        surface_sunken="#070614",
        border="#6d28d9",
        border_dim="#3b3563",
        selection="#2e1065",
        diff_add="#86efac",
        diff_del="#fda4af",
        diff_hunk="#c084fc",
        code="#e9d5ff",
        link="#a5b4fc",
    )
)

_register(
    _p(
        "nocturne",
        "Nocturne",
        "dark",
        "Low-saturation slate and steel blue. Maximum focus, minimum colour noise.",
        inspired_by="night-shift engineering",
        tui={"background": "#0e1116", "panel": "#151a21", "cursor": "#7dd3fc"},
        brand="#7dd3fc",
        brand_deep="#475569",
        brand_soft="#1e293b",
        accent="#c4b5fd",
        accent_soft="#312e54",
        ok="#86efac",
        warn="#fcd34d",
        err="#fca5a5",
        info="#93c5fd",
        text="#dbe2ea",
        text_dim="#c2ccd8",
        text_muted="#8e9aa8",
        text_faint="#6b7684",
        surface="#0e1116",
        surface_raised="#151a21",
        surface_sunken="#090b0f",
        border="#475569",
        border_dim="#2b3441",
        selection="#1e293b",
        diff_add="#86efac",
        diff_del="#fca5a5",
        diff_hunk="#7dd3fc",
        code="#cfdae6",
        link="#93c5fd",
    )
)

# -- light ----------------------------------------------------------------- #
_register(
    _p(
        "paper",
        "Paper",
        "light",
        "The reference light theme: ink on warm white, indigo signal, honest contrast.",
        inspired_by="print typography",
        tui={"background": "#f8fafc", "panel": "#ffffff", "cursor": "#0f766e"},
        brand="#0f766e",
        brand_deep="#4338ca",
        brand_soft="#ccfbf1",
        accent="#be185d",
        accent_soft="#fce7f3",
        ok="#15803d",
        warn="#b45309",
        err="#b91c1c",
        info="#0369a1",
        text="#0f172a",
        text_dim="#334155",
        text_muted="#64748b",
        text_faint="#94a3b8",
        surface="#f8fafc",
        surface_raised="#ffffff",
        surface_sunken="#eef2f7",
        border="#4338ca",
        border_dim="#cbd5e1",
        selection="#ccfbf1",
        diff_add="#166534",
        diff_del="#9f1239",
        diff_hunk="#0f766e",
        code="#111827",
        link="#1d4ed8",
    )
)

_register(
    _p(
        "linen",
        "Linen",
        "light",
        "Sepia-tinted paper with terracotta accents. Warm, bookish, easy in bright rooms.",
        inspired_by="sunlit desks",
        tui={"background": "#faf6ef", "panel": "#fffdf8", "cursor": "#c2410c"},
        brand="#c2410c",
        brand_deep="#8a5a2b",
        brand_soft="#ffe8d6",
        accent="#9d174d",
        accent_soft="#fce7f3",
        ok="#3f6212",
        warn="#a16207",
        err="#9f1239",
        info="#0e7490",
        text="#2b2118",
        text_dim="#4a3b2c",
        text_muted="#7a6a56",
        text_faint="#a2917a",
        surface="#faf6ef",
        surface_raised="#fffdf8",
        surface_sunken="#f2ebdd",
        border="#8a5a2b",
        border_dim="#ddd0ba",
        selection="#ffe8d6",
        diff_add="#3f6212",
        diff_del="#9f1239",
        diff_hunk="#c2410c",
        code="#33291e",
        link="#1d4ed8",
    )
)

_register(
    _p(
        "frost",
        "Frost",
        "light",
        "Cool white with glacier blue. Clinical, precise, excellent for dense tables.",
        inspired_by="arctic daylight",
        tui={"background": "#f6f9fc", "panel": "#ffffff", "cursor": "#1d4ed8"},
        brand="#1d4ed8",
        brand_deep="#0e7490",
        brand_soft="#dbeafe",
        accent="#7c3aed",
        accent_soft="#ede9fe",
        ok="#047857",
        warn="#b45309",
        err="#be123c",
        info="#0284c7",
        text="#0b1220",
        text_dim="#2c3a4f",
        text_muted="#5b6b80",
        text_faint="#8b9aad",
        surface="#f6f9fc",
        surface_raised="#ffffff",
        surface_sunken="#eaf1f8",
        border="#0e7490",
        border_dim="#c8d6e5",
        selection="#dbeafe",
        diff_add="#047857",
        diff_del="#be123c",
        diff_hunk="#1d4ed8",
        code="#101a2b",
        link="#2563eb",
    )
)

#: Presentation order: darks first (the product default), then lights.
PALETTE_ORDER: tuple[str, ...] = (
    "midnight",
    "abyss",
    "ember",
    "aurora",
    "nocturne",
    "paper",
    "linen",
    "frost",
)

DEFAULT_THEME = "midnight"
DARK_THEMES: tuple[str, ...] = tuple(k for k in PALETTE_ORDER if PALETTES[k].mode == "dark")
LIGHT_THEMES: tuple[str, ...] = tuple(k for k in PALETTE_ORDER if PALETTES[k].mode == "light")

#: Aliases a user might reasonably type.
_ALIASES: dict[str, str] = {
    "default": DEFAULT_THEME,
    "dark": "midnight",
    "light": "paper",
    "oled": "abyss",
    "solar": "ember",
    "warm": "ember",
    "cool": "nocturne",
    "violet": "aurora",
    "purple": "aurora",
    "sepia": "linen",
    "white": "paper",
    "ice": "frost",
    "blue": "frost",
}


def theme_names(*, mode: str | None = None) -> list[str]:
    """Palette keys, optionally filtered by ``dark``/``light``."""

    if mode is None:
        return list(PALETTE_ORDER)
    wanted = mode.strip().lower()
    return [key for key in PALETTE_ORDER if PALETTES[key].mode == wanted]


def has_palette(key: str) -> bool:
    try:
        get_palette(key)
    except KeyError:
        return False
    return True


def get_palette(key: str | None) -> Palette:
    """Resolve a palette key or alias, falling back to the default."""

    raw = (key or "").strip().lower()
    if not raw:
        return PALETTES[DEFAULT_THEME]
    raw = _ALIASES.get(raw, raw)
    if raw in PALETTES:
        return PALETTES[raw]
    raise KeyError(key or "")


def describe_palette(key: str) -> dict[str, Any]:
    palette = get_palette(key)
    return palette.to_dict()


# --------------------------------------------------------------------------- #
# glyph sets
# --------------------------------------------------------------------------- #
#: Three glyph sets so the product looks intentional on every terminal.
#: ``unicode`` is the default; ``ascii`` is forced by ``--ascii`` or ``TERM=dumb``;
#: ``nerd`` is opt-in for terminals with a patched font.
GLYPH_SETS: dict[str, dict[str, str]] = {
    "unicode": {
        "check": "✔",
        "cross": "✖",
        "warn": "⚠",
        "arrow": "→",
        "bullet": "•",
        "spark": "✦",
        "brain": "🧠",
        "wrench": "🔧",
        "globe": "🌐",
        "folder": "📁",
        "file": "📄",
        "clock": "⏱",
        "robot": "🤖",
        "shield": "🛡",
        "corner": "╰",
        "tee": "├",
        "rail": "│",
        "diamond": "◆",
        "circle": "●",
        "hollow": "○",
        "ellipsis": "…",
        "lock": "🔒",
        "key": "🔑",
        "palette": "🎨",
        "layers": "▤",
        "flow": "⇢",
        "star": "★",
    },
    "ascii": {
        "check": "+",
        "cross": "x",
        "warn": "!",
        "arrow": "->",
        "bullet": "*",
        "spark": "*",
        "brain": "M",
        "wrench": "T",
        "globe": "W",
        "folder": "D",
        "file": "f",
        "clock": "t",
        "robot": "A",
        "shield": "S",
        "corner": "`",
        "tee": "|",
        "rail": "|",
        "diamond": "+",
        "circle": "*",
        "hollow": "o",
        "ellipsis": "...",
        "lock": "#",
        "key": "k",
        "palette": "P",
        "layers": "=",
        "flow": "->",
        "star": "*",
    },
    "nerd": {
        "check": "✔",
        "cross": "✘",
        "warn": "⚠",
        "arrow": "➜",
        "bullet": "●",
        "spark": "✦",
        "brain": "󰠔",
        "wrench": "󰐕",
        "globe": "󰖟",
        "folder": "󰉋",
        "file": "󰈙",
        "clock": "󰅐",
        "robot": "󰚩",
        "shield": "󰦝",
        "corner": "╰",
        "tee": "├",
        "rail": "│",
        "diamond": "◆",
        "circle": "●",
        "hollow": "○",
        "ellipsis": "…",
        "lock": "󰍁",
        "key": "󰌆",
        "palette": "󰸉",
        "layers": "󰉍",
        "flow": "➜",
        "star": "★",
    },
}


# --------------------------------------------------------------------------- #
# colour maths (used by `ua theme check`)
# --------------------------------------------------------------------------- #
def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` colour."""

    raw = hex_color.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        raise ValueError(f"not a hex colour: {hex_color!r}")
    r, g, b = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio (1.0 – 21.0)."""

    a = relative_luminance(foreground)
    b = relative_luminance(background)
    lighter, darker = (a, b) if a > b else (b, a)
    return (lighter + 0.05) / (darker + 0.05)


# --------------------------------------------------------------------------- #
# environment detection
# --------------------------------------------------------------------------- #
def detect_color_depth(environ: Mapping[str, str] | None = None) -> str:
    """``truecolor`` | ``256`` | ``standard`` | ``none`` from the environment."""

    env = dict(environ if environ is not None else __import__("os").environ)
    if env.get("NO_COLOR"):
        return "none"
    if env.get("TERM") == "dumb":
        return "none"
    flags = env.get("COLORTERM", "").lower()
    term = env.get("TERM", "").lower()
    if "truecolor" in flags or "24bit" in flags:
        return "truecolor"
    # Terminals that are known truecolor even when COLORTERM is unset.
    if any(marker in term for marker in ("kitty", "wezterm", "alacritty", "ghostty", "foot")):
        return "truecolor"
    if env.get("WT_SESSION") or "terminator" in term or "konsole" in term:
        return "truecolor"
    if "256" in term or env.get("TERM_PROGRAM") in {"iTerm.app", "Hyper", "Apple_Terminal"}:
        return "256"
    if term in {"xterm", "screen", "linux", "vt100"} or term.startswith("xterm"):
        return "standard"
    return "256"


def detect_preferred_mode(environ: Mapping[str, str] | None = None) -> str:
    """Guess ``dark`` or ``light`` from the environment.

    Terminals do not report their background colour, so this is a best-effort
    heuristic used only to *preselect* the first onboarding question — the user
    always confirms.  Signals, in order of trust:

    1. ``UNKNOWN_AGENT_THEME_MODE`` (explicit override),
    2. ``COLORFGBG`` (``"15;0"`` → light-on-dark, ``"0;15"`` → dark-on-light),
    3. desktop dark-mode hints (``GTK_THEME``, ``KDE_COLOR_SCHEME``),
    4. ``dark`` — the product default.
    """

    env = dict(environ if environ is not None else __import__("os").environ)
    explicit = env.get("UNKNOWN_AGENT_THEME_MODE", "").strip().lower()
    if explicit in {"dark", "light"}:
        return explicit
    colorfgbg = env.get("COLORFGBG", "").strip()
    if colorfgbg and ";" in colorfgbg:
        parts = colorfgbg.split(";")
        try:
            bg = int(parts[-1])
        except ValueError:
            bg = -1
        if 0 <= bg <= 6:
            return "light"
        if bg >= 8:
            return "dark"
    gtk = env.get("GTK_THEME", "").lower()
    if gtk:
        return "light" if "light" in gtk else "dark"
    if "light" in env.get("KDE_COLOR_SCHEME", "").lower():
        return "light"
    return "dark"
