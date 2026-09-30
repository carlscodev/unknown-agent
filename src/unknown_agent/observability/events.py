"""Structured event stream: the single source of truth for observability.

Every meaningful thing the agent does emits an :class:`Event`.  Consumers:

* the CLI renderer (live streaming to the terminal),
* the run journal (``.unknown/runs/<id>/events.jsonl``) for replay and audit,
* ``ua runs show --events`` for post-mortems,
* future exporters (OpenTelemetry, dashboards) without touching the agent core.

Events are additive and versioned: never repurpose a field, add a new one.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from unknown_agent.config.secrets import redact_text

EventType = Literal[
    "run.start",
    "run.end",
    "run.blocked",
    "plan.created",
    "plan.revised",
    "step.start",
    "step.end",
    "thought",
    "message",
    "tool.requested",
    "tool.started",
    "tool.finished",
    "tool.denied",
    "tool.failed",
    "observation",
    "memory.recall",
    "memory.write",
    "context.built",
    "provider.request",
    "provider.response",
    "gate.started",
    "gate.finished",
    "gate.failed",
    "browser.session",
    "browser.action",
    "browser.finding",
    "artifact",
    "budget.warning",
    "error",
    "log",
    "user.input",
    "interrupt",
]


@dataclass(slots=True)
class Event:
    """One immutable observation about the run."""

    type: EventType
    run_id: str = ""
    step: int = 0
    ts: float = field(default_factory=time.time)
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    level: str = "info"

    def to_json(self) -> str:
        payload = asdict(self)
        payload["message"] = redact_text(self.message)
        payload["data"] = _redact_data(self.data)
        return json.dumps(payload, ensure_ascii=False, default=str)

    def summary(self) -> str:
        return self.message or self.type


def _redact_data(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            out[key] = redact_text(value)
        elif isinstance(value, dict):
            out[key] = _redact_data(value)
        elif isinstance(value, list):
            out[key] = [redact_text(item) if isinstance(item, str) else item for item in value]
        else:
            out[key] = value
    return out


EventSink = Callable[[Event], None]


class EventBus:
    """Fan-out dispatcher with optional JSONL persistence."""

    def __init__(
        self,
        *,
        journal: Path | None = None,
        sinks: Iterable[EventSink] | None = None,
        hook_bus: Any = None,
    ) -> None:
        self._sinks: list[EventSink] = list(sinks or [])
        self._journal = journal
        self._hook_bus = hook_bus
        self._history: list[Event] = []
        self._keep_history = journal is None
        if journal is not None:
            journal.parent.mkdir(parents=True, exist_ok=True)

    def attach_journal(self, journal: Path) -> None:
        """Persist events to ``journal`` (JSONL) from now on.

        The runner knows its run directory only after setup, so the journal is
        attached then - a run without a journal would be a run nobody can audit.
        """

        journal.parent.mkdir(parents=True, exist_ok=True)
        self._journal = journal
        self._keep_history = False
        if journal.exists() and journal.stat().st_size:
            return
        journal.touch()

    @property
    def journal(self) -> Path | None:
        return self._journal

    def attach_hook_bus(self, hook_bus: Any) -> None:
        """Route future events to a durable async hook bus without blocking emitters."""

        self._hook_bus = hook_bus

    def subscribe(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def unsubscribe(self, sink: EventSink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def emit(self, event: Event) -> Event:
        if self._journal is not None:
            try:
                with self._journal.open("a", encoding="utf-8") as handle:
                    handle.write(event.to_json() + "\n")
            except OSError:  # pragma: no cover - journal not writable
                pass
        else:
            self._history.append(event)
        if self._hook_bus is not None:
            with contextlib.suppress(Exception):
                self._hook_bus.publish(event)
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:  # pragma: no cover - a broken sink must not kill the run
                continue
        return event

    def history(self) -> list[Event]:
        return list(self._history)

    @staticmethod
    def read_journal(path: Path) -> list[Event]:
        """Load a persisted journal back into :class:`Event` objects."""

        events: list[Event] = []
        if not path.is_file():
            return events
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(
                Event(
                    type=payload.get("type", "log"),
                    run_id=payload.get("run_id", ""),
                    step=int(payload.get("step", 0)),
                    ts=float(payload.get("ts", time.time())),
                    message=payload.get("message", ""),
                    data=payload.get("data", {}),
                    duration_ms=payload.get("duration_ms"),
                    level=payload.get("level", "info"),
                )
            )
        return events
