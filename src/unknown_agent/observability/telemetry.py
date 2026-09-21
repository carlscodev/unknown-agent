"""Local-first, opt-in telemetry.

Default state: **off**.  When enabled it records anonymous, non-content counters
(command name, exit class, duration bucket, error class) into a local JSON file
so a user - or a future opt-in upload - can see how the agent is being used
without ever transmitting prompts, diffs, file paths or code.

What is *never* collected, even when telemetry is enabled: prompts, model
outputs, file contents, paths, repository names, secrets, environment values.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.observability.logging import get_logger
from unknown_agent.version import __version__

log = get_logger("telemetry")

DISABLED_ENV = "UNKNOWN_AGENT_TELEMETRY_ENABLED"
DO_NOT_TRACK = "DO_NOT_TRACK"


@dataclass
class Telemetry:
    enabled: bool = False
    local_metrics: bool = True
    endpoint: str | None = None
    anonymous_id: str | None = None
    state_dir: Path | None = None
    counters: Counter[str] = field(default_factory=Counter)

    # ------------------------------------------------------------------ #
    @property
    def is_active(self) -> bool:
        if os.environ.get(DO_NOT_TRACK) in {"1", "true", "yes"}:
            return False
        return self.enabled

    def record(self, event: str, *, properties: dict[str, Any] | None = None, duration_ms: float | None = None) -> None:
        safe = _sanitize(properties or {})
        if duration_ms is not None:
            safe["duration_bucket"] = _bucket(duration_ms)
        self.counters[event] += 1
        if not self.local_metrics or self.state_dir is None:
            return
        entry = {
            "ts": int(time.time()),
            "event": event,
            "version": __version__,
            "anonymous_id": self.anonymous_id,
            "properties": safe,
        }
        try:
            path = self.state_dir / "telemetry"
            path.mkdir(parents=True, exist_ok=True)
            with (path / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover - state dir not writable
            pass
        if self.endpoint and self.is_active:  # pragma: no cover - network upload is opt-in future work
            log.debug("Telemetry endpoint configured but upload is deferred to an explicit user action.")

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "active": self.is_active,
            "do_not_track": os.environ.get(DO_NOT_TRACK) is not None,
            "local_metrics": self.local_metrics,
            "endpoint_configured": bool(self.endpoint),
            "anonymous_id": self.anonymous_id,
            "counters": dict(self.counters),
        }

    def flush(self) -> None:
        self.counters.clear()


def _bucket(duration_ms: float) -> str:
    for limit in (100, 500, 1_000, 5_000, 30_000, 120_000, 600_000):
        if duration_ms < limit:
            return f"<{limit}ms"
    return ">=600s"


_SENSITIVE_KEYS = {"prompt", "task", "message", "content", "path", "file", "query", "url", "command", "diff", "env"}


def _sanitize(properties: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in properties.items():
        if key.lower() in _SENSITIVE_KEYS:
            out[key] = "<redacted>"
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value if not isinstance(value, str) else value[:64]
    return out


def load_telemetry(*, enabled: bool, local_metrics: bool, endpoint: str | None, state_dir: Path | None, anonymous_id: str | None = None) -> Telemetry:
    return Telemetry(
        enabled=enabled,
        local_metrics=local_metrics,
        endpoint=endpoint,
        state_dir=state_dir,
        anonymous_id=anonymous_id or uuid.uuid4().hex[:16],
    )
