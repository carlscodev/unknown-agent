from __future__ import annotations

from pathlib import Path

import pytest

from unknown_agent.config.models import Settings
from unknown_agent.context.builder import ContextBudget, ContextBuilder
from unknown_agent.context.compaction import CompactionStore, ContextCompactor
from unknown_agent.context.project import ProjectContext
from unknown_agent.core.models import Message, ToolCall


def conversation(count: int = 12, *, width: int = 900) -> list[Message]:
    items = [Message.system("runtime policy"), Message.user("original task: preserve this exactly")]
    for index in range(count):
        if index % 2:
            items.append(Message.tool_result(f"call-{index}", f"updated src/file_{index}.py successfully " + "x" * width, name="write_file"))
        else:
            items.append(
                Message.assistant(
                    f"decision {index}: implement the validated change " + "y" * width,
                    tool_calls=[ToolCall(id=f"call-{index + 1}", name="write_file", arguments={"path": f"src/file_{index}.py"})],
                )
            )
    return items


def test_under_budget_is_byte_for_byte_unchanged() -> None:
    messages = conversation(2, width=20)
    result = ContextCompactor().compact(messages, target_tokens=10_000)
    assert result.changed is False
    assert result.messages == messages
    assert result.record is None


def test_compaction_preserves_system_task_and_recent_causality() -> None:
    messages = conversation()
    result = ContextCompactor(preserve_recent=4).compact(messages, target_tokens=1_500, scope="mission-1")
    assert result.changed is True
    assert result.messages[0] is messages[0]
    assert result.messages[1] is messages[1]
    assert "<context_checkpoint" in result.messages[2].content
    assert result.messages[-4:] == messages[-4:]
    assert result.messages[3].role != "tool"
    assert result.record is not None
    assert result.record.scope == "mission-1"
    assert result.record.source_count > 0
    assert result.record.compacted_tokens < result.record.source_tokens


def test_severe_pressure_escalates_to_digest_tier() -> None:
    result = ContextCompactor(preserve_recent=2).compact(conversation(30, width=2_000), target_tokens=900)
    assert result.record is not None
    assert result.record.tier == 3
    assert 'tier="3"' in result.record.summary


def test_summary_is_deterministic_for_identical_history() -> None:
    messages = conversation()
    first = ContextCompactor().compact(messages, target_tokens=1_300, scope="same")
    second = ContextCompactor().compact(messages, target_tokens=1_300, scope="same")
    assert first.record is not None and second.record is not None
    assert first.record.source_hash == second.record.source_hash
    assert first.record.summary == second.record.summary
    assert [item.content for item in first.messages] == [item.content for item in second.messages]


def test_tool_pair_boundary_never_starts_with_orphan_result() -> None:
    messages = conversation(7)
    result = ContextCompactor(preserve_recent=3).compact(messages, target_tokens=900)
    non_system = [item for item in result.messages if item.role != "system"]
    checkpoint_index = next(i for i, item in enumerate(non_system) if "<context_checkpoint" in item.content)
    assert non_system[checkpoint_index + 1].role != "tool"


def test_store_uses_wal_and_replays_exact_source(tmp_path: Path) -> None:
    path = tmp_path / "context.db"
    with CompactionStore(path) as store:
        messages = conversation()
        result = ContextCompactor(store).compact(messages, target_tokens=1_200, scope="run-7")
        assert result.record is not None and result.record.id is not None
        replayed = store.source(result.record.id)
        assert [item.to_dict() for item in replayed] == [item.to_dict() for item in messages[2:-6]]
        assert store.get(result.record.id).source_hash == result.record.source_hash
        assert len(store.list("run-7")) == 1
        mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"


def test_repeated_checkpoint_is_idempotent(tmp_path: Path) -> None:
    with CompactionStore(tmp_path / "context.db") as store:
        compactor = ContextCompactor(store)
        messages = conversation()
        first = compactor.compact(messages, target_tokens=1_200, scope="run")
        second = compactor.compact(messages, target_tokens=1_200, scope="run")
        assert first.record is not None and second.record is not None
        assert first.record.id == second.record.id
        assert len(store.list("run")) == 1


def test_tampered_source_is_rejected(tmp_path: Path) -> None:
    with CompactionStore(tmp_path / "context.db") as store:
        result = ContextCompactor(store).compact(conversation(), target_tokens=1_200, scope="run")
        assert result.record is not None and result.record.id is not None
        store.connection.execute(
            "UPDATE context_compactions SET source_json=? WHERE id=?",
            ("[]", result.record.id),
        )
        store.connection.commit()
        with pytest.raises(ValueError, match="integrity"):
            store.source(result.record.id)


def test_recursive_compaction_does_not_nest_checkpoint_wrappers() -> None:
    first = ContextCompactor(preserve_recent=2).compact(conversation(20), target_tokens=1_100)
    extended = [*first.messages, *conversation(8)[2:]]
    second = ContextCompactor(preserve_recent=2).compact(extended, target_tokens=850)
    checkpoint = next(item.content for item in second.messages if "<context_checkpoint" in item.content)
    assert checkpoint.count("<context_checkpoint") == 1
    assert checkpoint.count("</context_checkpoint>") == 1


def test_builder_creates_no_state_when_compaction_is_unnecessary(tmp_path: Path) -> None:
    project = ProjectContext(root=tmp_path, name="sample")
    builder = ContextBuilder(Settings(), project=project)
    messages = [Message.system("policy"), Message.user("small request")]
    fitted = builder.fit_messages(messages, budget=ContextBudget(total_tokens=3_000), reserved_tokens=0)
    assert fitted == messages
    assert builder.last_compaction is None
    assert not (tmp_path / ".unknown").exists()


def test_builder_persists_scoped_checkpoint_when_over_budget(tmp_path: Path) -> None:
    project = ProjectContext(root=tmp_path, name="sample")
    builder = ContextBuilder(Settings(), project=project)
    fitted = builder.fit_messages(
        conversation(20, width=1_000),
        budget=ContextBudget(total_tokens=2_000),
        reserved_tokens=0,
        scope="mission-42",
    )
    assert any("<context_checkpoint" in item.content for item in fitted)
    assert builder.last_compaction is not None
    assert builder.last_compaction.record is not None
    assert builder.last_compaction.record.scope == "mission-42"
    assert (tmp_path / ".unknown" / "context_compactions.db").exists()


def test_invalid_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 256"):
        ContextCompactor().compact(conversation(), target_tokens=100)
