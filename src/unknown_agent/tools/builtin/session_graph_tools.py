"""Agent tools for durable session timelines and alternative branches."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.memory.session_graph import BranchConflict, ConcurrentAppend, SessionGraphStore
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext


def _db(ctx: ToolContext) -> Path:
    return ctx.project_root / ".unknown" / "memory" / "session_graph.db"


def _session(ctx: ToolContext, supplied: str) -> str:
    return supplied.strip() or ctx.session_id or ctx.run_id


def _branch(store: SessionGraphStore, session_id: str, identifier: str) -> str:
    branch = store.branch(identifier, session_id=session_id) or store.branch(identifier)
    if branch is None:
        raise KeyError(f"no branch {identifier!r} in session {session_id!r}")
    if branch.session_id != session_id:
        raise ValueError("branch belongs to a different session")
    return branch.id


def _failure(exc: Exception) -> ToolResult:
    return ToolResult.failure(str(exc))


class SessionGraphStartTool(Tool):
    name = "session_graph_start"
    description = "Create the main event-sourced timeline for a durable session."
    category = "memory"
    risk = RiskLevel.WRITE
    tags = ("session", "branch", "durable")
    parameters = (
        ParameterSpec("session_id", "string", "Defaults to the active session/run id.", default=""),
        ParameterSpec("name", "string", "Initial branch name.", default="main"),
    )

    async def run(self, ctx: ToolContext, session_id: str = "", name: str = "main") -> ToolResult:
        resolved = _session(ctx, session_id)
        if not resolved:
            return ToolResult.failure("no active session id; provide session_id")
        try:
            with SessionGraphStore(_db(ctx)) as store:
                branch = store.create_session(resolved, branch_name=name)
            return ToolResult.success(
                f"created session {resolved} branch {name} ({branch.id})", branch=branch.to_dict()
            )
        except (sqlite3.Error, ValueError) as exc:
            return _failure(exc)


class SessionEventAppendTool(Tool):
    name = "session_event_append"
    description = "Append a typed event to a branch with optional optimistic head protection."
    category = "memory"
    risk = RiskLevel.WRITE
    tags = ("session", "event", "durable")
    parameters = (
        ParameterSpec("branch", "string", "Branch id or name.", required=True),
        ParameterSpec("kind", "string", "Event kind, e.g. message.added or value.set.", required=True),
        ParameterSpec("payload", "object", "Structured event payload.", required=True),
        ParameterSpec("role", "string", "Optional actor role.", default=""),
        ParameterSpec("session_id", "string", "Defaults to the active session.", default=""),
        ParameterSpec("expected_head", "integer", "Reject if the branch moved; omit to append latest.", default=None),
    )

    async def run(
        self,
        ctx: ToolContext,
        branch: str = "",
        kind: str = "",
        payload: dict[str, Any] | None = None,
        role: str = "",
        session_id: str = "",
        expected_head: int | None = None,
    ) -> ToolResult:
        resolved = _session(ctx, session_id)
        try:
            with SessionGraphStore(_db(ctx)) as store:
                branch_id = _branch(store, resolved, branch)
                event = store.append(
                    branch_id,
                    kind,
                    dict(payload or {}),
                    role=role,
                    expected_head=expected_head,
                )
            return ToolResult.success(
                f"appended event {event.id} ({event.kind}) to {branch}", event=event.to_dict()
            )
        except (sqlite3.Error, KeyError, ValueError, ConcurrentAppend) as exc:
            return _failure(exc)


class SessionBranchForkTool(Tool):
    name = "session_branch_fork"
    description = "Fork a named alternative from the current or an earlier event without copying history."
    category = "memory"
    risk = RiskLevel.WRITE
    tags = ("session", "branch", "fork")
    parameters = (
        ParameterSpec("parent", "string", "Parent branch id or name.", required=True),
        ParameterSpec("name", "string", "Unique name for the alternative.", required=True),
        ParameterSpec("at_event_id", "integer", "Fork from this visible event; default parent head.", default=None),
        ParameterSpec("session_id", "string", "Defaults to the active session.", default=""),
    )

    async def run(
        self,
        ctx: ToolContext,
        parent: str = "",
        name: str = "",
        at_event_id: int | None = None,
        session_id: str = "",
    ) -> ToolResult:
        resolved = _session(ctx, session_id)
        try:
            with SessionGraphStore(_db(ctx)) as store:
                parent_id = _branch(store, resolved, parent)
                child = store.fork(parent_id, name, at_event_id=at_event_id)
            return ToolResult.success(
                f"forked {name} from {parent} at event {child.fork_event_id}", branch=child.to_dict()
            )
        except (sqlite3.Error, KeyError, ValueError) as exc:
            return _failure(exc)


class SessionBranchListTool(Tool):
    name = "session_branch_list"
    description = "List named alternatives, ancestry, heads and merge status for a session."
    category = "memory"
    risk = RiskLevel.READ
    tags = ("session", "branch")
    parameters = (
        ParameterSpec("session_id", "string", "Defaults to the active session.", default=""),
    )

    async def run(self, ctx: ToolContext, session_id: str = "") -> ToolResult:
        resolved = _session(ctx, session_id)
        try:
            with SessionGraphStore(_db(ctx)) as store:
                branches = store.branches(resolved)
            return ToolResult.success(
                json.dumps([item.to_dict() for item in branches], indent=2),
                branches=[item.to_dict() for item in branches],
            )
        except sqlite3.Error as exc:
            return _failure(exc)


class SessionTimelineTool(Tool):
    name = "session_timeline"
    description = "Read the inherited plus local event timeline and its materialized state."
    category = "memory"
    risk = RiskLevel.READ
    tags = ("session", "branch", "replay")
    parameters = (
        ParameterSpec("branch", "string", "Branch id or name.", required=True),
        ParameterSpec("session_id", "string", "Defaults to the active session.", default=""),
    )

    async def run(self, ctx: ToolContext, branch: str = "", session_id: str = "") -> ToolResult:
        resolved = _session(ctx, session_id)
        try:
            with SessionGraphStore(_db(ctx)) as store:
                branch_id = _branch(store, resolved, branch)
                timeline = [event.to_dict() for event in store.timeline(branch_id)]
                state = store.materialize(branch_id)
                integrity = store.verify(branch_id)
            payload = {"events": timeline, "state": state, "integrity_errors": integrity}
            return ToolResult.success(
                json.dumps(payload, indent=2)[:16000],
                events=timeline,
                state=state,
                integrity_errors=integrity,
            )
        except (sqlite3.Error, KeyError, ValueError) as exc:
            return _failure(exc)


class SessionBranchMergePreviewTool(Tool):
    name = "session_branch_merge_preview"
    description = "Detect semantic value/artifact conflicts before merging a fork."
    category = "memory"
    risk = RiskLevel.READ
    tags = ("session", "branch", "merge")
    parameters = (
        ParameterSpec("source", "string", "Fork branch id or name.", required=True),
        ParameterSpec("target", "string", "Parent branch id or name.", default="main"),
        ParameterSpec("session_id", "string", "Defaults to the active session.", default=""),
    )

    async def run(
        self, ctx: ToolContext, source: str = "", target: str = "main", session_id: str = ""
    ) -> ToolResult:
        resolved = _session(ctx, session_id)
        try:
            with SessionGraphStore(_db(ctx)) as store:
                source_id = _branch(store, resolved, source)
                target_id = _branch(store, resolved, target)
                preview = store.preview_merge(source_id, target_id)
            return ToolResult.success(json.dumps(preview.to_dict(), indent=2), preview=preview.to_dict())
        except (sqlite3.Error, KeyError, ValueError) as exc:
            return _failure(exc)


class SessionBranchMergeTool(Tool):
    name = "session_branch_merge"
    description = "Merge a conflict-free fork by replaying events with cryptographic provenance."
    category = "memory"
    risk = RiskLevel.WRITE
    tags = ("session", "branch", "merge")
    parameters = SessionBranchMergePreviewTool.parameters

    async def run(
        self, ctx: ToolContext, source: str = "", target: str = "main", session_id: str = ""
    ) -> ToolResult:
        resolved = _session(ctx, session_id)
        try:
            with SessionGraphStore(_db(ctx)) as store:
                source_id = _branch(store, resolved, source)
                target_id = _branch(store, resolved, target)
                events = store.merge(source_id, target_id)
            return ToolResult.success(
                f"merged {source} into {target}: {len(events)} event(s) replayed",
                events=[event.to_dict() for event in events],
            )
        except (sqlite3.Error, KeyError, ValueError, BranchConflict, ConcurrentAppend) as exc:
            return _failure(exc)


def build_tools() -> list[Tool]:
    return [
        SessionGraphStartTool(),
        SessionEventAppendTool(),
        SessionBranchForkTool(),
        SessionBranchListTool(),
        SessionTimelineTool(),
        SessionBranchMergePreviewTool(),
        SessionBranchMergeTool(),
    ]
