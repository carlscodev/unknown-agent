from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from unknown_agent.config.loader import load_settings
from unknown_agent.core.models import RiskLevel, ToolCall, ToolResult
from unknown_agent.observability.events import EventBus
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext
from unknown_agent.tools.builtin import fs_tools, mutation_tools
from unknown_agent.tools.executor import ToolExecutor
from unknown_agent.tools.mutations import MutationQueue
from unknown_agent.tools.policy import PolicyEngine
from unknown_agent.tools.registry import ToolRegistry


class AssertContentTool(Tool):
    name = "assert_content"
    description = "Validate file content after staged mutations."
    category = "validation"
    risk = RiskLevel.EXECUTE
    parameters = (
        ParameterSpec("path", "string", required=True),
        ParameterSpec("expected", "string", required=True),
    )

    async def run(self, ctx: ToolContext, path: str = "", expected: str = "") -> ToolResult:
        actual = (ctx.project_root / path).read_text(encoding="utf-8")
        if actual.strip() != expected:
            return ToolResult.failure(f"expected {expected!r}, got {actual.strip()!r}")
        return ToolResult.success("validation passed")


class SlowWriteTool(Tool):
    name = "slow_write"
    description = "A cancellable write used to prove rollback behavior."
    category = "filesystem"
    risk = RiskLevel.WRITE
    parameters = (
        ParameterSpec("path", "string", required=True),
        ParameterSpec("content", "string", required=True),
    )

    async def run(self, ctx: ToolContext, path: str = "", content: str = "") -> ToolResult:
        await asyncio.sleep(30)
        (ctx.project_root / path).write_text(content, encoding="utf-8")
        return ToolResult.success("written")


@pytest.fixture
def mutation_environment(project: Path) -> tuple[MutationQueue, ToolExecutor, Path]:
    loaded = load_settings(cwd=project)
    loaded.paths.ensure_project_dirs()
    context = ToolContext(
        settings=loaded.settings,
        paths=loaded.paths,
        project_root=project,
        events=EventBus(),
        run_id="mutation-test",
        backups_dir=loaded.paths.backups_dir,
    )
    registry = ToolRegistry()
    for tool in [
        *fs_tools.build_tools(),
        *mutation_tools.build_tools(),
        SlowWriteTool(),
        AssertContentTool(),
    ]:
        registry.register(tool, source="test")
    policy = PolicyEngine(loaded.settings, project_root=project)
    executor = ToolExecutor(registry, loaded.settings, context, policy)
    queue = MutationQueue(project / ".unknown" / "test-mutations.db", project_root=project)
    context.extra["mutation_queue"] = queue
    yield queue, executor, project
    queue.close()


def stage_write(
    queue: MutationQueue,
    executor: ToolExecutor,
    transaction_id: str,
    path: str,
    content: str,
) -> None:
    queue.stage(
        transaction_id,
        tool_name="write_file",
        arguments={"path": path, "content": content},
        executor=executor,
    )


