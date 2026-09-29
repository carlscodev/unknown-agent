"""Deterministic, durable context compaction for long-running missions.

The compactor is deliberately model-independent: a provider outage must never make
an already-running mission unable to fit its own history into a context window.
Every checkpoint stores the exact source messages, a content hash, the generated
summary and token accounting in SQLite WAL.  A checkpoint can therefore be
verified and replayed after process termination.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from unknown_agent.core.models import Message, ToolCall

CHARS_PER_TOKEN = 4
CHECKPOINT_OPEN = "<context_checkpoint"
CHECKPOINT_CLOSE = "</context_checkpoint>"


def _tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _message_cost(message: Message) -> int:
    calls = json.dumps([call.to_dict() for call in message.tool_calls], sort_keys=True)
    return _tokens(message.content + calls) + 20


def _serialise(messages: list[Message]) -> str:
    payload = [
        {
            "role": item.role,
            "content": item.content,
            "tool_calls": [call.to_dict() for call in item.tool_calls],
            "tool_call_id": item.tool_call_id,
            "name": item.name,
            "created_at": item.created_at,
        }
        for item in messages
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deserialise(raw: str) -> list[Message]:
    messages: list[Message] = []
    for item in json.loads(raw):
        calls = [
            ToolCall(
                id=str(call["id"]),
                name=str(call["name"]),
                arguments=dict(call.get("arguments") or {}),
                raw_arguments=call.get("raw_arguments"),
            )
            for call in item.get("tool_calls", [])
        ]
        messages.append(
            Message(
                role=item["role"],
                content=str(item.get("content") or ""),
                tool_calls=calls,
                tool_call_id=item.get("tool_call_id"),
                name=item.get("name"),
                created_at=float(item.get("created_at") or time.time()),
            )
        )
    return messages


class CompactionRecord(BaseModel):
    """Validated immutable representation of one persisted checkpoint."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    scope: str = ""
    tier: Literal[1, 2, 3]
    source_hash: str = Field(min_length=64, max_length=64)
    source_count: int = Field(ge=1)
    source_tokens: int = Field(ge=1)
    compacted_tokens: int = Field(ge=1)
    summary: str = Field(min_length=1)
    created_at: float = Field(gt=0)


