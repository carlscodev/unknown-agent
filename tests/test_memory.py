"""Layered memory: persistent facts, sessions, procedures, working memory."""

from __future__ import annotations

from pathlib import Path

from unknown_agent.memory.procedures import ProcedureLibrary
from unknown_agent.memory.sessions import SessionStore
from unknown_agent.memory.store import MemoryStore
from unknown_agent.memory.working import WorkingMemory


def test_facts_round_trip_and_search(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    fact_id = store.remember("the API lives in src/api", kind="architecture", tags=["api"])
    assert fact_id > 0
    hits = store.search("api")
    assert any("src/api" in hit["text"] for hit in hits)
    assert store.get(fact_id)["text"].startswith("the API")
    assert store.stats().facts == 1
    assert store.forget(fact_id) is True
    assert store.stats().facts == 0
    store.close()


def test_facts_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    first = MemoryStore(path)
    first.remember("deploys run from main", kind="workflow")
    first.close()
    second = MemoryStore(path)
    assert second.stats().facts == 1
    second.close()


def test_markdown_mirror_is_human_readable(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db", markdown_dir=tmp_path / "memory")
    store.remember("tests use pytest", kind="convention")
    files = list((tmp_path / "memory").rglob("*.md"))
    assert files, "memory should be mirrored as markdown for humans and diffs"
    assert "pytest" in files[0].read_text()


def test_procedures_are_discovered_and_parsed(tmp_path: Path) -> None:
    directory = tmp_path / "procedures"
    directory.mkdir()
    (directory / "add-endpoint.md").write_text(
        "---\nname: add-endpoint\ntriggers: [endpoint, route]\n---\n\n1. write the test first\n2. implement\n",
        encoding="utf-8",
    )
    library = ProcedureLibrary(directory)
    items = library.list(refresh=True)
    assert [item.name for item in items] == ["add-endpoint"]
    matches = library.match("add a new endpoint")
    assert matches
    assert matches[0].name == "add-endpoint"
    assert "test first" in matches[0].body


def test_sessions_record_and_query(tmp_path: Path) -> None:
    from unknown_agent.memory.sessions import SessionRecord

    store = SessionStore(tmp_path / "sessions.db")
    store.upsert(
        SessionRecord(
            id="run-1",
            task="fix parser",
            summary="fixed the parser",
            status="completed",
            files_changed=["src/p.py"],
        )
    )
    assert [item.id for item in store.list()] == ["run-1"]
    assert store.last().task == "fix parser"
    assert store.stats()["sessions"] >= 1
    store.close()


def test_working_memory_detects_repeated_failures() -> None:
    memory = WorkingMemory()
    memory.start("do the thing")
    memory.record_observation("gate", "tests failed: 2 errors", ok=False)
    memory.record_observation("gate", "tests failed: 2 errors", ok=False)
    assert memory.repeated_failure("tests failed: 5 errors", threshold=2)  # numbers are normalised
    memory.learn("the suite needs pythonpath=src")
    assert any("pythonpath" in item for item in memory.facts_learned)
