"""The production executor: a worker is a bounded agent run.

Everything here runs against a fake ``run_task`` — the point is the *contract*
between orchestration and the core loop: the role's boundaries reach the request,
the run's evidence reaches the result, and nothing is invented on the way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from unknown_agent.core.models import GateResult, GateStatus, RunState, RunStatus, VerificationReport
from unknown_agent.core.runner import RunRequest, RunResult
from unknown_agent.orchestration import (
    AgentExecutor,
    WorkItem,
    deliverables_instruction,
    get_role,
    parse_deliverables,
)
from unknown_agent.orchestration.worker import Worker, WorkerContext

CHECKOUT = "build a checkout flow with cart, stripe payments, responsive UI, docs"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run_result(
    *,
    diff: str = "",
    files: list[str] | None = None,
    summary: str = "",
    gates: list[GateResult] | None = None,
    status: RunStatus = RunStatus.COMPLETED,
    steps: int = 4,
    tokens: int = 1200,
    blockers: list[str] | None = None,
) -> RunResult:
    state = RunState.new("task", cwd=".")
    state.status = status
    state.summary = summary
    state.files_changed = list(files or [])
    state.blockers = list(blockers or [])
    state.budget.steps_used = steps
    state.budget.tokens_used = tokens
    verification = VerificationReport(results=list(gates or [])) if gates is not None else None
    return RunResult(
        state=state,
        outcome=None,
        run_dir=Path("/tmp/run"),
        verification=verification,
        diff=diff,
    )


def _executor(result: RunResult | Exception, **kwargs: Any) -> AgentExecutor:
    """An executor whose runner returns ``result`` (or raises it)."""

    async def fake_runner(loaded: Any, request: RunRequest, *, hooks: Any = None) -> RunResult:
        if isinstance(result, Exception):
            raise result
        return result

    return AgentExecutor(loaded=object(), runner=fake_runner, **kwargs)


def _item(role: str, task: str = CHECKOUT, **kwargs: Any) -> WorkItem:
    entry = get_role(role)
    return WorkItem(id="w1", role=role, task=task, owned_paths=list(entry.owned_paths), **kwargs)


# --------------------------------------------------------------------------- #
# the request the role produces
# --------------------------------------------------------------------------- #
def test_a_read_only_role_gets_a_read_only_run() -> None:
    executor = _executor(_run_result())
    request = executor.build_request(_item("inspector"), get_role("inspector"), WorkerContext())
    assert request.read_only is True


def test_a_mutating_role_may_write() -> None:
    executor = _executor(_run_result())
    request = executor.build_request(_item("coder"), get_role("coder"), WorkerContext())
    assert request.read_only is False


def test_the_role_gates_reach_the_run() -> None:
    executor = _executor(_run_result())
    request = executor.build_request(_item("coder"), get_role("coder"), WorkerContext())
    assert request.gates == ["syntax", "tests"]


def test_extra_gates_are_added_without_duplicates() -> None:
    executor = _executor(_run_result(), extra_gates=("tests", "diff"))
    request = executor.build_request(_item("coder"), get_role("coder"), WorkerContext())
    assert request.gates == ["syntax", "tests", "diff"]


def test_the_step_limit_is_the_tighter_of_role_and_item() -> None:
    executor = _executor(_run_result())
    role = get_role("coder")
    item = _item("coder", max_steps=3)
    request = executor.build_request(item, role, WorkerContext())
    assert request.max_steps == 3
    generous = _item("coder", max_steps=999)
    assert executor.build_request(generous, role, WorkerContext()).max_steps == role.max_steps


def test_the_worker_prompt_carries_the_role_mission_and_boundaries() -> None:
    executor = _executor(_run_result())
    role = get_role("coder")
    request = executor.build_request(_item("coder"), role, WorkerContext())
    assert role.mission in request.task
    assert CHECKOUT in request.task


def test_the_run_is_asked_to_declare_its_deliverables() -> None:
    executor = _executor(_run_result())
    role = get_role("coder")
    request = executor.build_request(_item("coder"), role, WorkerContext())
    assert "<deliverables>" in (request.extra_instructions or "")
    for output in role.outputs:
        assert output in (request.extra_instructions or "")


def test_model_and_provider_overrides_reach_the_run() -> None:
    executor = _executor(_run_result(), model="gpt-x", provider="openrouter")
    request = executor.build_request(_item("coder"), get_role("coder"), WorkerContext())
    assert request.model == "gpt-x"
    assert request.provider == "openrouter"


def test_orchestration_metadata_identifies_the_worker() -> None:
    executor = _executor(_run_result())
    role = get_role("coder")
    request = executor.build_request(_item("coder"), role, WorkerContext())
    meta = request.metadata["orchestration"]
    assert meta["work_id"] == "w1"
    assert meta["role"] == "coder"
    assert meta["reasoning_mode"] == role.reasoning_mode
    assert meta["deny_tools"] == list(role.deny_tools)


def test_the_context_brief_includes_repo_facts_and_other_workers_results() -> None:
    from unknown_agent.orchestration import WorkResult

    executor = _executor(_run_result())
    context = WorkerContext(
        repo_map="src/demo (3 files)",
        conventions=["black", "pytest"],
        policy_summary="writes allowed under src/",
        previous_results={"w0": WorkResult(item_id="w0", role="inspector", ok=True, summary="mapped the repo")},
    )
    request = executor.build_request(_item("coder"), get_role("coder"), context)
    extra = request.extra_instructions or ""
    assert "src/demo" in extra
    assert "black" in extra
    assert "mapped the repo" in extra


def test_a_dry_run_executor_never_applies_writes() -> None:
    executor = _executor(_run_result(), dry_run=True)
    assert executor.build_request(_item("coder"), get_role("coder"), WorkerContext()).dry_run is True


# --------------------------------------------------------------------------- #
# the result the run produces
# --------------------------------------------------------------------------- #
def test_a_diff_becomes_the_patch_artefact() -> None:
    result = _run_result(diff="--- a/src/cart.py\n+++ b/src/cart.py\n+total = 0", files=["src/cart.py"])
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert outcome.ok
    assert "src/cart.py" in outcome.artifacts["patch"]
    assert outcome.files_touched == ["src/cart.py"]


def test_a_coder_who_claimed_a_patch_without_a_diff_is_not_believed() -> None:
    """Evidence beats claims: no diff means no patch artefact."""

    summary = "done\n<deliverables>\npatch: implemented the cart\ntests: all green\nnotes: ok\n</deliverables>"
    result = _run_result(summary=summary, diff="", files=[])
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert "patch" not in outcome.artifacts
    # ...and the worker's own required-output check turns that into a failure.
    item = _item("coder")
    final = run(Worker("coder", executor=_executor(result)).run(item))
    assert not final.ok
    assert "did not produce required outputs" in (final.error or "")


def test_gate_reports_become_the_test_and_verification_artefacts() -> None:
    gates = [
        GateResult(name="tests", status=GateStatus.PASSED, summary="12 passed", required=True),
        GateResult(name="syntax", status=GateStatus.PASSED, summary="ok", required=True),
    ]
    result = _run_result(diff="+x", gates=gates)
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert "tests: passed" in outcome.artifacts["tests"]
    assert "12 passed" in outcome.artifacts["tests"]


def test_prose_deliverables_come_from_the_worker_declaration() -> None:
    summary = (
        "planned the build\n<deliverables>\nplan: 3 milestones, foundation first\n"
        "blueprint: src/demo layout\nrisks: stripe webhook replay\nalternatives: none\n</deliverables>"
    )
    result = _run_result(summary=summary)
    outcome = run(_executor(result).execute(_item("planner"), get_role("planner"), WorkerContext()))
    assert outcome.ok
    assert outcome.artifacts["plan"].startswith("3 milestones")
    assert outcome.artifacts["risks"] == "stripe webhook replay"
    assert "alternatives" not in outcome.artifacts, "'none' is not a deliverable"


def test_steps_tokens_and_run_id_reach_the_result() -> None:
    result = _run_result(diff="+x", steps=7, tokens=4200)
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert outcome.steps_used == 7
    assert outcome.tokens_used == 4200
    assert outcome.data["run_id"]
    assert outcome.data["run_dir"]


def test_a_failed_run_reports_its_blockers() -> None:
    result = _run_result(
        status=RunStatus.BLOCKED,
        summary="stopped",
        blockers=["needs a decision about the payment provider"],
        gates=[GateResult(name="tests", status=GateStatus.FAILED, summary="2 failed", required=True)],
    )
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert not outcome.ok
    assert "needs a decision" in (outcome.error or "")
    assert "tests: 2 failed" in (outcome.error or "")


def test_blocking_gate_failures_are_blockers_even_when_the_run_looks_happy() -> None:
    result = _run_result(
        summary="done",
        diff="+x",
        gates=[GateResult(name="lint", status=GateStatus.FAILED, summary="3 issues", required=True)],
    )
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert "lint" in (outcome.error or "")


def test_fix_hints_become_follow_ups() -> None:
    result = _run_result(
        summary="done",
        diff="+x",
        gates=[GateResult(name="lint", status=GateStatus.FAILED, summary="x", fix_hint="run ruff --fix")],
    )
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert outcome.follow_ups == ["run ruff --fix"]


def test_evidence_lists_gates_and_changed_files() -> None:
    result = _run_result(
        diff="+x",
        files=["src/cart.py", "tests/test_cart.py"],
        gates=[GateResult(name="tests", status=GateStatus.PASSED, summary="3 passed")],
    )
    outcome = run(_executor(result).execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert any("gate tests" in line for line in outcome.evidence)
    assert any("changed src/cart.py" in line for line in outcome.evidence)
    assert any("diff" in line for line in outcome.evidence)


def test_a_skipped_browser_gate_produces_no_screenshots() -> None:
    result = _run_result(
        summary="styled",
        diff="+css",
        gates=[GateResult(name="browser", status=GateStatus.SKIPPED, summary="no url")],
    )
    outcome = run(_executor(result).execute(_item("ux-finisher"), get_role("ux-finisher"), WorkerContext()))
    assert "screenshots" not in outcome.artifacts


def test_browser_artefacts_are_collected_when_the_gate_ran() -> None:
    result = _run_result(
        summary="styled",
        diff="+css",
        gates=[
            GateResult(name="browser", status=GateStatus.PASSED, summary="journey ok", artifacts=["shot-1.png"])
        ],
    )
    outcome = run(_executor(result).execute(_item("browser-validator"), get_role("browser-validator"), WorkerContext()))
    assert "shot-1.png" in outcome.artifacts["screenshots"]


def test_a_runner_that_raises_is_contained() -> None:
    executor = _executor(RuntimeError("provider exploded"))
    outcome = run(executor.execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert not outcome.ok
    assert "RuntimeError" in (outcome.error or "")
    assert "could not run" in outcome.summary


def test_the_executor_records_the_runs_it_produced() -> None:
    executor = _executor(_run_result(diff="+x"))
    run(executor.execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert executor.runs and executor.runs[0]["role"] == "coder"


def test_an_observer_sees_each_finished_run() -> None:
    seen: list[str] = []
    executor = _executor(_run_result(diff="+x"), on_run=lambda item, result: seen.append(item.id))
    run(executor.execute(_item("coder"), get_role("coder"), WorkerContext()))
    assert seen == ["w1"]


def test_a_broken_observer_does_not_break_the_run() -> None:
    def boom(item, result):
        raise ValueError("observer exploded")

    executor = _executor(_run_result(diff="+x"), on_run=boom)
    assert run(executor.execute(_item("coder"), get_role("coder"), WorkerContext())).ok


# --------------------------------------------------------------------------- #
# the deliverables protocol
# --------------------------------------------------------------------------- #
def test_parse_deliverables_ignores_noise_and_empty_answers() -> None:
    text = (
        "some prose\n<deliverables>\nplan: a plan\nrisks: none\nnotes: N/A\n"
        "malformed line without a colon\nblueprint:   \n</deliverables>\ntrailing"
    )
    assert parse_deliverables(text) == {"plan": "a plan"}


def test_parse_deliverables_without_a_block_is_empty() -> None:
    assert parse_deliverables("no block here") == {}
    assert parse_deliverables("") == {}


def test_the_instruction_names_every_output() -> None:
    instruction = deliverables_instruction(("patch", "tests", "notes"))
    for name in ("patch", "tests", "notes"):
        assert f"{name}:" in instruction
    assert "<deliverables>" in instruction
    assert "none" in instruction, "the worker must be allowed to say it produced nothing"


def test_the_instruction_is_empty_for_a_role_with_no_outputs() -> None:
    assert deliverables_instruction(()) == ""


@pytest.fixture
def project_or_tmp(tmp_path: Path) -> Path:
    """A directory the runner can treat as a project root."""

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    return tmp_path


@pytest.mark.parametrize("role_id", ["coder", "inspector", "planner", "verifier", "releaser"])
def test_every_role_can_be_executed_end_to_end_offline(role_id: str) -> None:
    """The mapping must not KeyError on any role's output vocabulary."""

    entry = get_role(role_id)
    gates = [GateResult(name=name, status=GateStatus.PASSED, summary="ok") for name in entry.verify_gates]
    result = _run_result(
        summary="done\n<deliverables>\n" + "\n".join(f"{name}: produced" for name in entry.outputs) + "\n</deliverables>",
        diff="+change",
        files=["src/x.py"],
        gates=gates or None,
    )
    outcome = run(_executor(result).execute(_item(role_id), entry, WorkerContext()))
    assert outcome.ok
    assert outcome.artifacts


