"""Durable automation primitives for scheduled and webhook-triggered work."""

from unknown_agent.automation.actions import AgentRunPayload, register_agent_action
from unknown_agent.automation.scheduler import (
    ActionRegistration,
    ActionResult,
    ExecutionContext,
    ExecutionRecord,
    ExecutionStatus,
    Job,
    JobSpec,
    Scheduler,
    TriggerKind,
)

__all__ = [
    "ActionRegistration",
    "ActionResult",
    "AgentRunPayload",
    "ExecutionContext",
    "ExecutionRecord",
    "ExecutionStatus",
    "Job",
    "JobSpec",
    "Scheduler",
    "TriggerKind",
    "register_agent_action",
]
