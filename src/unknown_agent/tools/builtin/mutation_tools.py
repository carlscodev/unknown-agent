"""Agent-facing tools for durable transactional filesystem mutations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext
from unknown_agent.tools.mutations import MutationQueue


def _queue(ctx: ToolContext, *, create: bool = True) -> MutationQueue | None:
    existing = ctx.extra.get("mutation_queue")
    if isinstance(existing, MutationQueue):
        return existing
    path = ctx.project_root / ".unknown" / "mutations.db"
    if not create and not path.exists():
        return None
    queue = MutationQueue(path, project_root=ctx.project_root)
    ctx.extra["mutation_queue"] = queue
    return queue


def _executor(ctx: ToolContext) -> Any:
    executor = ctx.extra.get("tool_executor")
    if executor is None:
        raise RuntimeError("mutation tools require the shared ToolExecutor")
    return executor


def _failure(exc: Exception) -> ToolResult:
    return ToolResult.failure(f"{type(exc).__name__}: {exc}")


class MutationBeginTool(Tool):
    name = "mutation_begin"
    description = "Open a durable transaction for a group of reversible filesystem mutations."
    category = "filesystem"
    risk = RiskLevel.WRITE
    tags = ("transaction", "mutation", "atomic")
    parameters = (
        ParameterSpec("label", "string", "Human-readable purpose of the transaction.", default=""),
        ParameterSpec("scope", "string", "Mission/run scope; defaults to the active run.", default=""),
    )

    async def run(self, ctx: ToolContext, label: str = "", scope: str = "") -> ToolResult:
        try:
            queue = _queue(ctx)
            assert queue is not None
            transaction = queue.begin(scope=scope or ctx.run_id, label=label)
            payload = transaction.model_dump(mode="json")
            ctx.emit("mutation.opened", transaction.id, transaction=payload)
            return ToolResult.success(
                f"opened mutation transaction {transaction.id}", transaction=payload
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            return _failure(exc)


class MutationStageTool(Tool):
    name = "mutation_stage"
    description = (
        "Stage a reversible write/delete or a validation gate. Captures path fingerprints; "
        "a failed validation rolls the complete mutation set back."
    )
    category = "filesystem"
    risk = RiskLevel.WRITE
    tags = ("transaction", "mutation", "atomic")
    parameters = (
        ParameterSpec("transaction_id", "string", "Open mutation transaction id.", required=True),
        ParameterSpec("tool", "string", "Mutating tool name, such as write_file or apply_patch.", required=True),
        ParameterSpec("arguments", "object", "Arguments for the staged tool call.", required=True),
    )

    async def run(
        self,
        ctx: ToolContext,
        transaction_id: str = "",
        tool: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> ToolResult:
        try:
            queue = _queue(ctx)
            assert queue is not None
            operation = queue.stage(
                transaction_id,
                tool_name=tool,
                arguments=arguments or {},
                executor=_executor(ctx),
            )
            payload = operation.model_dump(mode="json")
            ctx.emit(
                "mutation.staged",
                f"{transaction_id} operation {operation.position}: {tool}",
                operation=payload,
            )
            return ToolResult.success(
                f"staged operation {operation.position} ({tool}) in {transaction_id}",
                operation=payload,
            )
        except (KeyError, OSError, PermissionError, sqlite3.Error, ValueError) as exc:
            return _failure(exc)


class MutationPreviewTool(Tool):
    name = "mutation_preview"
    description = "Validate current path fingerprints and inspect all staged operations before commit."
    category = "filesystem"
    risk = RiskLevel.READ
    tags = ("transaction", "mutation", "inspect")
    parameters = (
        ParameterSpec("transaction_id", "string", "Mutation transaction id.", required=True),
    )

    async def run(self, ctx: ToolContext, transaction_id: str = "") -> ToolResult:
        try:
            queue = _queue(ctx, create=False)
            if queue is None:
                return ToolResult.failure("No mutation queue exists for this project.")
            transaction = queue.preview(transaction_id)
            payload = transaction.model_dump(mode="json")
            return ToolResult.success(json.dumps(payload, indent=2)[:16000], transaction=payload)
        except (KeyError, OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            return _failure(exc)


class MutationCommitTool(Tool):
    name = "mutation_commit"
    description = (
        "Preflight and atomically apply every staged mutation. Any failure or cancellation "
        "restores all affected paths from snapshots."
    )
    category = "filesystem"
    risk = RiskLevel.WRITE
    concurrency_safe = False
    tags = ("transaction", "mutation", "atomic", "commit")
    parameters = (
        ParameterSpec("transaction_id", "string", "Open mutation transaction id.", required=True),
    )

    async def run(self, ctx: ToolContext, transaction_id: str = "") -> ToolResult:
        try:
            queue = _queue(ctx, create=False)
            if queue is None:
                return ToolResult.failure("No mutation queue exists for this project.")
            transaction = await queue.commit(transaction_id, _executor(ctx))
            payload = transaction.model_dump(mode="json")
            ctx.emit(
                "mutation.finished",
                f"{transaction.id}: {transaction.status}",
                transaction=payload,
            )
            if transaction.status != "committed":
                return ToolResult.failure(
                    f"transaction {transaction.id} ended as {transaction.status}: {transaction.error}",
                    output=json.dumps(payload, indent=2)[:16000],
                )
            return ToolResult.success(
                f"committed {len(transaction.operations)} operations in {transaction.id}",
                transaction=payload,
            )
        except (KeyError, OSError, PermissionError, RuntimeError, sqlite3.Error, ValueError) as exc:
            return _failure(exc)


class MutationCancelTool(Tool):
    name = "mutation_cancel"
    description = "Cancel an open or applying transaction; active work is cancelled and rolled back."
    category = "filesystem"
    risk = RiskLevel.WRITE
    concurrency_safe = True
    tags = ("transaction", "mutation", "cancel")
    parameters = (
        ParameterSpec("transaction_id", "string", "Mutation transaction id.", required=True),
    )

    async def run(self, ctx: ToolContext, transaction_id: str = "") -> ToolResult:
        try:
            queue = _queue(ctx, create=False)
            if queue is None:
                return ToolResult.failure("No mutation queue exists for this project.")
            transaction = queue.cancel(transaction_id)
            payload = transaction.model_dump(mode="json")
            return ToolResult.success(
                f"transaction {transaction.id}: {transaction.status}", transaction=payload
            )
        except (KeyError, OSError, sqlite3.Error, ValueError) as exc:
            return _failure(exc)


class MutationListTool(Tool):
    name = "mutation_list"
    description = "List durable mutation transactions and their current status."
    category = "filesystem"
    risk = RiskLevel.READ
    tags = ("transaction", "mutation", "inspect")
    parameters = (
        ParameterSpec("scope", "string", "Optional mission/run scope.", default=""),
        ParameterSpec("limit", "integer", "Maximum transactions.", default=50, minimum=1, maximum=1000),
    )

    async def run(self, ctx: ToolContext, scope: str = "", limit: int = 50) -> ToolResult:
        try:
            queue = _queue(ctx, create=False)
            if queue is None:
                return ToolResult.success("No mutation transactions.", transactions=[])
            transactions = [
                item.model_dump(mode="json") for item in queue.transactions(scope=scope, limit=limit)
            ]
            return ToolResult.success(
                json.dumps(transactions, indent=2)[:16000], transactions=transactions
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            return _failure(exc)


class MutationRecoverTool(Tool):
    name = "mutation_recover"
    description = "Recover interrupted commits by restoring their durable snapshots."
    category = "filesystem"
    risk = RiskLevel.WRITE
    concurrency_safe = False
    tags = ("transaction", "mutation", "recovery")
    parameters = ()

    async def run(self, ctx: ToolContext) -> ToolResult:
        try:
            queue = _queue(ctx, create=False)
            if queue is None:
                return ToolResult.success("No mutation queue requires recovery.", recovered=[])
            recovered = [item.model_dump(mode="json") for item in queue.recover()]
            return ToolResult.success(
                f"recovered {len(recovered)} interrupted transaction(s)", recovered=recovered
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            return _failure(exc)


def build_tools() -> list[Tool]:
    return [
        MutationBeginTool(),
        MutationStageTool(),
        MutationPreviewTool(),
        MutationCommitTool(),
        MutationCancelTool(),
        MutationListTool(),
        MutationRecoverTool(),
    ]


__all__ = ["build_tools"]
