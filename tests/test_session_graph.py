"""Durability, branching and conflict tests for event-sourced sessions."""

from __future__ import annotations

from pathlib import Path

import pytest

from unknown_agent.memory.session_graph import (
    BranchConflict,
    ConcurrentAppend,
    SessionGraphStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> SessionGraphStore:
    graph = SessionGraphStore(tmp_path / "sessions.db", snapshot_interval=3)
    yield graph
    graph.close()


def test_append_materializes_reproducible_state(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    store.append(main.id, "message.added", {"content": "build it"}, role="user")
    store.append(main.id, "value.set", {"key": "model", "value": "frontier"})
    store.append(main.id, "artifact.updated", {"path": "src/app.py", "sha256": "abc"})

    state = store.materialize(main.id)
    assert state["messages"][0]["content"] == "build it"
    assert state["values"]["model"] == "frontier"
    assert state["artifacts"]["src/app.py"]["sha256"] == "abc"
    assert state["event_count"] == 3


def test_fork_shares_ancestor_without_copying_events(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    ancestor = store.append(main.id, "message.added", {"content": "root"}, role="user")
    child = store.fork(main.id, "experiment")
    store.append(child.id, "message.added", {"content": "alternative"}, role="assistant")

    assert [event.id for event in store.timeline(child.id)] == [ancestor.id, ancestor.id + 1]
    assert len(store.own_events(child.id)) == 1
    assert len(store.own_events(main.id)) == 1


def test_fork_at_old_event_excludes_later_parent_history(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    first = store.append(main.id, "value.set", {"key": "choice", "value": "a"})
    store.append(main.id, "value.set", {"key": "choice", "value": "b"})
    child = store.fork(main.id, "from-first", at_event_id=first.id)

    assert [event.id for event in store.timeline(child.id)] == [first.id]
    assert store.materialize(child.id)["values"]["choice"] == "a"


def test_optimistic_head_rejects_stale_writer(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    first = store.append(main.id, "message.added", {"content": "one"})
    second = store.append(main.id, "message.added", {"content": "two"}, expected_head=first.id)
    with pytest.raises(ConcurrentAppend, match="head moved"):
        store.append(main.id, "message.added", {"content": "stale"}, expected_head=first.id)
    assert store.branch(main.id).head_event_id == second.id  # type: ignore[union-attr]


def test_hash_chain_detects_tampering(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    event = store.append(main.id, "value.set", {"key": "safe", "value": True})
    assert store.verify(main.id) == []

    store.connection.execute(
        "UPDATE session_events SET payload=? WHERE id=?",
        ('{"key":"safe","value":false}', event.id),
    )
    assert "content hash mismatch" in store.verify(main.id)[0]


def test_clean_merge_replays_with_provenance(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    store.append(main.id, "value.set", {"key": "base", "value": 1})
    child = store.fork(main.id, "approach-b")
    source = store.append(child.id, "value.set", {"key": "strategy", "value": "b"})

    preview = store.preview_merge(child.id, main.id)
    assert preview.clean and len(preview.events_to_replay) == 1
    merged = store.merge(child.id, main.id)

    assert store.materialize(main.id)["values"]["strategy"] == "b"
    assert merged[0].payload["_merge_provenance"]["source_event"] == source.id
    assert store.branch(child.id).status == "merged"  # type: ignore[union-attr]


def test_divergent_semantic_write_blocks_merge(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    store.append(main.id, "value.set", {"key": "model", "value": "a"})
    child = store.fork(main.id, "experiment")
    store.append(child.id, "value.set", {"key": "model", "value": "b"})
    store.append(main.id, "value.set", {"key": "model", "value": "c"})

    preview = store.preview_merge(child.id, main.id)
    assert preview.conflicts == ["value:model"]
    with pytest.raises(BranchConflict, match="value:model"):
        store.merge(child.id, main.id)


def test_identical_semantic_write_is_not_a_conflict(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    child = store.fork(main.id, "same")
    store.append(child.id, "value.set", {"key": "mode", "value": "high"})
    store.append(main.id, "value.set", {"key": "mode", "value": "high"})
    assert store.preview_merge(child.id, main.id).clean


def test_snapshot_has_stable_state_hash(store: SessionGraphStore) -> None:
    main = store.create_session("s1")
    store.append(main.id, "message.added", {"content": "hello"})
    first = store.snapshot(main.id)
    second = store.snapshot(main.id)
    assert first["state_hash"] == second["state_hash"]
    assert first["event_id"] == second["event_id"]


def test_cannot_merge_different_sessions(store: SessionGraphStore) -> None:
    one = store.create_session("s1")
    two = store.create_session("s2")
    with pytest.raises(ValueError, match="different sessions"):
        store.preview_merge(one.id, two.id)


def test_wal_mode_supports_concurrent_readers(store: SessionGraphStore) -> None:
    mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
