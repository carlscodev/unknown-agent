"""Memory tools exposed to the agent: recall, remember, forget, procedures."""

from __future__ import annotations

from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext


class MemorySearchTool(Tool):
    name = "memory_search"
    description = (
        "Search long-term project memory (conventions, architecture notes, past decisions, known "
        "gotchas) and prior runs. Call this before making assumptions about how the project works."
    )
    category = "memory"
    risk = RiskLevel.READ
    tags = ("memory", "primary")
    parameters = (
        ParameterSpec("query", "string", "Natural-language or keyword query.", required=True),
        ParameterSpec("limit", "integer", "Maximum facts to return.", default=10, minimum=1, maximum=50),
        ParameterSpec("kind", "string", "Restrict to a fact kind.", default=""),
    )

    async def run(self, ctx: ToolContext, query: str, limit: int = 10, kind: str = "") -> ToolResult:
        memory = ctx.memory
        if memory is None:
            return ToolResult.success("Memory store unavailable; no facts recalled.", facts=[])
        facts = memory.search(query, limit=limit, kind=kind or None)
        if not facts:
            return ToolResult.success(f"No stored facts match {query!r}.", facts=[])
        lines = [f"#{fact['id']} [{fact['kind']}] {fact['text']}" for fact in facts]
        ctx.emit("memory.recall", f"recalled {len(facts)} fact(s) for {query[:60]!r}", count=len(facts))
        return ToolResult.success("\n".join(lines), facts=facts, count=len(facts))


class MemoryForgetTool(Tool):
    name = "memory_forget"
    description = "Delete a stored fact by id. Use when a remembered convention turned out to be wrong."
    category = "memory"
    risk = RiskLevel.DESTRUCTIVE
    parameters = (ParameterSpec("fact_id", "integer", "Fact id from memory_search.", required=True, minimum=1),)

    async def run(self, ctx: ToolContext, fact_id: int) -> ToolResult:
        memory = ctx.memory
        if memory is None:
            return ToolResult.failure("Memory store unavailable.")
        removed = memory.forget(fact_id)
        return ToolResult.success(f"forgot fact #{fact_id}") if removed else ToolResult.failure(f"No fact with id {fact_id}.")


class ProcedureListTool(Tool):
    name = "procedure_list"
    description = "List stored procedural memory: reusable workflows the agent applies to recurring tasks."
    category = "memory"
    risk = RiskLevel.READ
    parameters = (ParameterSpec("contains", "string", "Filter by substring in name or body.", default=""),)

    async def run(self, ctx: ToolContext, contains: str = "") -> ToolResult:
        from unknown_agent.memory.procedures import ProcedureLibrary

        library = ProcedureLibrary(ctx.paths.procedures_dir, ctx.settings.project.conventions)
        procedures = library.list()
        if contains:
            needle = contains.lower()
            procedures = [item for item in procedures if needle in item.name.lower() or needle in item.body.lower()]
        if not procedures:
            return ToolResult.success("No procedures stored.", count=0)
        lines = [f"{item.name}: {' '.join(item.body.split())[:110]}" for item in procedures]
        return ToolResult.success("\n".join(lines), count=len(procedures))


class ProcedureAddTool(Tool):
    name = "procedure_add"
    description = "Store a reusable procedure (workflow) that future runs should follow."
    category = "memory"
    risk = RiskLevel.WRITE
    parameters = (
        ParameterSpec("name", "string", "Short kebab-case name.", required=True),
        ParameterSpec("body", "string", "Markdown body describing the steps and rules.", required=True),
        ParameterSpec("scope", "string", "user | project", default="project", enum=("user", "project")),
    )

    async def run(self, ctx: ToolContext, name: str, body: str, scope: str = "project") -> ToolResult:
        from unknown_agent.memory.procedures import ProcedureLibrary

        target_dir = ctx.paths.procedures_dir if scope == "user" else ctx.project_root / ".unknown" / "procedures"
        library = ProcedureLibrary(target_dir)
        path = library.save(name, body, scope=scope)
        return ToolResult.success(f"stored procedure {name!r} at {path}", path=str(path))


def build_tools() -> list[Tool]:
    return [MemorySearchTool(), MemoryForgetTool(), ProcedureListTool(), ProcedureAddTool()]


__all__ = ["MemorySearchTool", "MemoryForgetTool", "ProcedureListTool", "ProcedureAddTool", "build_tools"]
