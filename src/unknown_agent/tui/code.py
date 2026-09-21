"""Code rendering for the TUI — styled, indented, narrow-safe.

The old ``TUIAppShell._format_result`` did::

    body.append(str(output), style=text)

That's fine for a one-line answer. For a 300-line HTML/CSS page it
produces broken wrapped garbage: indentation is lost (Rich collapses
it), line structure is lost (no code fences), the block overflows the
40-col viewport, and the conversation is flooded with raw source that
should have been a file.

This module fixes that without touching the agent's tool layer:

* **CodeBlockRenderer** — detects markdown fences (`````lang````), preserves
  indentation, renders each block inside a bordered, scroll-safe container,
  keeps it horizontally manageable on narrow, and shows only a preview for
  huge blocks (``> preview_lines``) with a note that the full file was
  written to disk.
* **Inline code** — `` `snippet` `` is rendered with accent_dim, not as a block.
* **File-write hint** — when a code block is too large, the renderer does
  NOT stream it raw. It returns a short preview + ``“full file written to
  <path>”`` so the TUI stays readable. The caller (``shell_app`` or the
  artifact router) decides whether that path was already written or should
  be inferred.

Narrow rules:

* Code is *never* reflowed like prose. It is kept verbatim, truncated
  with an ellipsis when a line exceeds ``width - 6`` (room for border +
  gutter), and wrapped only when ``wrap_code=True`` is explicitly asked.
* On ``phone <60`` we hard-cap to ``width - 4`` and reduce indent from
  4 → 2 so the block doesn't become a sliver.
* Preview thresholds: ``> 40 lines`` or ``> 2500 chars`` on wide,
  ``> 20 lines`` or ``> 1200 chars`` on narrow → preview first N lines.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

from rich.console import Group
from rich.text import Text

from unknown_agent.tui.design import GLYPH, Tokens, truncate

# ```lang?  fence — language tag optional, may be followed by filename
_FENCE_RE = re.compile(r"^\s*```\s*([A-Za-z0-9_+#.\-]+)?\s*(.*)$")
# Inline `code`
_INLINE_RE = re.compile(r"`([^`\n]+)`")


@dataclass(slots=True)
class RenderedCode:
    """Result of rendering one code-containing message."""

    had_code: bool
    renderable: object  # Rich renderable (Group / Text)
    was_truncated: bool
    preview_lines: int
    total_lines: int


def _detect_fences(text: str) -> list[tuple[int, int, str, str]]:
    """Find ````` blocks: (start_line, end_line, lang, content)."""
    lines = text.splitlines()
    blocks: list[tuple[int, int, str, str]] = []
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if m:
            lang = (m.group(1) or "").strip().lower()
            start = i
            j = i + 1
            while j < len(lines) and not _FENCE_RE.match(lines[j]):
                j += 1
            if j < len(lines):
                # Found closing fence
                content = "\n".join(lines[start + 1 : j])
                blocks.append((start, j, lang, content))
                i = j + 1
                continue
        i += 1
    return blocks


def _style_for_lang(lang: str, tokens: Tokens) -> str:
    # One accent for code — not a rainbow. Language only changes the label,
    # not the hue, to keep the palette quiet.
    return tokens.accent_dim if lang else tokens.dim


def _render_code_block(
    content: str,
    lang: str,
    tokens: Tokens,
    width: int,
    *,
    preview_lines: int,
    total_lines: int,
) -> object:
    """Render a single fenced block as a bordered, narrow-safe Group."""
    # Decide preview vs full
    was_truncated = total_lines > preview_lines
    shown = content.splitlines()[:preview_lines] if was_truncated else content.splitlines()
    # Narrow adjustments
    is_phone = width < 60
    gutter = 2 if is_phone else 4
    max_content = max(20, width - gutter - 2)  # -2 for border vibes
    # Build block
    block = Text()
    # Top label: language + line count, subtle
    label = lang or "code"
    header = Text()
    header.append(f" {label}", style=f"bold {tokens.muted}")
    if was_truncated:
        header.append(f"  ·  {total_lines} lines, showing {preview_lines}", style=tokens.faint)
    else:
        header.append(f"  ·  {total_lines} lines", style=tokens.faint)
    # Top rule
    rule = "─" * min(max(12, len(label) + 8), max_content)
    lines: list[Text] = [header, Text(rule, style=tokens.border)]
    # Code lines — preserve indent, truncate with ellipsis on overflow
    for raw in shown:
        # Preserve indent but cap on phone
        if is_phone and raw.startswith("    "):
            raw = raw.replace("    ", "  ", 1)
        # Truncate, don't wrap, so the shape stays code-like
        if len(raw) > max_content:
            raw = raw[: max_content - 1] + "…"
        line = Text()
        line.append(" ", style=tokens.faint)
        # Gutter bar — quiet
        line.append("│ ", style=tokens.border)
        line.append(raw, style=tokens.text)
        lines.append(line)
    if was_truncated:
        lines.append(Text(rule, style=tokens.border))
        hint = Text()
        hint.append("  … ", style=tokens.faint)
        hint.append(f"full block is {total_lines} lines — preview above", style=tokens.muted)
        lines.append(hint)
    else:
        lines.append(Text(rule, style=tokens.border))
    return Group(*lines)


def render_message(
    text: str,
    tokens: Tokens,
    width: int,
    *,
    file_hint: str | None = None,
    wrap_code: bool = False,
) -> RenderedCode:
    """Render a message that may contain markdown code fences.

    * If no fences → render as prose with inline `code` highlighted and
      wrapped to ``width`` via ``Text`` (Rich handles it).
    * If fences → each fence becomes a bordered block via
      ``_render_code_block``; prose around it stays as normal Text.
    * Huge blocks are previewed; we return ``was_truncated`` so the caller
      can add a “written to <file>” note or actually write the file.

    ``width`` is the viewport width (from ``self.size.width`` or
    ``self.console.width``). ``file_hint`` is the path the full file was
    (or will be) written to — when given, the truncated hint says
    ``“full file written to <path>”`` instead of the generic preview line.
    """
    text = text or ""
    width = max(30, int(width or 80))
    is_narrow = width < 80
    # Thresholds adapt to width — narrow is less tolerant
    preview_lines = 18 if is_narrow else 36
    max_chars_preview = 1200 if is_narrow else 2500

    fences = _detect_fences(text)
    if not fences:
        # Plain prose — handle inline `code`
        # Split on inline ticks and style them
        parts = _INLINE_RE.split(text)
        body = Text()
        for idx, part in enumerate(parts):
            if idx % 2 == 1:
                # Inside backticks
                body.append(part, style=f"bold {tokens.accent_dim}")
            else:
                # Wrap prose — let Rich do soft wrap but clamp long tokens
                # We do a light pre-wrap for narrow so no token overflows
                if is_narrow and len(part) > width:
                    # Simple word wrap for the prose part
                    wrapped = textwrap.fill(part, width=width, break_long_words=True, break_on_hyphens=True)
                    body.append(wrapped, style=tokens.text)
                else:
                    body.append(part, style=tokens.text)
        return RenderedCode(had_code=False, renderable=body, was_truncated=False, preview_lines=0, total_lines=0)

    # Mixed: interleave prose + code blocks
    lines = text.splitlines()
    out: list[object] = []
    last = 0
    had_code = False
    any_truncated = False
    for start, end, lang, content in fences:
        # Prose before this block
        if start > last:
            prose = "\n".join(lines[last:start]).strip()
            if prose:
                # Render prose chunk as inline-aware Text
                r = render_message(prose, tokens, width)
                out.append(r.renderable)
        # Code block
        total = len(content.splitlines()) or 1
        # Also consider char threshold — a single huge line still wants preview
        if len(content) > max_chars_preview:
            # Force preview to ~preview_lines worth of chars
            shown_chars = "\n".join(content.splitlines()[:preview_lines])
            if len(shown_chars) < len(content):
                total = max(total, preview_lines + 1)
        block = _render_code_block(content, lang, tokens, width, preview_lines=preview_lines, total_lines=total)
        out.append(block)
        if isinstance(block, Group):
            # Check if block was truncated by inspecting its renderables (last hint)
            # Simpler: compare total vs preview
            if total > preview_lines or len(content) > max_chars_preview:
                any_truncated = True
        if file_hint and (total > preview_lines or len(content) > max_chars_preview):
            hint = Text()
            hint.append("  ", style=tokens.faint)
            hint.append("full file written to ", style=tokens.muted)
            hint.append(file_hint, style=f"bold {tokens.accent_dim}")
            out.append(hint)
        had_code = True
        last = end + 1
    # Trailing prose
    if last < len(lines):
        tail = "\n".join(lines[last:]).strip()
        if tail:
            r = render_message(tail, tokens, width)
            out.append(r.renderable)

    if not out:
        return RenderedCode(had_code=False, renderable=Text(""), was_truncated=False, preview_lines=0, total_lines=0)
    if len(out) == 1:
        return RenderedCode(had_code=had_code, renderable=out[0], was_truncated=any_truncated, preview_lines=preview_lines, total_lines=sum(len(c.splitlines()) for _, _, _, c in fences))
    return RenderedCode(had_code=True, renderable=Group(*out), was_truncated=any_truncated, preview_lines=preview_lines, total_lines=sum(len(c.splitlines()) for _, _, _, c in fences))


def render_code_only(code: str, lang: str, tokens: Tokens, width: int) -> object:
    """Convenience when you already know it's pure code (no prose)."""
    return render_message(f"```{lang}\n{code}\n```", tokens, width).renderable


__all__ = ["RenderedCode", "render_message", "render_code_only"]
