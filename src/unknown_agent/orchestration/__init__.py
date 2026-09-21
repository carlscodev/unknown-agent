"""Orchestration: several specialists instead of one generalist.

A single agent asked to "build the checkout, make it look premium, and ship it"
has to hold three different disciplines in one context, and it will do the last
thing it was told best.  Orchestration splits the work into roles with different
missions, different tool grants and different definitions of done — then merges
the results and proves them.

The pieces:

:mod:`~unknown_agent.orchestration.roles`
    Ten roles: planner, repo inspector, code generator, refactorer, UI/UX
    finisher, browser validator, verifier, documenter, releaser, tooling.  Each
    declares its mission, its outputs, the tools it may use, the tools it may
    *not*, whether it mutates the tree, its reasoning depth, its stage, the paths
    it owns, and the gates that verify it.

:mod:`~unknown_agent.orchestration.worker`
    The execution unit and its state machine (``pending → queued → running →
    succeeded | failed → blocked | cancelled``).  A worker's tool subset is the
    *intersection* of its role's grant and the session registry — a role can never
    grant itself a tool the session does not have.  Executors are injected, which
    is what makes the whole layer testable offline
    (:class:`~unknown_agent.orchestration.worker.OfflineExecutor`).

:mod:`~unknown_agent.orchestration.conductor`
    ``analyze_task`` scores a request deterministically and explainably;
    :class:`~unknown_agent.orchestration.conductor.Router` turns that score into a
    shape (``solo``, ``pipeline``, ``fanout``, ``graph``, ``escalate``) and a work
    graph; :class:`~unknown_agent.orchestration.conductor.Conductor` runs the graph
    in batches, retries with failure context, escalates to the planner when a
    blocker is structural, verifies each worker's gates, and respects a budget.

Two invariants the conductor enforces, because getting them wrong is what makes
multi-agent systems produce garbage:

* **Ownership.**  Two mutating workers whose ``owned_paths`` overlap are never
  scheduled concurrently — the overlap becomes a dependency.  And a merge that
  finds two roles claiming the same file is a *failure*, not a coin flip.
* **Budget.**  Worker count, step count, token count and wall clock are checked
  before every dispatch.  A plan that cannot fit the budget is trimmed at routing
  time (optional roles first), so work is never cancelled halfway.

Every dispatch, transition, retry and merge decision is appended to a JSONL
ledger (:mod:`~unknown_agent.orchestration.ledger`) at
``.unknown/orchestration/<run>.jsonl``.  "Why did the agent do that?" has an
answer on disk.
"""

from __future__ import annotations

from unknown_agent.orchestration.executor import AgentExecutor, deliverables_instruction, parse_deliverables
from unknown_agent.orchestration.conductor import (
    DEFAULT_MILESTONES,
    Budget,
    Conductor,
    MergeOutcome,
    OrchestrationOutcome,
    Router,
    TaskAnalysis,
    WorkGraph,
    analyze_task,
    conflicting_pairs,
    merge_results,
    orchestrate,
)
from unknown_agent.orchestration.ledger import (
    EVENT_KINDS,
    Ledger,
    LedgerEvent,
    ledger_path_for,
    list_ledgers,
    read_ledger,
    timeline,
)
from unknown_agent.orchestration.roles import (
    ROLES,
    ROLE_ALIASES,
    ROLE_ORDER,
    Role,
    get_role,
    has_role,
    resolve_role_id,
    roles_for_pattern,
    roles_payload,
)
from unknown_agent.orchestration.worker import (
    LEGAL_TRANSITIONS,
    CallableExecutor,
    OfflineExecutor,
    OrchestrationError,
    WorkExecutor,
    WorkItem,
    WorkResult,
    Worker,
    WorkerContext,
    WorkState,
)

__all__ = [
    "AgentExecutor",
    "DEFAULT_MILESTONES",
    "deliverables_instruction",
    "parse_deliverables",
    # conductor
    "Budget",
    "Conductor",
    "MergeOutcome",
    "OrchestrationOutcome",
    "Router",
    "TaskAnalysis",
    "WorkGraph",
    "analyze_task",
    "conflicting_pairs",
    "merge_results",
    "orchestrate",
    # ledger
    "EVENT_KINDS",
    "Ledger",
    "LedgerEvent",
    "ledger_path_for",
    "list_ledgers",
    "read_ledger",
    "timeline",
    # roles
    "ROLES",
    "ROLE_ALIASES",
    "ROLE_ORDER",
    "Role",
    "get_role",
    "has_role",
    "resolve_role_id",
    "roles_for_pattern",
    "roles_payload",
    # worker
    "LEGAL_TRANSITIONS",
    "CallableExecutor",
    "OfflineExecutor",
    "OrchestrationError",
    "WorkExecutor",
    "WorkItem",
    "WorkResult",
    "Worker",
    "WorkerContext",
    "WorkState",
]
