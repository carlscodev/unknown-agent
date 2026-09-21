"""The orchestration ledger: an append-only audit trail of every delegation.

Multi-agent systems fail in a specific way: nobody can say afterwards *who* did
*what*, *why* it was delegated, or *where* it went wrong.  The ledger exists so
that question always has an answer, in a format both a human and a script can read.

One JSON line per event, appended as it happens (never buffered to the end, so a
crash still leaves a trail).  Events are typed and carry the work id, the role,
the pattern, the budget state and the outcome.  ``ua orchestrate show`` renders
them; ``ua orchestrate ledger --json`` pipes them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from unknown_agent.observability.logging import get_logger

log = get_logger("orchestration.ledger")

__all__ = ["LedgerEvent", "Ledger", "EVENT_KINDS", "ledger_path_for", "list_ledgers", "read_ledger", "timeline"]

#: The event vocabulary.  Adding a kind is fine; renaming one breaks tooling.
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "run_started",
        "plan_selected",
        "work_created",
        "work_queued",
        "worker_started",
        "worker_finished",
        "work_failed",
        "work_retried",
        "work_blocked",
        "work_skipped",
        "work_cancelled",
        "dependency_failed",
        "conflict_detected",
        "merge_started",
        "merge_finished",
        "merge_conflict",
        "verify_started",
        "verify_finished",
        "escalation",
        "replan",
        "budget_warning",
        "budget_exhausted",
        "run_finished",
        "note",
    }
)


def ledger_path_for(project_root: Path | str, run_id: str = "") -> Path:
    """Where a run's ledger lives: ``.unknown/orchestration/<run-id>.jsonl``."""

    root = Path(project_root)
    directory = root / ".unknown" / "orchestration"
    name = f"{run_id or time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    return directory / name


@dataclass(slots=True)
class LedgerEvent:
    """One recorded event."""

    kind: str
    at: float = field(default_factory=time.time)
    work_id: str = ""
    role: str = ""
    run_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "at": round(self.at, 3)}
        if self.run_id:
            payload["run_id"] = self.run_id
        if self.work_id:
            payload["work_id"] = self.work_id
        if self.role:
            payload["role"] = self.role
        if self.data:
            payload["data"] = self.data
        return payload

    def line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @staticmethod
    def from_dict(payload: Mapping[str, Any]) -> LedgerEvent:
        return LedgerEvent(
            kind=str(payload.get("kind", "note")),
            at=float(payload.get("at") or time.time()),
            work_id=str(payload.get("work_id", "")),
            role=str(payload.get("role", "")),
            run_id=str(payload.get("run_id", "")),
            data=dict(payload.get("data") or {}),
        )


