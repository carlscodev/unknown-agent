"""Persistent project memory (SQLite + FTS5, with a Markdown mirror).

This is the layer that makes the second run on a repository better than the
first: conventions, architecture facts, decisions, gotchas and reusable commands
survive across tasks, sessions and machines (the database is a file the user owns
and can back up, inspect, export or delete).

Schema (v1)
-----------
``facts``      id, text, kind, tags, source, run_id, confidence, created_at, updated_at, uses, last_used, pinned
``facts_fts``  FTS5 virtual table over (text, tags) kept in sync by triggers
``meta``       schema version and free-form key/values

Design rules:
* every fact carries provenance (``source``, ``run_id``) and a confidence score;
* search degrades gracefully - FTS5 when available, LIKE otherwise;
* pruning respects ``memory.retention_days`` but never deletes pinned facts;
* nothing is written without a caller explicitly asking to remember.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.config.secrets import redact_text
from unknown_agent.observability.logging import get_logger

log = get_logger("memory.store")

SCHEMA_VERSION = 1
FACT_KINDS = ("convention", "architecture", "gotcha", "decision", "command", "fact")

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'fact',
    tags        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    run_id      TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0.8,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    uses        INTEGER NOT NULL DEFAULT 0,
    last_used   REAL,
    pinned      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind);
CREATE INDEX IF NOT EXISTS idx_facts_created ON facts(created_at DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, tags, content='facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, tags) VALUES('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, tags) VALUES('delete', old.id, old.text, old.tags);
  INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
"""


@dataclass(slots=True)
class MemoryStats:
    facts: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    oldest: float | None = None
    newest: float | None = None
    database_bytes: int = 0
    fts: bool = False
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "by_kind": self.by_kind,
            "oldest": self.oldest,
            "newest": self.newest,
            "database_bytes": self.database_bytes,
            "fts": self.fts,
            "path": self.path,
        }


