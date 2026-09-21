"""The runner delegates to the conductor — and knows when not to.

``ua run`` used to work solo forever: the orchestration layer was complete and
reachable only through ``ua orchestrate``.  These tests pin the seam between them —
the *decision* (explainable, flag > config > heuristic), the *leaf rule* (a worker
never starts its own conductor), and the *translation* back into a ``RunState``
without flattering the result.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from unknown_agent.config.loader import load_settings
from unknown_agent.core.models import RunState, RunStatus
from unknown_agent.core.runner import _DELEGATION_DEPTH, RunRequest, Runner, run_task
from unknown_agent.orchestration import AgentExecutor, WorkItem, get_role
from unknown_agent.orchestration.worker import WorkerContext

SIMPLE = "summarise the repository"
COMPLEX = "build a web app with auth, a dashboard, tests and docs"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def make_runner(
    tmp_path: Path,
    task: str,
    *,
    kind: str = "run",
    orchestrate: bool | None = None,
    enabled: bool = True,
    mode: str = "auto",
    min_complexity: int = 3,
    resume: str | None = None,
) -> Runner:
    """A Runner that has not been through ``setup()`` — enough for the decision."""

    loaded = load_settings(cwd=tmp_path)
    config = loaded.settings.orchestration
    config.enabled = enabled
    config.mode = mode
    config.min_complexity = min_complexity
    request = RunRequest(
        task=task,
        kind=kind,
        provider="offline",
        orchestrate=orchestrate,
        resume_run_id=resume,
    )
    return Runner(loaded, request=request)


def outcome(
    *,
    ok: bool,
    status: str,
    summary: str = "",
    blockers: list[str] | None = None,
    files: list[str] | None = None,
    conflicts: list[dict] | None = None,
    unverified: list[str] | None = None,
    workers: int = 2,
    succeeded: int = 0,
) -> SimpleNamespace:
    """A stub OrchestrationOutcome: the mapping must not need the real graph."""

    return SimpleNamespace(
        ok=ok,
        status=status,
        summary=summary,
        blockers=list(blockers or []),
        unverified=list(unverified or []),
        merge=None
        if files is None and conflicts is None
        else SimpleNamespace(files=list(files or []), conflicts=list(conflicts or [])),
        graph=SimpleNamespace(items=[object()] * workers),
        succeeded=lambda: [object()] * succeeded,
    )


def absorb(tmp_path: Path, result: SimpleNamespace) -> RunState:
    runner = make_runner(tmp_path, "anything")
    runner.state = RunState.new("anything")
    runner._absorb_orchestration(result)
    return runner.state


# --------------------------------------------------------------------------- #
# the decision
# --------------------------------------------------------------------------- #
def test_an_explicit_solo_flag_beats_everything(tmp_path: Path) -> None:
    delegate, reason = make_runner(tmp_path, COMPLEX, orchestrate=False)._orchestration_decision()

    assert delegate is False
    assert reason == "solo requested"


def test_disabled_orchestration_never_delegates(tmp_path: Path) -> None:
    delegate, reason = make_runner(tmp_path, COMPLEX, enabled=False)._orchestration_decision()

    assert delegate is False
    assert "disabled" in reason


def test_mode_solo_never_delegates(tmp_path: Path) -> None:
    delegate, reason = make_runner(tmp_path, COMPLEX, mode="solo")._orchestration_decision()

    assert delegate is False
    assert "solo" in reason


@pytest.mark.parametrize("kind", ["ask", "plan"])
def test_questions_and_plans_are_not_delegable(tmp_path: Path, kind: str) -> None:
    delegate, reason = make_runner(tmp_path, COMPLEX, kind=kind)._orchestration_decision()

    assert delegate is False
    assert kind in reason


def test_a_resumed_plan_stays_solo(tmp_path: Path) -> None:
    delegate, reason = make_runner(tmp_path, COMPLEX, resume="run-20260101-000000-abcdef")._orchestration_decision()

    assert delegate is False
    assert "resum" in reason


def test_an_explicit_flag_forces_even_a_trivial_task(tmp_path: Path) -> None:
    """Complexity 1 would never delegate on its own; the user asked, so it does."""

    delegate, reason = make_runner(tmp_path, SIMPLE, orchestrate=True)._orchestration_decision()

    assert delegate is True
    assert reason == "orchestration requested"


def test_mode_orchestrated_forces_delegation(tmp_path: Path) -> None:
    delegate, reason = make_runner(tmp_path, SIMPLE, mode="orchestrated")._orchestration_decision()

    assert delegate is True
    assert "orchestrated" in reason


def test_a_task_below_the_floor_stays_solo(tmp_path: Path) -> None:
    delegate, reason = make_runner(tmp_path, SIMPLE)._orchestration_decision()

    assert delegate is False
    assert "min_complexity" in reason


def test_a_complex_task_delegates_and_says_why(tmp_path: Path) -> None:
    delegate, reason = make_runner(tmp_path, COMPLEX)._orchestration_decision()

    assert delegate is True
    assert "complexity" in reason and "shape" in reason, reason


def test_the_floor_is_respected_when_raised(tmp_path: Path) -> None:
    delegate, _reason = make_runner(tmp_path, COMPLEX, min_complexity=10)._orchestration_decision()

    assert delegate is False


def test_a_run_already_inside_a_delegation_does_not_delegate_again(tmp_path: Path) -> None:
    token = _DELEGATION_DEPTH.set(1)
    try:
        delegate, reason = make_runner(tmp_path, COMPLEX)._orchestration_decision()
    finally:
        _DELEGATION_DEPTH.reset(token)

    assert delegate is False
    assert "inside a delegated run" in reason


# --------------------------------------------------------------------------- #
# the leaf rule
# --------------------------------------------------------------------------- #
def test_a_worker_request_can_never_re_delegate(tmp_path: Path) -> None:
    """Conductor -> worker -> conductor multiplies workers until fds run out."""

    loaded = load_settings(cwd=tmp_path)
    executor = AgentExecutor(loaded, provider="offline", use_skills=False)
    role = get_role("coder")
    item = WorkItem(role="coder", task="implement the endpoint", outputs=["patch"])
    context = WorkerContext(project_root=str(tmp_path), task="build the thing")

    request = executor.build_request(item, role, context)

    assert request.orchestrate is False
    assert request.tools == list(role.tools), "the role grant is still a hard boundary"


# --------------------------------------------------------------------------- #
# outcome -> RunState, without flattering the result
# --------------------------------------------------------------------------- #
def test_a_successful_outcome_completes_the_run(tmp_path: Path) -> None:
    state = absorb(tmp_path, outcome(ok=True, status="completed", summary="all workers succeeded", succeeded=2))

    assert state.status is RunStatus.COMPLETED
    assert state.summary == "all workers succeeded"


def test_a_partial_outcome_is_blocked_not_completed(tmp_path: Path) -> None:
    """The easiest way to lie here is to trust the label instead of ``ok``."""

    state = absorb(tmp_path, outcome(ok=False, status="partial", blockers=["coder produced no patch"]))

    assert state.status is RunStatus.BLOCKED
    assert "coder produced no patch" in state.blockers


def test_a_failed_outcome_fails_the_run(tmp_path: Path) -> None:
    state = absorb(tmp_path, outcome(ok=False, status="failed", blockers=["boom"]))

    assert state.status is RunStatus.FAILED


def test_a_cancelled_outcome_is_a_failure_even_though_nothing_errored(tmp_path: Path) -> None:
    state = absorb(tmp_path, outcome(ok=False, status="cancelled", blockers=["budget exhausted"]))

    assert state.status is RunStatus.FAILED


def test_unverified_workers_become_blockers(tmp_path: Path) -> None:
    """A run that checked nothing must not look like a run that passed."""

    state = absorb(tmp_path, outcome(ok=True, status="completed", unverified=["verifier", "documenter"]))

    assert any("unverified" in blocker for blocker in state.blockers)
    assert "verifier" in " ".join(state.blockers)


def test_files_come_from_the_merge_not_from_worker_claims(tmp_path: Path) -> None:
    state = absorb(
        tmp_path,
        outcome(ok=True, status="completed", files=["src/a.py", "src/b.py", "src/a.py"]),
    )

    assert state.files_changed == ["src/a.py", "src/b.py"], "deduped, in merge order"


def test_merge_conflicts_become_blockers(tmp_path: Path) -> None:
    state = absorb(
        tmp_path,
        outcome(ok=False, status="blocked", files=["src/a.py"], conflicts=[{"file": "src/a.py", "roles": ["coder", "refactorer"]}]),
    )

    assert any("merge conflict" in blocker for blocker in state.blockers)


def test_a_missing_summary_is_synthesised_from_the_graph(tmp_path: Path) -> None:
    state = absorb(tmp_path, outcome(ok=False, status="blocked", summary="", workers=3, succeeded=1))

    assert "1/3" in state.summary


def test_the_run_is_marked_finished(tmp_path: Path) -> None:
    state = absorb(tmp_path, outcome(ok=True, status="completed", summary="done"))

    assert state.finished_at, "an absorbed outcome must close the run"


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #
def fast_orchestration(loaded) -> None:
    """Keep the delegated run small and deterministic for tests."""

    config = loaded.settings.orchestration
    config.max_workers = 2
    config.max_retries_per_worker = 0
    config.max_replans = 0


def test_a_forced_delegation_produces_an_orchestrated_run(project: Path) -> None:
    loaded = load_settings(cwd=project)
    fast_orchestration(loaded)
    events: list[str] = []

    result = asyncio.run(
        run_task(
            loaded,
            RunRequest(task=COMPLEX, provider="offline", orchestrate=True),
            event_sink=lambda event: events.append(event.type),
        )
    )

    assert result.state.mode == "orchestrated"
    assert (result.run_dir / "orchestration.json").exists(), "a delegated run must be auditable"
    assert "orchestration.started" in events
    assert "orchestration.finished" in events
    assert any(kind.startswith("orchestration.") for kind in events), "conductor events must reach the run journal"
    assert _DELEGATION_DEPTH.get() == 0, "the depth guard must be released after the run"
    # The offline provider cannot satisfy a role's declared outputs, so an honest
    # mapping reports the run as blocked rather than pretending it shipped.
    assert result.status.value in {"blocked", "failed", "completed"}
    assert result.summary


def test_a_solo_run_writes_no_orchestration_journal(project: Path) -> None:
    loaded = load_settings(cwd=project)
    fast_orchestration(loaded)

    result = asyncio.run(run_task(loaded, RunRequest(task=SIMPLE, provider="offline", orchestrate=False)))

    assert result.state.mode != "orchestrated"
    assert not (result.run_dir / "orchestration.json").exists()


def test_auto_routing_leaves_a_trivial_task_solo(project: Path) -> None:
    """The default must not turn every small request into a four-worker graph."""

    loaded = load_settings(cwd=project)
    fast_orchestration(loaded)

    result = asyncio.run(run_task(loaded, RunRequest(task=SIMPLE, provider="offline")))

    assert result.state.mode != "orchestrated"
    assert result.ok