@pytest.mark.asyncio
async def test_successful_transaction_commits_all_files(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, project = mutation_environment
    transaction = queue.begin(scope="run-1", label="two files")
    stage_write(queue, executor, transaction.id, "alpha.txt", "alpha")
    stage_write(queue, executor, transaction.id, "beta.txt", "beta")

    finished = await queue.commit(transaction.id, executor)

    assert finished.status == "committed"
    assert (project / "alpha.txt").read_text() == "alpha\n"
    assert (project / "beta.txt").read_text() == "beta\n"
    assert [operation.status for operation in finished.operations] == ["applied", "applied"]
    assert queue.verify_events(transaction.id) == []


@pytest.mark.asyncio
async def test_failure_rolls_back_prior_and_existing_files(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, project = mutation_environment
    existing = project / "existing.txt"
    existing.write_text("original\n", encoding="utf-8")
    transaction = queue.begin(label="rollback")
    stage_write(queue, executor, transaction.id, "existing.txt", "changed")
    queue.stage(
        transaction.id,
        tool_name="create_file",
        arguments={"path": "existing.txt", "content": "must fail"},
        executor=executor,
    )

    finished = await queue.commit(transaction.id, executor)

    assert finished.status == "rolled_back"
    assert "already exists" in finished.error
    assert existing.read_text() == "original\n"
    assert finished.operations[0].status == "rolled_back"
    assert finished.operations[1].status == "failed"


@pytest.mark.asyncio
async def test_new_files_are_removed_on_rollback(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, project = mutation_environment
    existing = project / "collision.txt"
    existing.write_text("keep", encoding="utf-8")
    transaction = queue.begin()
    stage_write(queue, executor, transaction.id, "new/deep/file.txt", "temporary")
    queue.stage(
        transaction.id,
        tool_name="create_file",
        arguments={"path": "collision.txt", "content": "fail"},
        executor=executor,
    )
    finished = await queue.commit(transaction.id, executor)
    assert finished.status == "rolled_back"
    assert not (project / "new").exists()
    assert existing.read_text() == "keep"


@pytest.mark.asyncio
async def test_failed_validation_gate_rolls_back_mutations(
    mutation_environment: tuple[MutationQueue, ToolExecutor, Path],
) -> None:
    queue, executor, project = mutation_environment
    target = project / "validated.txt"
    target.write_text("before\n", encoding="utf-8")
    transaction = queue.begin(label="verify before consolidate")
    stage_write(queue, executor, transaction.id, "validated.txt", "after")
    queue.stage(
        transaction.id,
        tool_name="assert_content",
        arguments={"path": "validated.txt", "expected": "impossible"},
        executor=executor,
    )

    finished = await queue.commit(transaction.id, executor)

    assert finished.status == "rolled_back"
    assert "expected 'impossible'" in finished.error
    assert target.read_text() == "before\n"


@pytest.mark.asyncio
async def test_precondition_drift_blocks_commit_before_side_effects(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, project = mutation_environment
    target = project / "src" / "demo" / "calc.py"
    original = target.read_text()
    transaction = queue.begin()
    stage_write(queue, executor, transaction.id, "src/demo/calc.py", "transaction")
    target.write_text("external change\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="precondition changed"):
        await queue.commit(transaction.id, executor)

    assert target.read_text() == "external change\n"
    assert queue.get(transaction.id).status == "open"
    assert original != target.read_text()


def test_policy_is_checked_while_staging(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, _project = mutation_environment
    executor.settings.project.protected_paths = ["protected.txt"]
    transaction = queue.begin()
    with pytest.raises(PermissionError, match="protected"):
        stage_write(queue, executor, transaction.id, "protected.txt", "denied")
    assert queue.get(transaction.id).operations == []


def test_read_and_non_path_tools_are_rejected(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, _project = mutation_environment
    transaction = queue.begin()
    with pytest.raises(ValueError, match="neither a reversible mutation nor a validation gate"):
        queue.stage(
            transaction.id,
            tool_name="read_file",
            arguments={"path": "pyproject.toml"},
            executor=executor,
        )


@pytest.mark.asyncio
async def test_commit_is_idempotent(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, project = mutation_environment
    transaction = queue.begin()
    stage_write(queue, executor, transaction.id, "once.txt", "once")
    first = await queue.commit(transaction.id, executor)
    first_mtime = (project / "once.txt").stat().st_mtime_ns
    second = await queue.commit(transaction.id, executor)
    assert first.status == second.status == "committed"
    assert (project / "once.txt").stat().st_mtime_ns == first_mtime


@pytest.mark.asyncio
async def test_active_commit_cancels_and_rolls_back(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, project = mutation_environment
    target = project / "slow.txt"
    target.write_text("before", encoding="utf-8")
    transaction = queue.begin()
    queue.stage(
        transaction.id,
        tool_name="slow_write",
        arguments={"path": "slow.txt", "content": "after"},
        executor=executor,
    )
    task = asyncio.create_task(queue.commit(transaction.id, executor))
    for _ in range(100):
        if queue.get(transaction.id).status == "applying":
            break
        await asyncio.sleep(0.01)
    queue.cancel(transaction.id)
    finished = await task
    assert finished.status == "cancelled"
    assert target.read_text() == "before"


def test_interrupted_transaction_is_recovered_from_snapshot(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, executor, project = mutation_environment
    target = project / "recover.txt"
    target.write_text("before", encoding="utf-8")
    transaction = queue.begin()
    stage_write(queue, executor, transaction.id, "recover.txt", "after")
    prepared = queue.get(transaction.id)
    queue._snapshot(prepared)
    queue._set_status(transaction.id, "applying")
    target.write_text("partial", encoding="utf-8")

    recovered = queue.recover()

    assert [item.id for item in recovered] == [transaction.id]
    assert recovered[0].status == "rolled_back"
    assert target.read_text() == "before"


def test_event_chain_detects_tampering(mutation_environment: tuple[MutationQueue, ToolExecutor, Path]) -> None:
    queue, _executor, _project = mutation_environment
    transaction = queue.begin()
    queue.connection.execute(
        "UPDATE mutation_events SET payload_json='{}' WHERE transaction_id=? AND sequence=1",
        (transaction.id,),
    )
    queue.connection.commit()
    assert any("content hash mismatch" in item for item in queue.verify_events(transaction.id))


@pytest.mark.asyncio
async def test_agent_tools_drive_transaction_end_to_end(
    mutation_environment: tuple[MutationQueue, ToolExecutor, Path],
) -> None:
    _queue_instance, executor, project = mutation_environment
    opened = await executor.execute(ToolCall.new("mutation_begin", {"label": "agent batch"}))
    assert opened.ok
    transaction_id = opened.data["transaction"]["id"]
    staged = await executor.execute(
        ToolCall.new(
            "mutation_stage",
            {
                "transaction_id": transaction_id,
                "tool": "write_file",
                "arguments": {"path": "agent.txt", "content": "from agent"},
            },
        )
    )
    assert staged.ok, staged.error
    committed = await executor.execute(
        ToolCall.new("mutation_commit", {"transaction_id": transaction_id})
    )
    assert committed.ok, committed.error
    assert (project / "agent.txt").read_text() == "from agent\n"


def test_all_transaction_tools_are_registered(project: Path) -> None:
    loaded = load_settings(cwd=project)
    from unknown_agent.tools.factory import build_default_registry

    registry = build_default_registry(loaded.settings, include_browser=False)
    expected = {
        "mutation_begin",
        "mutation_stage",
        "mutation_preview",
        "mutation_commit",
        "mutation_cancel",
        "mutation_list",
        "mutation_recover",
    }
    assert expected <= set(registry.names())