# --------------------------------------------------------------------------- #
# the role's grant is a boundary the runner enforces
# --------------------------------------------------------------------------- #
def test_the_role_grant_becomes_the_run_allow_list() -> None:
    executor = _executor(_run_result())
    role = get_role("documenter")
    request = executor.build_request(_item("documenter"), role, WorkerContext())
    assert request.tools == list(role.tools)
    # The documenter writes docs; it does not cut releases or run the suite.
    assert "git_commit" not in (request.tools or [])
    assert "test_run" not in (request.tools or [])


def test_a_read_only_role_cannot_be_granted_write_tools_by_the_executor() -> None:
    executor = _executor(_run_result())
    for role_id in ("inspector", "planner", "verifier", "browser-validator", "tooling"):
        role = get_role(role_id)
        request = executor.build_request(_item(role_id), role, WorkerContext())
        assert request.read_only is True
        assert "write_file" not in (request.tools or [])
        assert "apply_patch" not in (request.tools or [])


def test_a_withheld_tool_is_refused_with_the_role_in_the_reason(project_or_tmp: Path) -> None:
    """The boundary must be *felt*: an attempted call is refused, not ignored."""

    from unknown_agent.config.loader import load_settings
    from unknown_agent.core.runner import Runner, RunRequest

    loaded = load_settings(cwd=project_or_tmp)
    runner = Runner(
        loaded,
        request=RunRequest(
            task="write the docs",
            provider="offline",
            tools=list(get_role("documenter").tools),
            metadata={"orchestration": {"role": "documenter"}},
        ),
    )
    run(runner.setup())
    assert runner.registry is not None
    reason = runner.registry.withholder_reason("git_commit")
    assert reason is not None
    assert "documenter" in reason