class Ledger:
    """Append-only event recorder with an in-memory mirror.

    The in-memory copy is what the UI reads (live progress); the file is what
    survives the process.  Writing is best-effort: a ledger that cannot be written
    must never fail a run, so I/O errors are logged and swallowed.
    """

    def __init__(self, path: Path | str | None = None, *, run_id: str = "", enabled: bool = True) -> None:
        self.path = Path(path) if path else None
        self.run_id = run_id
        self.enabled = enabled
        self.events: list[LedgerEvent] = []
        self._handle: Any = None
        if self.path is not None and enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a", encoding="utf-8")
            except OSError as exc:
                log.warning("orchestration ledger unavailable at %s: %s", self.path, exc)
                self.path = None

    # -- writing ----------------------------------------------------------- #
    def record(self, kind: str, *, work_id: str = "", role: str = "", **data: Any) -> LedgerEvent:
        """Append one event.  Unknown kinds are accepted but flagged in the payload."""

        event = LedgerEvent(
            kind=kind,
            work_id=work_id,
            role=role,
            run_id=self.run_id,
            data={key: value for key, value in data.items() if value is not None},
        )
        if kind not in EVENT_KINDS:
            event.data["unregistered_kind"] = True
        self.events.append(event)
        if self._handle is not None:
            try:
                self._handle.write(event.line() + "\n")
                self._handle.flush()
            except (OSError, ValueError) as exc:  # pragma: no cover - disk full, closed handle
                log.warning("ledger write failed: %s", exc)
                self._handle = None
        return event

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:  # pragma: no cover
                pass
            self._handle = None

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- reading ----------------------------------------------------------- #
    def by_kind(self, kind: str) -> list[LedgerEvent]:
        return [event for event in self.events if event.kind == kind]

    def for_work(self, work_id: str) -> list[LedgerEvent]:
        return [event for event in self.events if event.work_id == work_id]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for event in self.events:
            out[event.kind] = out.get(event.kind, 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        """The shape of the run, as data."""

        started = self.by_kind("worker_started")
        finished = self.by_kind("worker_finished")
        failed = self.by_kind("work_failed") + self.by_kind("worker_finished")
        ok = [event for event in finished if event.data.get("ok")]
        roles: dict[str, int] = {}
        for event in started:
            roles[event.role] = roles.get(event.role, 0) + 1
        return {
            "run_id": self.run_id,
            "events": len(self.events),
            "workers_started": len(started),
            "workers_succeeded": len(ok),
            "workers_failed": len([event for event in finished if not event.data.get("ok")]),
            "retries": len(self.by_kind("work_retried")),
            "escalations": len(self.by_kind("escalation")),
            "replans": len(self.by_kind("replan")),
            "conflicts": len(self.by_kind("merge_conflict")) + len(self.by_kind("conflict_detected")),
            "roles": roles,
            "path": str(self.path) if self.path else "",
        }

    @staticmethod
    def read(path: Path | str, *, kinds: Sequence[str] = ()) -> list[LedgerEvent]:
        """Read a ledger file back (``ua orchestrate ledger``)."""

        target = Path(path)
        events: list[LedgerEvent] = []
        wanted = set(kinds)
        try:
            with target.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = LedgerEvent.from_dict(payload)
                    if wanted and event.kind not in wanted:
                        continue
                    events.append(event)
        except OSError as exc:
            raise FileNotFoundError(f"cannot read ledger {target}: {exc}") from exc
        return events

    @staticmethod
    def list_ledgers(project_root: Path | str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Recent ledgers in a project, newest first."""

        directory = Path(project_root) / ".unknown" / "orchestration"
        if not directory.exists():
            return []
        files = sorted(directory.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]
        out: list[dict[str, Any]] = []
        for path in files:
            try:
                stat = path.stat()
                with path.open("r", encoding="utf-8") as handle:
                    lines = [line for line in handle if line.strip()]
            except OSError:
                continue
            first = json.loads(lines[0]) if lines else {}
            last = json.loads(lines[-1]) if lines else {}
            out.append(
                {
                    "path": str(path),
                    "run_id": first.get("run_id", path.stem),
                    "events": len(lines),
                    "modified": stat.st_mtime,
                    "size_bytes": stat.st_size,
                    "last_kind": last.get("kind", ""),
                }
            )
        return out


def timeline(events: Iterable[LedgerEvent]) -> list[str]:
    """A one-line-per-event human timeline (used by ``ua orchestrate show``)."""

    icons = {
        "run_started": "▶",
        "plan_selected": "◈",
        "work_created": "·",
        "worker_started": "→",
        "worker_finished": "✔",
        "work_failed": "✖",
        "work_retried": "↻",
        "work_blocked": "⛔",
        "merge_finished": "⊕",
        "merge_conflict": "⚠",
        "verify_finished": "◍",
        "escalation": "⇧",
        "replan": "↺",
        "run_finished": "■",
    }
    lines: list[str] = []
    for event in events:
        icon = icons.get(event.kind, "•")
        stamp = time.strftime("%H:%M:%S", time.localtime(event.at))
        subject = event.role or event.work_id or ""
        detail = event.data.get("summary") or event.data.get("note") or event.data.get("reason") or ""
        if isinstance(detail, dict):
            detail = json.dumps(detail, ensure_ascii=False)
        lines.append(f"{stamp} {icon} {event.kind:<18} {subject:<18} {str(detail)[:80]}")
    return lines


def list_ledgers(project_root: Path | str, *, limit: int = 20) -> list[dict[str, Any]]:
    """The most recent ledgers under ``<project>/.unknown/orchestration``.

    Newest first, newest determined by file mtime rather than by name, because a
    re-run with an explicit ``--run-id`` would otherwise sort into the past.  Used
    by ``ua orchestrate ledger`` to offer a picker instead of a path.
    """

    directory = Path(project_root) / ".unknown" / "orchestration"
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in directory.glob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover - raced away
            continue
        found.append(
            {
                "path": str(path),
                "run_id": path.stem,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )
    found.sort(key=lambda item: item["modified"], reverse=True)
    return found[: max(1, limit)]


def read_ledger(path: Path | str) -> list[LedgerEvent]:
    """Parse a ledger file back into events.  Malformed lines are skipped, not fatal.

    A ledger is an audit trail written by a process that may have been killed
    mid-append, so the last line is allowed to be a fragment.
    """

    events: list[LedgerEvent] = []
    target = Path(path)
    if not target.is_file():
        return events
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            # ``from_dict`` is the single reader: it knows the envelope nests its
            # fields under ``data``, so re-implementing that here would drift.
            events.append(LedgerEvent.from_dict(payload))
    return events
