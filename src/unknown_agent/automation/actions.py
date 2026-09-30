"""Production actions that bridge automation into the existing agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from unknown_agent.automation.scheduler import ActionResult, ExecutionContext, Scheduler
from unknown_agent.config.loader import LoadedSettings
from unknown_agent.core.runner import RunRequest, run_task


class AgentRunPayload(BaseModel):
    """Strict payload accepted by the built-in ``agent.run`` action."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=100_000)
    kind: Literal["run", "build", "fix", "ask", "plan"] = "run"
    mode: Literal["auto", "plan", "react"] = "auto"
    read_only: bool = False
    dry_run: bool = False
    model: str | None = None
    provider: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=1000)
    definition_of_done: str | None = Field(default=None, max_length=20_000)
    gates: list[str] | None = None
    tools: list[str] | None = None
    orchestrate: bool | None = None
    extra_instructions: str | None = Field(default=None, max_length=100_000)


def register_agent_action(
    scheduler: Scheduler,
    loaded_factory: Callable[[], LoadedSettings],
) -> None:
    """Register long-horizon mission execution on a scheduler instance."""

    async def execute(context: ExecutionContext) -> ActionResult:
        payload = AgentRunPayload.model_validate(context.payload)
        request = RunRequest(
            **payload.model_dump(),
            yes=True,
            non_interactive=True,
            metadata={
                "scheduler_job_id": context.job_id,
                "scheduler_execution_id": context.execution_id,
                "scheduler_occurrence_key": context.occurrence_key,
            },
        )
        result = await run_task(loaded_factory(), request)
        return ActionResult(
            ok=result.ok,
            message=result.summary,
            output={
                "run_id": result.run_id,
                "status": result.status.value,
                "run_dir": str(result.run_dir),
                "files_changed": result.files_changed,
                "blockers": result.blockers,
            },
        )

    scheduler.register_action("agent.run", execute, source="unknown-agent:built-in")