def test_a_read_only_role_loses_shell_run_even_though_the_role_grants_it(project_or_tmp: Path) -> None:
    """The real boundary: verifier, browser-validator and tooling all grant
    ``shell_run`` while being non-mutating, and a shell can write anything.

    ``read_only`` is what makes that safe — it withholds every non-read tool, so the
    grant list being aspirational costs nothing.  If this test ever fails, three
    read-only roles can mutate the tree.
    """

    from unknown_agent.config.loader import load_settings
    from unknown_agent.core.models import RiskLevel
    from unknown_agent.core.runner import Runner, RunRequest

    for role_id in ("verifier", "browser-validator", "tooling"):
        role = get_role(role_id)
        assert not role.mutates
        assert "shell_run" in role.tools, "fixture premise: this role grants a shell"
        loaded = load_settings(cwd=project_or_tmp)
        runner = Runner(
            loaded,
            request=RunRequest(
                task="prove it",
                provider="offline",
                read_only=not role.mutates,
                tools=list(role.tools),
                metadata={"orchestration": {"role": role_id}},
            ),
        )
        run(runner.setup())
        assert runner.registry is not None
        assert "shell_run" not in runner.registry.names(), role_id
        assert "write_file" not in runner.registry.names(), role_id
        assert not [
            name
            for name in runner.registry.names()
            if runner.registry.get(name).risk in (RiskLevel.EXECUTE, RiskLevel.DESTRUCTIVE)
        ], role_id


def test_an_allow_list_can_never_widen_the_session_grant(project_or_tmp: Path) -> None:
    """The allow-list only withholds; it runs after read-only filtering."""

    from unknown_agent.config.loader import load_settings
    from unknown_agent.core.runner import Runner, RunRequest

    loaded = load_settings(cwd=project_or_tmp)
    runner = Runner(
        loaded,
        request=RunRequest(
            task="prove it",
            provider="offline",
            read_only=True,
            tools=["write_file", "shell_run", "read_file"],
            metadata={"orchestration": {"role": "verifier"}},
        ),
    )
    run(runner.setup())
    assert runner.registry is not None
    assert runner.registry.names() == ["read_file"]