class CompactionResult(BaseModel):
    """The fitted messages and accounting returned to prompt assembly."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[Message]
    changed: bool = False
    record: CompactionRecord | None = None
    dropped_messages: int = Field(default=0, ge=0)


class CompactionStore:
    """SQLite WAL journal containing exact compaction inputs and outputs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS context_compactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                tier INTEGER NOT NULL CHECK(tier BETWEEN 1 AND 3),
                source_hash TEXT NOT NULL,
                source_json TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                source_tokens INTEGER NOT NULL,
                compacted_tokens INTEGER NOT NULL,
                summary TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(scope, source_hash, tier)
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> CompactionStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save(self, record: CompactionRecord, source: list[Message]) -> CompactionRecord:
        raw = _serialise(source)
        actual_hash = hashlib.sha256(raw.encode()).hexdigest()
        if actual_hash != record.source_hash:
            raise ValueError("compaction source hash does not match source messages")
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO context_compactions
                    (scope, tier, source_hash, source_json, source_count,
                     source_tokens, compacted_tokens, summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.scope,
                    record.tier,
                    record.source_hash,
                    raw,
                    record.source_count,
                    record.source_tokens,
                    record.compacted_tokens,
                    record.summary,
                    record.created_at,
                ),
            )
        row = self.connection.execute(
            "SELECT * FROM context_compactions WHERE scope=? AND source_hash=? AND tier=?",
            (record.scope, record.source_hash, record.tier),
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite contract guard
            raise sqlite3.IntegrityError("compaction insert did not produce a row")
        return self._record(row)

    def get(self, record_id: int) -> CompactionRecord:
        row = self.connection.execute("SELECT * FROM context_compactions WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown compaction {record_id}")
        self._verify_row(row)
        return self._record(row)

    def source(self, record_id: int) -> list[Message]:
        row = self.connection.execute("SELECT * FROM context_compactions WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown compaction {record_id}")
        self._verify_row(row)
        return _deserialise(str(row["source_json"]))

    def list(self, scope: str, *, limit: int = 100) -> list[CompactionRecord]:
        rows = self.connection.execute(
            "SELECT * FROM context_compactions WHERE scope=? ORDER BY id DESC LIMIT ?",
            (scope, max(1, min(limit, 1000))),
        ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _verify_row(row: sqlite3.Row) -> None:
        actual = hashlib.sha256(str(row["source_json"]).encode()).hexdigest()
        if actual != row["source_hash"]:
            raise ValueError(f"compaction {row['id']} failed integrity verification")

    @staticmethod
    def _record(row: sqlite3.Row) -> CompactionRecord:
        tier = int(row["tier"])
        if tier not in {1, 2, 3}:
            raise ValueError(f"invalid persisted compaction tier {tier}")
        return CompactionRecord(
            id=int(row["id"]),
            scope=str(row["scope"]),
            tier=cast("Literal[1, 2, 3]", tier),
            source_hash=str(row["source_hash"]),
            source_count=int(row["source_count"]),
            source_tokens=int(row["source_tokens"]),
            compacted_tokens=int(row["compacted_tokens"]),
            summary=str(row["summary"]),
            created_at=float(row["created_at"]),
        )


class ContextCompactor:
    """Fit a conversation while preserving anchors, causality and recent turns."""

    def __init__(self, store: CompactionStore | None = None, *, preserve_recent: int = 6) -> None:
        self.store = store
        self.preserve_recent = max(2, preserve_recent)

    def compact(self, messages: list[Message], *, target_tokens: int, scope: str = "") -> CompactionResult:
        if target_tokens < 256:
            raise ValueError("target_tokens must be at least 256")
        current = sum(_message_cost(item) for item in messages)
        if current <= target_tokens:
            return CompactionResult(messages=list(messages))

        systems = [item for item in messages if item.role == "system"]
        conversation = [item for item in messages if item.role != "system"]
        if len(conversation) <= 2:
            return CompactionResult(messages=self._hard_fit(messages, target_tokens), changed=True)

        # The originating request is an invariant: summaries may compress history,
        # but they never replace the user's task anchor.
        anchor = conversation[0]
        recent_start = max(1, len(conversation) - self.preserve_recent)
        while recent_start > 1 and conversation[recent_start].role == "tool":
            recent_start -= 1
        source = conversation[1:recent_start]
        recent = conversation[recent_start:]
        if not source:
            fitted = [*systems, anchor, *self._fit_tail(recent, target_tokens - sum(_message_cost(x) for x in [*systems, anchor]))]
            return CompactionResult(messages=fitted, changed=True, dropped_messages=len(messages) - len(fitted))

        fixed_cost = sum(_message_cost(item) for item in [*systems, anchor, *recent])
        summary_target = max(160, target_tokens - fixed_cost - 30)
        pressure = sum(_message_cost(item) for item in source) / summary_target
        tier: Literal[1, 2, 3] = 1 if pressure <= 2.0 else (2 if pressure <= 5.0 else 3)
        summary = self._summarise(source, tier=tier, max_tokens=summary_target)
        checkpoint = Message.user(summary)
        fitted = [*systems, anchor, checkpoint, *recent]
        if sum(_message_cost(item) for item in fitted) > target_tokens:
            # Escalate deterministically before sacrificing recent causal context.
            tier = 3
            summary = self._summarise(source, tier=3, max_tokens=max(96, summary_target // 2))
            checkpoint = Message.user(summary)
            fitted = [*systems, anchor, checkpoint, *recent]
        if sum(_message_cost(item) for item in fitted) > target_tokens:
            fixed = [*systems, anchor, checkpoint]
            recent = self._fit_tail(recent, target_tokens - sum(_message_cost(x) for x in fixed))
            fitted = [*fixed, *recent]

        raw = _serialise(source)
        record = CompactionRecord(
            scope=scope,
            tier=tier,
            source_hash=hashlib.sha256(raw.encode()).hexdigest(),
            source_count=len(source),
            source_tokens=sum(_message_cost(item) for item in source),
            compacted_tokens=_message_cost(checkpoint),
            summary=summary,
            created_at=time.time(),
        )
        if self.store is not None:
            record = self.store.save(record, source)
        return CompactionResult(
            messages=fitted,
            changed=True,
            record=record,
            dropped_messages=max(0, len(messages) - len(fitted) + 1),
        )

    @staticmethod
    def _hard_fit(messages: list[Message], target_tokens: int) -> list[Message]:
        systems = [item for item in messages if item.role == "system"]
        tail = [item for item in messages if item.role != "system"]
        remaining = target_tokens - sum(_message_cost(item) for item in systems)
        return [*systems, *ContextCompactor._fit_tail(tail, remaining)]

    @staticmethod
    def _fit_tail(messages: list[Message], target_tokens: int) -> list[Message]:
        kept: list[Message] = []
        used = 0
        for item in reversed(messages):
            cost = _message_cost(item)
            if kept and used + cost > max(0, target_tokens):
                break
            kept.append(item)
            used += cost
        kept.reverse()
        while kept and kept[0].role == "tool":
            kept.pop(0)
        return kept

    def _summarise(self, messages: list[Message], *, tier: int, max_tokens: int) -> str:
        categories: dict[str, list[str]] = {
            "decisions": [], "changes": [], "evidence": [], "failures": [], "requests": [], "other": []
        }
        per_item = {1: 420, 2: 220, 3: 120}[tier]
        for message in messages:
            text = self._normalise(message.content)
            if not text:
                if message.tool_calls:
                    text = "called " + ", ".join(call.name for call in message.tool_calls)
                else:
                    continue
            lowered = text.lower()
            if message.role == "tool" and any(word in lowered for word in ("error", "failed", "failure", "traceback")):
                category = "failures"
            elif message.role == "tool" and any(word in lowered for word in ("pass", "success", "created", "updated", "wrote")):
                category = "evidence"
            elif any(word in lowered for word in ("decid", "must", "will use", "chosen", "constraint")):
                category = "decisions"
            elif any(word in lowered for word in ("file", "patch", "edit", "commit", "implement")):
                category = "changes"
            elif message.role == "user":
                category = "requests"
            else:
                category = "other"
            prefix = message.name or message.role
            line = f"[{prefix}] {text[:per_item]}"
            if line not in categories[category]:
                categories[category].append(line)

        lines = [
            f'{CHECKPOINT_OPEN} tier="{tier}" source_messages="{len(messages)}">',
            "Durable deterministic summary of earlier conversation. Treat retained recent messages as newer authority.",
        ]
        labels = (("decisions", "Decisions and constraints"), ("changes", "Changes and artifacts"),
                  ("evidence", "Evidence"), ("failures", "Failures and blockers"),
                  ("requests", "Requests"), ("other", "Other retained facts"))
        max_per_category = {1: 12, 2: 7, 3: 4}[tier]
        for key, label in labels:
            items = categories[key][:max_per_category]
            if items:
                lines.append(f"{label}:")
                lines.extend(f"- {item}" for item in items)
        lines.append(CHECKPOINT_CLOSE)
        text = "\n".join(lines)
        max_chars = max(384, max_tokens * CHARS_PER_TOKEN)
        if len(text) > max_chars:
            text = text[: max_chars - len(CHECKPOINT_CLOSE) - 2].rstrip() + "\n" + CHECKPOINT_CLOSE
        return text

    @staticmethod
    def _normalise(text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        # Existing checkpoints are data, not instructions. Keep their facts while
        # removing nested control wrappers during recursive compaction.
        text = re.sub(r"</?context_checkpoint[^>]*>", "", text)
        return text


__all__ = ["CompactionRecord", "CompactionResult", "CompactionStore", "ContextCompactor"]
