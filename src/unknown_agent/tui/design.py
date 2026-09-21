"""The design system for the shell: tokens, glyphs and formatting rules.

Why this module exists
----------------------
The shell previously borrowed the CLI's ``midnight`` palette — cyan ``#22d3ee`` on
navy, violet ``#7c3aed`` borders, pink ``#f472b6`` accents. Three saturated hues
competing at full strength is the terminal equivalent of a neon sign: it reads as
"hacker toy", it fatigues the eye, and it makes genuinely important things (an error,
the focused control) impossible to distinguish from decoration, because *everything*
is already shouting.

This is a deliberate, self-contained replacement built on three rules:

1. **Neutrals carry the interface.** Structure — panels, borders, rules, labels — is
   drawn exclusively from a single cool-grey ramp. Nothing structural is coloured.
2. **Colour is information, never decoration.** A hue appears only when it means
   something: focus, or a semantic state (ok / warn / error). Because colour is rare,
   it is legible the instant it appears.
3. **One accent.** A restrained slate-blue. It marks focus and the active selection,
   and nothing else. There is no secondary accent and no gradient anywhere.

Hierarchy is then expressed the way print typography does it — with weight, spacing
and alignment rather than colour. Terminals give us bold, dim, italic and reverse;
that is a sufficient type scale if it is used consistently, which :data:`TYPE` enforces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Tokens",
    "DARK",
    "LIGHT",
    "tokens_for",
    "GLYPH",
    "TYPE",
    "rule",
    "kv",
    "meter",
    "truncate",
    "plural",
    "compact_number",
    "format_cost",
    "format_duration",
]


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Tokens:
    """Every colour the shell may use. Widgets must not hardcode hex anywhere else."""

    key: str
    mode: str

    # -- surfaces: four steps of depth, all neutral ------------------------ #
    bg: str              # app background, the deepest plane
    surface: str         # panels and the sidebar
    raised: str          # inputs, cards, the palette overlay
    overlay: str         # modal scrim

    # -- text: a four-step ramp, which is all a terminal needs ------------- #
    text: str            # primary content
    dim: str             # secondary content
    muted: str           # labels, metadata
    faint: str           # disabled, placeholders, rules

    # -- structure --------------------------------------------------------- #
    border: str          # resting borders — barely there by design
    border_strong: str   # focused borders

    # -- the single accent -------------------------------------------------- #
    accent: str
    accent_dim: str      # accent at rest / on fills

    # -- semantics: desaturated so they read as information, not alarm ----- #
    ok: str
    warn: str
    err: str
    info: str

    def color(self, token: str) -> str:
        """Look a token up by name, tolerating unknown names with a neutral."""

        return getattr(self, token, self.text)

    # Convenience styles, so the same intent is never spelled two ways.
    @property
    def label(self) -> str:
        return self.muted

    @property
    def value(self) -> str:
        return self.text

    @property
    def heading(self) -> str:
        return f"bold {self.dim}"


#: Dark theme. A cool neutral grey ramp (slate, gently blue-shifted so it does not
#: look muddy next to the accent) with a single restrained blue.
DARK = Tokens(
    key="quiet-dark",
    mode="dark",
    bg="#0e1013",
    surface="#14171c",
    raised="#191d23",
    overlay="#05070a",
    text="#e6e8ea",
    dim="#b6bbc2",
    muted="#848b95",
    faint="#575d66",
    border="#252a31",
    border_strong="#3a424c",
    accent="#7aa2d2",       # muted slate blue — present, never loud
    accent_dim="#43617f",
    ok="#7fb894",           # sage
    warn="#d4a76a",         # amber, desaturated
    err="#cf8a85",          # clay red, not fire-engine
    info="#8fa8bf",
)

#: Light theme, same structure. Warm-neutral paper rather than pure white, which is
#: harsh in a terminal and makes the greys look blue.
LIGHT = Tokens(
    key="quiet-light",
    mode="light",
    bg="#fbfbfa",
    surface="#f4f4f2",
    raised="#ffffff",
    overlay="#8a8a87",
    text="#1c1e21",
    dim="#42464c",
    muted="#6b7078",
    faint="#9aa0a8",
    border="#e2e2df",
    border_strong="#c6c7c4",
    accent="#3f6795",
    accent_dim="#a9bdd4",
    ok="#3f7d5a",
    warn="#8a6320",
    err="#a04f49",
    info="#4a6880",
)


def tokens_for(key: str | None = None) -> Tokens:
    """Resolve a theme key to tokens. Unknown keys fall back to dark, never crash."""

    if key and str(key).lower() in {"light", "quiet-light", "paper", "linen", "frost", "day"}:
        return LIGHT
    return DARK


# --------------------------------------------------------------------------- #
# glyphs
# --------------------------------------------------------------------------- #
#: Geometric, monochrome, single-width. No emoji: they break alignment across
#: terminals, render inconsistently, and read as toy-like in a professional tool.
GLYPH: dict[str, str] = {
    "ready": "●",
    "busy": "◐",
    "idle": "○",
    "warn": "▲",
    "error": "✕",
    "ok": "✓",
    "skip": "–",
    "dot": "·",
    "chevron": "›",
    "arrow": "→",
    "bullet": "▪",
    "bar_full": "━",
    "bar_empty": "─",
    "rule": "─",
    "vrule": "│",
    "prompt": "❯",
    "spin": "◜◝◞◟",
}

#: The type scale. Four steps, each with a fixed meaning — used everywhere so the
#: same kind of text always looks the same.
TYPE: dict[str, str] = {
    "title": "bold",
    "heading": "bold",
    "body": "",
    "label": "",
    "meta": "dim",
}


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def rule(width: int, tokens: Tokens) -> Any:
    """A hairline divider. Structure via a faint rule, not a heavy border."""

    from rich.text import Text

    return Text(GLYPH["rule"] * max(0, width), style=tokens.border)


def kv(label: str, value: str, tokens: Tokens, *, width: int = 9, value_style: str = "") -> Any:
    """A label/value row: muted label, aligned column, primary value.

    The fixed label column is what makes a stack of these read as a table instead of
    a ransom note, so every metric in the sidebar goes through this one function.
    """

    from rich.text import Text

    text = Text()
    text.append(f"{label:<{width}}", style=tokens.muted)
    text.append(value, style=value_style or tokens.text)
    return text


def meter(fraction: float, width: int, tokens: Tokens, *, tone: str = "") -> Any:
    """A horizontal meter.

    Uses a heavy/light rule pair rather than solid blocks: at this size blocks form a
    slab that dominates the panel, whereas rules read as a precise, quiet scale. It
    also avoids ``▰``/``▱``, which many terminal fonts render as slanted parallelograms.
    Colour escalates only when the value genuinely warrants attention.
    """

    from rich.text import Text

    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    if tone:
        colour = tokens.color(tone)
    else:
        colour = tokens.accent if fraction < 0.75 else (tokens.warn if fraction < 0.9 else tokens.err)
    text = Text()
    text.append(GLYPH["bar_full"] * filled, style=colour)
    text.append(GLYPH["bar_empty"] * (width - filled), style=tokens.faint)
    return text


def truncate(value: str, limit: int) -> str:
    """Shorten with a true ellipsis, never a hard cut mid-word-ish."""

    value = str(value)
    if limit <= 1 or len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def plural(count: int, singular: str, plural_form: str = "") -> str:
    return f"{count:,} {singular if count == 1 else (plural_form or singular + 's')}"


def compact_number(value: int) -> str:
    """``1234`` → ``1.2k``. Keeps metric rows narrow enough to stay aligned."""

    if value < 1000:
        return str(value)
    if value < 1_000_000:
        trimmed = f"{value / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{trimmed}k"
    trimmed = f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".")
    return f"{trimmed}M"


def format_cost(amount: float | None, *, estimated: bool = False, partial: bool = False) -> str:
    """Money, with its own uncertainty attached. Never shows a false precision."""

    if amount is None:
        return "—"
    prefix = "~" if estimated else ""
    suffix = "+" if partial else ""
    if amount == 0:
        return f"{prefix}$0.00{suffix}"
    if amount < 0.01:
        return f"{prefix}${amount:.4f}{suffix}"
    if amount < 1:
        return f"{prefix}${amount:.3f}{suffix}"
    return f"{prefix}${amount:,.2f}{suffix}"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
