"""Agent core: models, planner, execution loop, runner.

The package re-exports its public surface **lazily** (PEP 562).  Why that matters:
``unknown_agent.core.models`` is imported by the provider layer, and a package
``__init__`` runs before any submodule, so eager re-exports here created an import
cycle (``providers.base`` -> ``core.models`` -> ``core.__init__`` -> ``planner`` ->
``providers.base``) that only worked when the CLI happened to import ``core`` first.
Lazy attributes keep ``from unknown_agent.core import Runner`` working while making
``import unknown_agent.providers`` in a fresh interpreter safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

_MODELS = {
    "AgentDecision",
    "GateResult",
    "GateStatus",
    "Message",
    "Plan",
    "PlanStep",
    "RiskLevel",
    "RunBudget",
    "RunState",
    "RunStatus",
    "StepRecord",
    "StepStatus",
    "ToolCall",
    "ToolResult",
    "VerificationReport",
}
_PLANNER = {"Planner", "PlannerResult"}
_RUNNER = {"RunRequest", "RunResult", "Runner", "run_task"}

__all__ = sorted(_MODELS | _PLANNER | _RUNNER)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from unknown_agent.core.models import (
        AgentDecision,
        GateResult,
        GateStatus,
        Message,
        Plan,
        PlanStep,
        RiskLevel,
        RunBudget,
        RunState,
        RunStatus,
        StepRecord,
        StepStatus,
        ToolCall,
        ToolResult,
        VerificationReport,
    )
    from unknown_agent.core.planner import Planner, PlannerResult
    from unknown_agent.core.runner import RunRequest, RunResult, Runner, run_task


def __getattr__(name: str) -> Any:
    if name in _MODELS:
        from unknown_agent.core import models

        return getattr(models, name)
    if name in _PLANNER:
        from unknown_agent.core import planner

        return getattr(planner, name)
    if name in _RUNNER:
        from unknown_agent.core import runner

        return getattr(runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return __all__
