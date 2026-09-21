"""Streaming presentation for reasoning — rolling window, chunk buffering, narrow polish.

The old TUI dumped every reasoning delta as a raw line:

    hooks.on_message(delta) → Text(delta, style=dim) → RichLog.write()

On a phone (40 cols) that is catastrophic: a 200-token thought becomes
200 lines, or one long unwrapped spill that destroys the layout and the
scroll.

This module makes streaming feel intentional:

* **TUIChunkBuffer** — groups token fragments until a word boundary /
  punctuation, so the display never shows half-words or jittery flicker.
* **NarrowScreenOutputFormatter** — wraps/clamps text to the actual
  viewport width, preserving readability on 40-col Termius.
* **StreamingReasoningPresenter** — owns the live rolling window.
  Only the newest 10-20 words are shown prominently at the bottom;
  older words scroll upward into the RichLog history (or fade), giving
  a smooth upward motion instead of a chaotic spill.

The presenter is used by ``TUIAppShell.run_task``: during ``busy`` a
dedicated ``#reasoning-stream`` Static is visible and is updated via
``presenter.push(delta)``; on finish it collapses and the full, nicely
formatted reasoning (or tool summary) is appended once as a single
styled block.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

from rich.text import Text

from unknown_agent.tui.design import Tokens, truncate


# --------------------------------------------------------------------------- #
# Chunk buffering — group token fragments before rendering
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"\S+\s*")


class TUIChunkBuffer:
    """Accumulates raw model deltas until a word-worth is ready.

    Token streams arrive as ``\"He\"``, ``\"llo\"``, ``\" wh\"``, ...  Rendering
    each fragment separately flickers and shows half-words. This buffers
    until a whitespace or punctuation boundary, then flushes complete words.

    ``push`` returns ``\"\"`` while buffering, or the flushed words when a
    boundary is crossed. ``flush`` forces any remainder.
    """

    def __init__(self, *, min_words: int = 1) -> None:
        self._buf = ""
        self._min_words = max(1, min_words)

    def push(self, delta: str) -> str:
        if not delta:
            return ""
        self._buf += delta
        # Flush when we have at least min_words complete words and the buffer
        # ends at a word boundary (space/punct/newline).
        words = _WORD_RE.findall(self._buf)
        if len(words) >= self._min_words and self._buf and self._buf[-1] in " \t\n.,;:!?—–—\"'`)]}":
            out, self._buf = self._buf, ""
            return out
        # Also flush on newline — reasoning often uses line breaks as structure.
        if "\n" in self._buf and self._buf.strip():
            # Only if we have at least a few chars to avoid single-char lines
            if len(self._buf.strip()) > 12:
                out, self._buf = self._buf, ""
                return out
        return ""

    def flush(self) -> str:
        out, self._buf = self._buf, ""
        return out


# --------------------------------------------------------------------------- #
# Narrow formatter — ensure reasoning/code never overflows 40 cols
# --------------------------------------------------------------------------- #
def narrow_wrap(text: str, width: int, *, preserve_indent: bool = False) -> str:
    """Wrap ``text`` to ``width`` with awareness of narrow screens.

    On wide terminals we can let Rich handle wrapping; on narrow we clamp
    aggressively, break long tokens, and keep indent (for code) when asked.

    This is a lightweight helper — full markdown/code handling lives in
    ``CodeBlockRenderer``.
    """
    if width <= 0:
        width = 40
    # Clamp to a sane minimum — 20 cols is the absolute narrowest we support
    width = max(20, width)
    lines: list[str] = []
    for raw in text.splitlines() or [""]:
        if not raw.strip():
            lines.append("")
            continue
        # Preserve leading indent for code if requested
        indent = ""
        if preserve_indent:
            m = re.match(r"^(\s*)", raw)
            if m:
                indent = m.group(1)
                # Cap indent on narrow — 8 spaces max, else it eats the line
                if len(indent) > 8 and width < 60:
                    indent = indent[:8]
        content = raw.strip() if not preserve_indent else raw.lstrip()
        # Simple greedy wrap
        while len(indent) + len(content) > width:
            # Prefer to break at space
            space = content.rfind(" ", 0, width - len(indent))
            if space == -1:
                # Long token (e.g., a URL or minified JS) — hard break
                cut = width - len(indent) - 1
                lines.append(indent + content[:cut] + "…")
                content = content[cut:]
            else:
                lines.append(indent + content[:space])
                content = content[space + 1 :].lstrip()
            # Subsequent lines keep indent but dimmed visually by the renderer
        lines.append(indent + content)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# StreamingReasoningPresenter — the rolling 10-20 word window
# --------------------------------------------------------------------------- #
class StreamingReasoningPresenter:
    """Live reasoning presenter with a rolling window.

    Usage::

        presenter = StreamingReasoningPresenter(tokens, width=40)
        presenter.start("thinking")          # shows "thinking  ·  …"
        for delta in stream:
            chunk = buffer.push(delta)       # group until word boundary
            if chunk:
                presenter.push(chunk)        # appends words, updates window
        presenter.finish(full_text)          # collapses, returns final block

    Display rules:

    * Only the newest ``window_words`` (10-20) are shown prominently at the
      bottom, with full opacity and accent.
    * Older words scroll upward: they are no longer in the live window but
      have been appended to the upstream ``RichLog`` as faded history, so
      the user can still scroll up. The live widget itself never grows
      beyond ``max_lines`` (3-4 lines on narrow, 2 on wide).
    * On narrow we clamp to 2 lines; on wide we allow 3. Animation is
      implicit: each ``push`` replaces the Static's content, and Textual's
      offset/opacity transition does the rest.
    """

    def __init__(
        self,
        tokens: Tokens,
        *,
        width: int = 60,
        window_words: int = 16,
        max_lines: int = 3,
    ) -> None:
        self.tokens = tokens
        self.width = max(20, int(width or 60))
        self.window_words = max(8, min(24, int(window_words or 16)))
        self.max_lines = max(2, min(5, int(max_lines or 3)))
        self._words: deque[str] = deque()
        self._history: list[str] = []  # faded-up words (for final render)
        self._state = "thinking"
        self._started_at = time.time()
        self._updates = 0

    @property
    def state(self) -> str:
        return self._state

    def set_width(self, width: int) -> None:
        self.width = max(20, int(width or 40))
        # max_lines adapts to width — narrow gets fewer lines
        if self.width < 60:
            self.max_lines = 2
        elif self.width < 80:
            self.max_lines = 3
        else:
            self.max_lines = 4

    def start(self, state: str = "thinking") -> Text:
        self._state = state
        self._started_at = time.time()
        self._words.clear()
        self._history.clear()
        self._updates = 0
        return self._render_window()

    def push(self, chunk: str) -> Text | None:
        # Empty chunk is used by resize/reflow to request a re-render at new width
        if not chunk:
            return self._render_window()
        if not chunk.strip():
            return None
        # Split into words, preserving punctuation attached to each
        new_words = chunk.strip().split()
        if not new_words:
            return None
        for w in new_words:
            self._words.append(w)
        self._updates += 1
        # Keep only the rolling window; older words become history
        while len(self._words) > self.window_words:
            self._history.append(self._words.popleft())
            # Cap history so finish() is not huge (500 words max)
            if len(self._history) > 500:
                self._history.pop(0)
        # Throttle renders: only re-render every 2-3 words on fast streams
        # so the TUI doesn't flicker. We still return a Text every push()
        # but the caller can decide to sample.
        return self._render_window()

    def finish(self, full_text: str = "") -> Text:
        # Use full_text if provided (more complete than our window), else history+window
        text = (full_text or " ".join(list(self._history) + list(self._words))).strip()
        if not text:
            return Text("")
        # Final block is a single styled, wrapped paragraph — not a raw spill
        width = self.width
        wrapped = narrow_wrap(text, width - 4)
        body = Text()
        body.append("  ", style=self.tokens.faint)
        # Fade the full reasoning — it's history now, not live
        body.append(wrapped, style=self.tokens.muted)
        return body

    def _render_window(self) -> Text:
        tokens = self.tokens
        text = Text()

        # Header line: state + elapsed + rolling count — subtle, not shouty
        elapsed = time.time() - self._started_at
        # On narrow we shorten "thinking" → "…" to save cols
        state_label = self._state
        if self.width < 60 and len(state_label) > 8:
            state_label = state_label[:6] + "…"

        # Top rule on narrow helps separate reasoning from conversation
        if self.width < 80:
            text.append("─" * min(self.width - 4, 40), style=tokens.border)
            text.append("\n")

        text.append(f" {state_label}", style=f"bold {tokens.accent}")
        text.append(f"  {elapsed:.1f}s", style=tokens.faint)
        if self._words:
            text.append(f"  {len(self._words)}w window", style=tokens.muted)
        text.append("\n")

        # Rolling window — the newest words, wrapped to max_lines
        if self._words:
            window_text = " ".join(self._words)
            # Wrap to width - 2 (for padding) and clamp to max_lines
            wrapped_lines = narrow_wrap(window_text, self.width - 2).splitlines()
            if len(wrapped_lines) > self.max_lines:
                # Keep the newest lines (bottom), and hint that older scrolled up
                wrapped_lines = wrapped_lines[-self.max_lines :]
                # Subtle hint that history is above
                text.append("  ↑ older reasoning scrolled up\n", style=tokens.faint)
            for line in wrapped_lines:
                text.append("  ", style=tokens.faint)
                text.append(line, style=tokens.text)
                text.append("\n")
        else:
            # Idle shimmer — three dots that feel alive but not noisy
            dots = "·  ·  ·"
            if self._updates % 2 == 0:
                dots = "·  ·  ·"
            text.append(f"  {dots}", style=tokens.muted)
            text.append("\n")

        # On wide we also show a thin faded history preview (one line)
        if self.width >= 80 and self._history:
            preview = " ".join(self._history[-12:])  # last 12 words of history
            if preview:
                text.append("  ", style=tokens.faint)
                text.append(truncate(preview, self.width - 6), style=tokens.faint)
                text.append("\n")

        # Bottom spacing
        if self.width < 60:
            text.append("─" * min(self.width - 4, 40), style=tokens.border)

        return text

    def working_states(self) -> dict[str, str]:
        """Human labels for the structured working display."""
        return {
            "thinking": "thinking",
            "planning": "planning",
            "reading": "reading files",
            "editing": "editing files",
            "generating": "generating output",
            "tools": "calling tools",
            "verifying": "verifying",
        }


__all__ = [
    "TUIChunkBuffer",
    "StreamingReasoningPresenter",
    "narrow_wrap",
]
