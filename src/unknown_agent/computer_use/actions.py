"""The computer-use action vocabulary.

A *closed* set of typed actions.  The model cannot invent new verbs; anything
outside this vocabulary is rejected at parse time with the list of what exists.
That property is what makes the audit trail meaningful and the confirmation
policy enforceable - you cannot confirm what you cannot classify.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = ["ActionKind", "Action", "ActionResult", "Observation", "MUTATING_ACTIONS", "parse_action"]

ActionKind = Literal[
    "navigate",   # go to a URL
    "click",      # click at coordinates or on a described element
    "double_click",
    "type",       # type text into the focused element
    "key",        # press a key / chord (Enter, Tab, Ctrl+A)
    "scroll",     # scroll by an amount or to an element
    "hover",
    "wait",       # wait for ms or for a described condition
    "back",
    "screenshot", # observation only
    "read_dom",   # observation only: accessibility/text snapshot
]

#: Actions that can change application state - these hit the confirmation
#: policy when [computer_use] confirm_actions = "mutating".
MUTATING_ACTIONS: frozenset[str] = frozenset({"navigate", "click", "double_click", "type", "key", "back"})


@dataclass(slots=True)
class Action:
    kind: ActionKind
    #: Coordinates for pointer actions (CSS pixels in the viewport).
    x: int | None = None
    y: int | None = None
    #: Text for type/key, URL for navigate, selector/description for scroll-to.
    text: str = ""
    #: Scroll deltas.
    dx: int = 0
    dy: int = 0
    #: wait duration.
    ms: int = 0
    #: Optional human-readable intent ("click the Submit button") - journalled
    #: and shown in confirmations, so the user approves *intent*, not pixels.
    intent: str = ""

    @property
    def mutating(self) -> bool:
        return self.kind in MUTATING_ACTIONS

    def describe(self) -> str:
        core = self.intent or ""
        if self.kind in {"click", "double_click", "hover"}:
            where = f"({self.x}, {self.y})" if self.x is not None else "?"
            return f"{self.kind} at {where}" + (f" — {core}" if core else "")
        if self.kind == "type":
            preview = self.text if len(self.text) <= 40 else self.text[:37] + "..."
            return f"type {preview!r}" + (f" — {core}" if core else "")
        if self.kind == "key":
            return f"press {self.text}" + (f" — {core}" if core else "")
        if self.kind == "navigate":
            return f"navigate to {self.text}"
        if self.kind == "scroll":
            return f"scroll by ({self.dx}, {self.dy})"
        if self.kind == "wait":
            return f"wait {self.ms} ms"
        return self.kind

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        for key in ("x", "y", "text", "dx", "dy", "ms", "intent"):
            value = getattr(self, key)
            if value not in (None, "", 0):
                payload[key] = value
        return payload


_ALLOWED_KEYS = {"kind", "x", "y", "text", "dx", "dy", "ms", "intent"}
_VALID_KINDS = {
    "navigate", "click", "double_click", "type", "key", "scroll",
    "hover", "wait", "back", "screenshot", "read_dom",
}


def parse_action(data: dict[str, Any]) -> Action:
    """Parse an action dict from the model; raise ValueError with the vocabulary."""

    kind = str(data.get("kind", "")).strip().lower()
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown action kind {kind!r}; valid kinds: {', '.join(sorted(_VALID_KINDS))}")
    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown action fields: {', '.join(sorted(unknown))}")
    action = Action(kind=kind)  # type: ignore[arg-type]
    for key in ("x", "y"):
        if data.get(key) is not None:
            setattr(action, key, int(data[key]))
    for key in ("dx", "dy", "ms"):
        if data.get(key) is not None:
            setattr(action, key, int(data[key]))
    action.text = str(data.get("text", "") or "")
    action.intent = str(data.get("intent", "") or "")
    if kind in {"click", "double_click", "hover"} and (action.x is None or action.y is None):
        raise ValueError(f"{kind} needs x and y coordinates")
    if kind == "navigate" and not action.text:
        raise ValueError("navigate needs a URL in 'text'")
    if kind in {"type", "key"} and not action.text:
        raise ValueError(f"{kind} needs 'text'")
    return action


@dataclass(slots=True)
class Observation:
    """What the driver saw after an action (or on demand)."""

    url: str = ""
    title: str = ""
    screenshot_path: str = ""
    #: Compact text/accessibility snapshot of the visible page.
    dom_text: str = ""
    viewport: tuple[int, int] = (0, 0)
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "screenshot": self.screenshot_path,
            "dom_text_chars": len(self.dom_text),
            "viewport": list(self.viewport),
        }


@dataclass(slots=True)
class ActionResult:
    ok: bool
    action: Action
    observation: Observation | None = None
    error: str = ""
    denied: bool = False
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action.to_dict(),
            "observation": self.observation.to_dict() if self.observation else None,
            "error": self.error,
            "denied": self.denied,
            "duration_ms": round(self.duration_ms, 1),
        }