class MemoryStore:
    """SQLite-backed long-term memory for one project."""

    def __init__(self, db_path: Path, *, markdown_dir: Path | None = None, fts_enabled: bool = True) -> None:
        self.db_path = Path(db_path)
        self.markdown_dir = Path(markdown_dir) if markdown_dir else None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts = False
        self.connection = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._migrate(fts_enabled=fts_enabled)

    # -- lifecycle ---------------------------------------------------------
    def _migrate(self, *, fts_enabled: bool) -> None:
        self.connection.executescript(SCHEMA)
        if fts_enabled:
            try:
                self.connection.executescript(FTS_SCHEMA)
                self._fts = True
            except sqlite3.OperationalError:  # pragma: no cover - SQLite built without FTS5
                log.warning("SQLite FTS5 unavailable; falling back to LIKE search")
                self._fts = False
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def remember(
        self,
        text: str,
        *,
        kind: str = "fact",
        tags: Sequence[str] | None = None,
        source: str = "",
        run_id: str = "",
        confidence: float = 0.8,
        pinned: bool = False,
    ) -> int:
        """Store a fact.  Returns the fact id, or the id of an existing duplicate."""

        cleaned = redact_text(" ".join((text or "").split()))
        if not cleaned:
            raise ValueError("refusing to remember an empty fact")
        if len(cleaned) > 4000:
            cleaned = cleaned[:4000].rsplit(" ", 1)[0] + "..."
        kind = kind if kind in FACT_KINDS else "fact"
        tag_text = ",".join(sorted({tag.strip().lower() for tag in (tags or []) if tag.strip()}))
        now = time.time()

        existing = self.connection.execute("SELECT id FROM facts WHERE text = ?", (cleaned,)).fetchone()
        if existing:
            self.connection.execute(
                "UPDATE facts SET updated_at = ?, confidence = MAX(confidence, ?), uses = uses + 1 WHERE id = ?",
                (now, float(confidence), existing["id"]),
            )
            self.connection.commit()
            return int(existing["id"])

        cursor = self.connection.execute(
            """
            INSERT INTO facts(text, kind, tags, source, run_id, confidence, created_at, updated_at, pinned)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cleaned, kind, tag_text, source[:200], run_id[:80], float(max(0.0, min(1.0, confidence))), now, now, int(pinned)),
        )
        self.connection.commit()
        fact_id = int(cursor.lastrowid or 0)
        if self.markdown_dir is not None:
            self._mirror()
        log.debug("remembered fact %s (%s)", fact_id, kind)
        return fact_id

    def forget(self, fact_id: int) -> bool:
        cursor = self.connection.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self.connection.commit()
        if cursor.rowcount and self.markdown_dir is not None:
            self._mirror()
        return bool(cursor.rowcount)

    def forget_matching(self, pattern: str) -> int:
        cursor = self.connection.execute("DELETE FROM facts WHERE text LIKE ? AND pinned = 0", (f"%{pattern}%",))
        self.connection.commit()
        return int(cursor.rowcount or 0)

    def pin(self, fact_id: int, pinned: bool = True) -> bool:
        cursor = self.connection.execute("UPDATE facts SET pinned = ? WHERE id = ?", (int(pinned), fact_id))
        self.connection.commit()
        return bool(cursor.rowcount)

    def prune(self, retention_days: int) -> int:
        """Delete unpinned facts older than the retention window."""

        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86_400
        cursor = self.connection.execute("DELETE FROM facts WHERE created_at < ? AND pinned = 0", (cutoff,))
        self.connection.commit()
        removed = int(cursor.rowcount or 0)
        if removed and self.markdown_dir is not None:
            self._mirror()
        return removed

    # -- reads -------------------------------------------------------------
    def search(self, query: str, *, limit: int = 10, kind: str | None = None, tags: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Ranked search.  FTS5 when available, keyword/LIKE ranking otherwise."""

        query = (query or "").strip()
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if tags:
            clauses.append("(" + " OR ".join("tags LIKE ?" for _ in tags) + ")")
            params.extend(f"%{tag.lower()}%" for tag in tags)

        if not query:
            sql = "SELECT * FROM facts" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
            rows = self.connection.execute(sql, [*params, limit]).fetchall()
        elif self._fts:
            match = _fts_query(query)
            where = " AND ".join(["f.rowid IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?)", *clauses])
            sql = f"""
                SELECT f.*, bm25(facts_fts) AS score
                FROM facts f
                JOIN facts_fts ON facts_fts.rowid = f.id
                WHERE {where}
                ORDER BY score, f.pinned DESC, f.confidence DESC
                LIMIT ?
            """
            params = [match, *params, limit]
            try:
                rows = self.connection.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = self._like_search(query, clauses, params, limit)
        else:
            rows = self._like_search(query, clauses, params, limit)

        results = [dict(row) for row in rows]
        if results:
            ids = [row["id"] for row in results]
            self.connection.execute(
                f"UPDATE facts SET uses = uses + 1, last_used = ? WHERE id IN ({','.join('?' * len(ids))})",
                [time.time(), *ids],
            )
            self.connection.commit()
        return results

    def _like_search(self, query: str, clauses: list[str], params: list[Any], limit: int) -> list[sqlite3.Row]:
        terms = [term for term in query.split() if term]
        like_clauses = ["(text LIKE ? OR tags LIKE ?)" for _ in terms]
        like_params: list[Any] = []
        for term in terms:
            like_params.extend([f"%{term}%", f"%{term}%"])
        where = " AND ".join([*like_clauses, *clauses]) if like_clauses or clauses else "1"
        sql = f"SELECT * FROM facts WHERE {where} ORDER BY pinned DESC, confidence DESC, updated_at DESC LIMIT ?"
        return self.connection.execute(sql, [*like_params, *params, limit]).fetchall()

    def get(self, fact_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return dict(row) if row else None

    def list(self, *, limit: int = 100, kind: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        if kind:
            rows = self.connection.execute(
                "SELECT * FROM facts WHERE kind = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?", (kind, limit, offset)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM facts ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def recall_for_task(self, task: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Blend keyword recall with recency: what the agent should know up front."""

        seen: dict[int, dict[str, Any]] = {}
        for term in _keywords(task):
            for fact in self.search(term, limit=limit):
                seen.setdefault(int(fact["id"]), fact)
        if len(seen) < limit:
            for fact in self.list(limit=limit):
                seen.setdefault(int(fact["id"]), fact)
        ranked = sorted(seen.values(), key=lambda item: (item.get("pinned", 0), item.get("confidence", 0), item.get("updated_at", 0)), reverse=True)
        return ranked[:limit]

    def stats(self) -> MemoryStats:
        total = self.connection.execute("SELECT COUNT(*) AS c FROM facts").fetchone()["c"]
        by_kind = {
            row["kind"]: row["c"]
            for row in self.connection.execute("SELECT kind, COUNT(*) AS c FROM facts GROUP BY kind").fetchall()
        }
        bounds = self.connection.execute("SELECT MIN(created_at) AS oldest, MAX(created_at) AS newest FROM facts").fetchone()
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return MemoryStats(
            facts=int(total),
            by_kind=by_kind,
            oldest=bounds["oldest"],
            newest=bounds["newest"],
            database_bytes=size,
            fts=self._fts,
            path=str(self.db_path),
        )

    def export(self, path: Path | None = None) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": time.time(),
            "facts": [
                dict(row)
                for row in self.connection.execute("SELECT * FROM facts ORDER BY id").fetchall()
            ],
        }
        if path:
            Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload

    def import_facts(self, payload: dict[str, Any]) -> int:
        imported = 0
        for fact in payload.get("facts", []):
            self.remember(
                fact.get("text", ""),
                kind=fact.get("kind", "fact"),
                tags=(fact.get("tags") or "").split(",") if fact.get("tags") else [],
                source=fact.get("source", "import"),
                run_id=fact.get("run_id", ""),
                confidence=float(fact.get("confidence", 0.8)),
                pinned=bool(fact.get("pinned", 0)),
            )
            imported += 1
        return imported

    # -- markdown mirror ---------------------------------------------------
    def _mirror(self) -> None:
        if self.markdown_dir is None:
            return
        self.markdown_dir.mkdir(parents=True, exist_ok=True)
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in self.connection.execute("SELECT * FROM facts ORDER BY kind, updated_at DESC").fetchall():
            grouped.setdefault(row["kind"], []).append(row)
        for kind, rows in grouped.items():
            lines = [f"# Memory: {kind}", "", f"_{len(rows)} fact(s). Generated by Unknown Agent - safe to edit by hand._", ""]
            for row in rows:
                stamp = time.strftime("%Y-%m-%d", time.localtime(row["created_at"]))
                tags = f" `{row['tags']}`" if row["tags"] else ""
                lines.append(f"- {row['text']}  _{stamp}, confidence {row['confidence']:.1f}{tags}_")
            (self.markdown_dir / f"{kind}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fts_query(query: str) -> str:
    """Turn a user query into a safe FTS5 MATCH expression (prefix search, OR terms)."""

    terms = [term for term in "".join(char if char.isalnum() or char in "_-" else " " for char in query).split() if len(term) > 1]
    return " OR ".join(f"{term}*" for term in terms) or '""'


def _keywords(text: str, *, limit: int = 6) -> list[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "for", "with", "of", "in", "on", "is", "are", "be", "add", "fix",
        "make", "update", "please", "using", "use", "this", "that", "it", "we", "you", "our",
    }
    words = [word.strip(".,:;()[]'\"").lower() for word in (text or "").split()]
    ranked = [word for word in words if len(word) > 2 and word not in stop]
    unique: list[str] = []
    for word in ranked:
        if word not in unique:
            unique.append(word)
    return unique[:limit]


__all__ = ["MemoryStore", "MemoryStats", "FACT_KINDS", "SCHEMA_VERSION"]
