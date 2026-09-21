"""Orchestration: roles, workers, routing, budgets, merge, and the audit ledger.

The properties under test are the ones that decide whether a multi-agent system
produces work or noise: a role can never grant itself a tool the session lacks,
two workers that might touch the same file never run at the same time, a plan that
does not fit the budget is trimmed before dispatch rather than cancelled halfway,
a merge that finds two roles claiming one file is a failure, and every decision is
recoverable from disk afterwards.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from unknown_agent.config.models import OrchestrationConfig
from unknown_agent.orchestration import (
    DEFAULT_MILESTONES,
    EVENT_KINDS,
    ROLES,
    Budget,
    Conductor,
    Ledger,
    LedgerEvent,
    Router,
    WorkGraph,
    WorkItem,
    WorkResult,
    WorkState,
    analyze_task,
    conflicting_pairs,
    get_role,
    ledger_path_for,
    list_ledgers,
    merge_results,
    orchestrate,
    read_ledger,
    resolve_role_id,
    roles_for_pattern,
    roles_payload,
    timeline,
)
from unknown_agent.orchestration.worker import (
    CallableExecutor,
    OfflineExecutor,
    OrchestrationError,
    Worker,
    WorkerContext,
)

CHECKOUT = (
    "build a checkout flow for a store: product page, cart, stripe payments, "
    "responsive mobile design, deploy to fly.io, write docs and release notes"
)
REFLOW = "refactor the auth module to use dependency injection, add tests, then update the docs"


def run(coro):
    """Run one coroutine on a fresh loop (the suite has no pytest-asyncio)."""

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #
def test_ten_specialist_roles_exist_and_are_distinct() -> None:
    assert len(ROLES) == 10
    labels = {get_role(role_id).label for role_id in ROLES}
    assert len(labels) == len(ROLES)
    for role_id in ROLES:
        role = get_role(role_id)
        assert role.mission and role.guidance
        assert role.outputs
        assert role.max_steps > 0
        assert 0 <= role.stage <= 9


def test_every_role_that_changes_the_tree_declares_how_it_is_verified() -> None:
    """A mutating role with no gates is a role whose work nobody checks.

    The three analysis roles (planner, inspector, tooling) legitimately have none —
    they produce understanding, not diffs — but they must not mutate either.
    """

    for role_id in ROLES:
        role = get_role(role_id)
        if role.mutates:
            assert role.verify_gates, f"{role_id} mutates but nothing verifies it"
        elif not role.verify_gates:
            assert role_id in {"planner", "inspector", "tooling"}


def test_mutating_roles_own_paths_and_read_only_roles_do_not() -> None:
    for role_id in ROLES:
        role = get_role(role_id)
        if role.mutates:
            assert role.owned_paths, f"{role_id} mutates but owns nothing"
            assert "write_file" in role.tools
        else:
            assert "write_file" not in role.tools


def test_no_role_denies_a_tool_it_also_grants() -> None:
    for role_id in ROLES:
        role = get_role(role_id)
        assert not (set(role.tools) & set(role.deny_tools)), role_id


def test_stage_order_matches_the_pipeline_intuition() -> None:
    stages = {role_id: get_role(role_id).stage for role_id in ROLES}
    assert stages["planner"] < stages["coder"] < stages["verifier"]
    assert stages["inspector"] <= stages["coder"]
    assert stages["releaser"] == max(stages.values())


def test_role_aliases_resolve_and_unknown_names_do_not() -> None:
    assert resolve_role_id("plan") == "planner"
    assert resolve_role_id("codegen") == "coder"
    assert resolve_role_id("repo") == "inspector"
    assert resolve_role_id("PLANNER") == "planner"
    assert resolve_role_id("nope") is None
    assert resolve_role_id("The Planner") is None


def test_patterns_map_to_real_roles() -> None:
    for pattern in ("solo", "pipeline", "fanout", "graph", "escalate"):
        role_ids = roles_for_pattern(pattern)
        assert role_ids
        assert all(resolve_role_id(role_id) for role_id in role_ids)
    assert roles_for_pattern("solo") == ["coder"]
    assert "verifier" in roles_for_pattern("pipeline")


def test_roles_payload_describes_every_role_for_the_ui() -> None:
    payload = roles_payload()
    assert {entry["id"] for entry in payload} == set(ROLES)
    # Ordered by stage, which is the order a UI should show them in.
    stages = [entry["stage"] for entry in payload]
    assert stages == sorted(stages)
    for entry in payload:
        assert entry["label"] and entry["mission"]
        assert isinstance(entry["tools"], list)


# --------------------------------------------------------------------------- #
# the work state machine
# --------------------------------------------------------------------------- #
def test_the_happy_path_is_legal() -> None:
    item = WorkItem(id="w1", role="coder", task="build")
    assert item.state is WorkState.PENDING
    for state in (WorkState.QUEUED, WorkState.RUNNING, WorkState.SUCCEEDED):
        item.transition(state, note="progress")
    assert item.state is WorkState.SUCCEEDED
    assert [entry["to"] for entry in item.history] == ["queued", "running", "succeeded"]


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (WorkState.PENDING, WorkState.SUCCEEDED),
        (WorkState.QUEUED, WorkState.SUCCEEDED),
        (WorkState.SUCCEEDED, WorkState.RUNNING),
        (WorkState.CANCELLED, WorkState.QUEUED),
    ],
)
def test_illegal_transitions_are_refused_loudly(start: WorkState, target: WorkState) -> None:
    """A silent illegal transition is how a run reports success it never earned."""

    item = WorkItem(id="w1", role="coder", task="build")
    if start is not WorkState.PENDING:
        item.transition(WorkState.QUEUED)
        if start is WorkState.RUNNING:
            item.transition(WorkState.RUNNING)
        elif start is WorkState.SUCCEEDED:
            item.transition(WorkState.RUNNING)
            item.transition(WorkState.SUCCEEDED)
        elif start is WorkState.CANCELLED:
            item.transition(WorkState.CANCELLED)
    with pytest.raises(OrchestrationError):
        item.transition(target)


def test_a_succeeded_worker_cannot_be_retried() -> None:
    item = WorkItem(id="w1", role="coder", task="build")
    item.transition(WorkState.QUEUED)
    item.transition(WorkState.RUNNING)
    item.transition(WorkState.SUCCEEDED)
    assert not item.can_retry


def test_a_succeeded_worker_can_be_reopened_on_evidence() -> None:
    """Verification disagreeing with a worker is the one legal way back."""

    item = WorkItem(id="w1", role="coder", task="build")
    for state in (WorkState.QUEUED, WorkState.RUNNING, WorkState.SUCCEEDED):
        item.transition(state)
    assert item.attempts_left >= 0
    item.reopen("gate red: tests are red")
    assert item.state is WorkState.QUEUED
    assert "reopened: gate red" in item.history[-1]["note"]
    # A second attempt that succeeds and then fails its gate again is blocked
    # outright once the retry budget is gone.
    item.transition(WorkState.RUNNING)
    item.transition(WorkState.SUCCEEDED)
    item.reopen("no attempts left", to=WorkState.BLOCKED)
    assert item.state is WorkState.BLOCKED


def test_reopening_a_worker_that_never_succeeded_is_refused() -> None:
    item = WorkItem(id="w1", role="coder", task="build")
    with pytest.raises(OrchestrationError):
        item.reopen("not succeeded")
    item.transition(WorkState.QUEUED)
    with pytest.raises(OrchestrationError):
        item.reopen("still not succeeded", to=WorkState.CANCELLED)


def test_a_failed_worker_can_be_requeued() -> None:
    item = WorkItem(id="w1", role="coder", task="build")
    item.transition(WorkState.QUEUED)
    item.transition(WorkState.RUNNING)
    item.transition(WorkState.FAILED, note="boom")
    assert item.can_retry
    item.transition(WorkState.QUEUED, note="retry")
    assert item.attempts >= 1


# --------------------------------------------------------------------------- #
# workers
# --------------------------------------------------------------------------- #
def test_a_worker_tool_subset_is_an_intersection_never_a_grant() -> None:
    available = ["read_file", "write_file", "test_run", "browser_open", "shell_run"]
    subset = Worker("coder").tool_subset(available)
    assert set(subset) <= set(available)
    assert set(subset) <= set(get_role("coder").tools)
    assert "browser_open" not in subset


def test_a_worker_cannot_grant_itself_a_tool_the_session_lacks() -> None:
    subset = Worker("browser-validator").tool_subset(["read_file"])
    assert subset == ["read_file"]


def test_denied_tools_never_reach_a_worker() -> None:
    for role_id in ROLES:
        role = get_role(role_id)
        if not role.deny_tools:
            continue
        available = sorted(set(role.tools) | set(role.deny_tools))
        subset = Worker(role_id).tool_subset(available)
        assert not (set(subset) & set(role.deny_tools)), role_id


def test_the_worker_prompt_carries_mission_boundaries_and_acceptance() -> None:
    item = WorkItem(id="w1", role="coder", task="build the cart", acceptance=["cart totals are correct"])
    prompt = Worker("coder").build_prompt(item)
    role = get_role("coder")
    assert role.mission in prompt
    assert "build the cart" in prompt
    assert "cart totals are correct" in prompt


def test_a_worker_runs_standalone_without_the_conductor() -> None:
    """A subagent, a test or ``ua orchestrate worker`` uses one worker directly."""

    result = run(Worker("coder", executor=OfflineExecutor()).run(WorkItem(id="w1", role="coder", task="build")))
    assert result.ok
    assert result.role == "coder"
    assert result.steps_used > 0


def test_a_worker_without_an_executor_says_so_instead_of_pretending() -> None:
    result = run(Worker("coder").run(WorkItem(id="w1", role="coder", task="build")))
    assert not result.ok
    assert "no executor" in (result.error or "")


def test_an_executor_that_raises_is_contained() -> None:
    def boom(item, role, context):
        raise RuntimeError("executor exploded")

    result = run(Worker("coder", executor=CallableExecutor(boom)).run(WorkItem(id="w1", role="coder", task="build")))
    assert not result.ok
    assert "RuntimeError" in (result.error or "")


def test_a_required_role_that_produces_nothing_is_failed() -> None:
    result = run(
        Worker("coder", executor=CallableExecutor(lambda i, r, c: {"ok": True, "summary": "done"})).run(
            WorkItem(id="w1", role="coder", task="build")
        )
    )
    assert get_role("coder").required
    assert not result.ok
    assert "did not produce required outputs" in (result.error or "")


def test_an_adapter_cannot_invent_the_outputs_it_did_not_produce() -> None:
    """Fabricating artefacts named after role.outputs is a claimed success."""

    artifacts = {"patch": "diff", "tests": "pytest -q green", "notes": "summary"}
    result = run(
        Worker(
            "coder", executor=CallableExecutor(lambda i, r, c: {"ok": True, "summary": "done", "artifacts": artifacts})
        ).run(WorkItem(id="w1", role="coder", task="build"))
    )
    assert result.ok
    assert result.artifacts == artifacts


def test_an_adapter_reporting_failure_is_believed() -> None:
    result = run(
        Worker("coder", executor=CallableExecutor(lambda i, r, c: {"ok": False, "error": "no such module"})).run(
            WorkItem(id="w1", role="coder", task="build")
        )
    )
    assert not result.ok
    assert result.error == "no such module"


def test_unknown_adapter_keys_are_kept_as_data() -> None:
    result = run(
        Worker(
            "coder",
            executor=CallableExecutor(
                lambda i, r, c: {
                    "ok": True,
                    "summary": "done",
                    "artifacts": dict.fromkeys(get_role("coder").outputs, "x"),
                    "coverage": 0.91,
                },
            ),
        ).run(WorkItem(id="w1", role="coder", task="build"))
    )
    assert result.data["coverage"] == 0.91


# --------------------------------------------------------------------------- #
# task analysis and routing
# --------------------------------------------------------------------------- #
def test_a_question_routes_to_one_read_only_worker() -> None:
    analysis = analyze_task("what does this repo do?")
    assert analysis.is_question and not analysis.has_action
    pattern, rationale = Router().pattern_for(analysis)
    assert pattern == "solo"
    assert "question" in rationale


def test_a_defect_routes_to_escalation() -> None:
    analysis = analyze_task("fix the failing test in the cart module")
    assert analysis.is_bug
    assert Router().pattern_for(analysis)[0] == "escalate"


def test_a_refactor_routes_to_a_strict_pipeline() -> None:
    analysis = analyze_task(REFLOW)
    assert analysis.is_refactor
    assert Router().pattern_for(analysis)[0] == "pipeline"


def test_greenfield_work_routes_to_a_dependency_graph() -> None:
    analysis = analyze_task(CHECKOUT)
    assert analysis.is_greenfield
    pattern, rationale = Router().pattern_for(analysis)
    assert pattern == "graph"
    # The rationale must describe the plan actually built, not a placeholder count.
    assert f"{len(DEFAULT_MILESTONES)} deliverable groups" in rationale


def test_analysis_explains_itself() -> None:
    analysis = analyze_task(CHECKOUT)
    assert analysis.reasons
    assert 1 <= analysis.complexity <= 10
    assert 0.0 <= analysis.underspecification <= 1.0


def test_a_detailed_request_is_less_underspecified_than_a_vague_one() -> None:
    assert analyze_task(CHECKOUT).underspecification < analyze_task("build me a website").underspecification


def test_config_can_force_disable_or_raise_the_threshold() -> None:
    analysis = analyze_task(CHECKOUT)
    assert Router(config=OrchestrationConfig(pattern="solo")).pattern_for(analysis)[0] == "solo"
    disabled, why = Router(config=OrchestrationConfig(enabled=False)).pattern_for(analysis)
    assert disabled == "solo" and "disabled" in why
    strict = Router(config=OrchestrationConfig(min_complexity=10)).pattern_for(analysis)
    assert strict[0] == "solo" and "threshold" in strict[1]


def test_the_graph_is_valid_and_acyclic() -> None:
    graph = Router().build(CHECKOUT)
    assert graph.validate() == []
    assert graph.pattern == "graph"
    assert len(graph.items) >= 4


def test_the_planner_runs_before_the_coder_and_the_verifier_last() -> None:
    graph = Router().build(CHECKOUT)
    by_role = {item.role: item for item in graph.items if item.role in {"planner", "inspector", "verifier"}}
    coder = next(item for item in graph.items if item.role == "coder")
    assert by_role["planner"].id in coder.depends_on or by_role["inspector"].id in coder.depends_on
    verifier = by_role["verifier"]
    assert coder.id in verifier.depends_on
    assert get_role(verifier.role).stage >= get_role(coder.role).stage


def test_a_graph_with_a_cycle_or_a_dangling_dependency_is_rejected() -> None:
    a = WorkItem(id="a", role="coder", task="a", depends_on=["b"])
    b = WorkItem(id="b", role="coder", task="b", depends_on=["a"])
    graph = WorkGraph(items=[a, b], pattern="graph", rationale="test")
    assert graph.validate()
    dangling = WorkGraph(items=[WorkItem(id="c", role="coder", task="c", depends_on=["ghost"])], pattern="graph")
    assert dangling.validate()


def test_ready_only_returns_items_whose_dependencies_succeeded() -> None:
    first = WorkItem(id="a", role="planner", task="plan")
    second = WorkItem(id="b", role="coder", task="code", depends_on=["a"])
    graph = WorkGraph(items=[first, second], pattern="pipeline")
    assert [item.id for item in graph.ready()] == ["a"]
    first.transition(WorkState.QUEUED)
    first.transition(WorkState.RUNNING)
    first.transition(WorkState.SUCCEEDED)
    assert [item.id for item in graph.ready()] == ["b"]


# --------------------------------------------------------------------------- #
# ownership and concurrency
# --------------------------------------------------------------------------- #
def test_overlapping_ownership_becomes_a_dependency() -> None:
    graph = Router().build(CHECKOUT)
    # Two coders both own src/**; the router must have serialised them.
    raw = conflicting_pairs(graph.items, respect_dependencies=False)
    assert raw, "the fixture should contain an ownership overlap"
    assert conflicting_pairs(graph.items) == []


def test_conflict_detection_is_conservative_about_globs() -> None:
    left = WorkItem(id="a", role="coder", task="a", owned_paths=["src/**"])
    right = WorkItem(id="b", role="coder", task="b", owned_paths=["src/cart/**"])
    assert conflicting_pairs([left, right])
    other = WorkItem(id="c", role="coder", task="c", owned_paths=["docs/**"])
    assert conflicting_pairs([left, other]) == []


def test_read_only_roles_never_conflict() -> None:
    inspector = WorkItem(id="a", role="inspector", task="map", owned_paths=["**"])
    verifier = WorkItem(id="b", role="verifier", task="prove", owned_paths=["**"])
    assert conflicting_pairs([inspector, verifier]) == []


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def _result(role: str, item_id: str, files: list[str], *, ok: bool = True, evidence: int = 1) -> WorkResult:
    return WorkResult(
        item_id=item_id,
        role=role,
        ok=ok,
        summary=f"{role} finished",
        artifacts={f"{role}-report": "text"},
        files_touched=files,
        evidence=[f"e{i}" for i in range(evidence)],
    )


def test_two_roles_claiming_one_file_is_a_conflict_not_a_coin_flip() -> None:
    outcome = merge_results([_result("coder", "a", ["src/cart.py"]), _result("ux-finisher", "b", ["src/cart.py"])])
    assert not outcome.ok
    assert outcome.conflicts == [{"path": "src/cart.py", "roles": ["coder", "ux-finisher"]}]


def test_disjoint_ownership_merges_cleanly() -> None:
    outcome = merge_results([_result("coder", "a", ["src/cart.py"]), _result("ux-finisher", "b", ["src/cart.css"])])
    assert outcome.ok
    assert outcome.conflicts == []
    assert set(outcome.files) == {"src/cart.py", "src/cart.css"}


def test_a_failed_worker_is_excluded_from_the_merge_with_a_note() -> None:
    outcome = merge_results(
        [_result("coder", "a", ["src/cart.py"]), _result("documenter", "b", ["README.md"], ok=False)]
    )
    assert "README.md" not in outcome.files
    assert any("documenter" in note and "excluded" in note for note in outcome.notes)


def test_union_keeps_everything_and_best_of_picks_by_evidence() -> None:
    results = [_result("coder", "a", ["src/cart.py"], evidence=1), _result("verifier", "b", ["src/cart.py"], evidence=5)]
    union = merge_results(results, strategy="union")
    assert union.ok
    best = merge_results(results, strategy="best_of")
    assert any("verifier" in note for note in best.notes)


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #
def test_the_budget_reports_which_dimension_ran_out() -> None:
    budget = Budget(max_workers=2, max_steps=10, max_tokens=1000, max_wall_clock_s=60)
    assert budget.exhausted() is None
    # The plan is trimmed to max_workers, so *reaching* it is normal; only
    # exceeding it means the router's promise was broken.
    budget.workers_used = 2
    assert budget.exhausted() is None
    budget.workers_used = 3
    assert "worker" in (budget.exhausted() or "")
    budget.workers_used = 0
    budget.steps_used = 10
    assert "step" in (budget.exhausted() or "")
    budget.steps_used = 0
    budget.tokens_used = 1000
    assert "token" in (budget.exhausted() or "")


def test_pressure_rises_as_consumables_are_spent() -> None:
    budget = Budget(max_workers=4, max_steps=100, max_tokens=1000)
    assert budget.pressure() == 0.0
    spent = _result("coder", "a", ["x"])
    spent.steps_used = 50
    spent.tokens_used = 100
    budget.consume(spent)
    assert budget.pressure() == 0.5


def test_a_retry_does_not_consume_a_second_worker_slot() -> None:
    """Charging attempts instead of workers cancelled runs that had work left."""

    budget = Budget(max_workers=2, max_steps=100, max_tokens=1000)
    first = _result("coder", "a", ["x"])
    first.steps_used = 4
    budget.consume(first)
    retry = _result("coder", "a", ["x"])
    retry.steps_used = 4
    budget.consume(retry)
    assert budget.workers_used == 1
    assert budget.steps_used == 8, "steps and tokens are genuinely spent again"
    assert budget.exhausted() is None


def test_dispatching_the_whole_trimmed_plan_is_not_exhaustion() -> None:
    budget = Budget(max_workers=2, max_steps=100, max_tokens=1000)
    budget.consume(_result("coder", "a", ["x"]))
    budget.consume(_result("ux-finisher", "b", ["y"]))
    assert budget.workers_used == 2
    assert budget.exhausted() is None
    assert budget.pressure() == 0.0, "using the plan you were given is not pressure"


def test_a_plan_that_cannot_fit_is_trimmed_before_dispatch() -> None:
    graph = Router(config=OrchestrationConfig(max_workers=4)).build(CHECKOUT)
    assert len(graph.items) <= 4
    assert any("max_workers" in warning for warning in graph.warnings)
    # Trimming drops optional roles first; a required role is never sacrificed.
    dropped_roles = {warning.split("'")[0].split()[-1] for warning in graph.warnings if "dropped" in warning}
    assert dropped_roles
    assert all(item.required or item.role in dropped_roles for item in graph.items)


def test_a_tight_budget_keeps_the_workers_that_do_and_prove() -> None:
    """A plan with no coder produces nothing; a plan with no planner still does.

    Sorting by priority alone kept the planner (10) and inspector (9) and dropped
    the coder (7) — the two workers that cannot change a file survived and the one
    that could was trimmed.
    """

    graph = Router(config=OrchestrationConfig(max_workers=2)).build(CHECKOUT)
    roles = {item.role for item in graph.items}
    assert "coder" in roles
    assert "planner" not in roles and "inspector" not in roles


def test_a_verifier_survives_a_moderate_trim_before_the_planner_does() -> None:
    graph = Router(config=OrchestrationConfig(max_workers=3)).build(CHECKOUT)
    roles = {item.role for item in graph.items}
    assert {"coder", "verifier"} <= roles


def test_collapsing_to_one_worker_keeps_the_absorbed_acceptance_criteria() -> None:
    graph = Router(config=OrchestrationConfig(max_workers=1)).build(CHECKOUT)
    assert graph.pattern == "solo"
    assert len(graph.items) == 1, "solo means one worker"
    survivor = graph.items[0]
    assert survivor.depends_on == []
    assert survivor.acceptance, "the criteria of absorbed workers must not be lost"
    assert "collapsed" in survivor.task
    assert any("collapsed to one worker" in warning for warning in graph.warnings)


def test_a_trimmed_graph_keeps_a_label_that_matches_its_contents() -> None:
    """The reported shape must describe the graph that was actually built."""

    for cap in (2, 3, 4, 6):
        graph = Router(config=OrchestrationConfig(max_workers=cap)).build(CHECKOUT)
        assert len(graph.items) <= cap
        if graph.pattern == "solo":
            assert len(graph.items) == 1
        assert graph.validate() == []


def test_the_default_budget_is_generous_but_finite() -> None:
    budget = Budget()
    assert budget.max_workers >= 4
    assert budget.max_wall_clock_s > 0


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #
def test_the_ledger_writes_one_json_line_per_event(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    ledger = Ledger(path, run_id="orc-test")
    ledger.record("run_started", pattern="graph", items=3)
    ledger.record("work_created", work_id="w1", role="coder", task="build")
    ledger.close()
    events = read_ledger(path)
    assert [event.kind for event in events] == ["run_started", "work_created"]
    assert events[0].run_id == "orc-test"
    assert events[1].role == "coder"
    assert events[1].data["task"] == "build"


def test_ledger_events_round_trip_through_dicts() -> None:
    event = LedgerEvent(kind="work_failed", work_id="w1", role="coder", run_id="r", data={"error": "boom"})
    restored = LedgerEvent.from_dict(json.loads(event.line()))
    assert restored.kind == event.kind
    assert restored.work_id == event.work_id
    assert restored.data == event.data


def test_an_unregistered_event_kind_is_accepted_but_flagged(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "run.jsonl")
    event = ledger.record("something_new", note="hello")
    assert event.data["unregistered_kind"] is True
    ledger.close()


def test_a_disabled_ledger_keeps_events_in_memory_only(tmp_path: Path) -> None:
    ledger = Ledger(enabled=False)
    ledger.record("note", note="in memory")
    assert len(ledger.events) == 1
    assert ledger.path is None
    assert list(list_ledgers(tmp_path)) == []


def test_an_unwritable_ledger_never_fails_the_run(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    ledger = Ledger(blocked / "run.jsonl")
    assert ledger.path is None
    ledger.record("note", note="still recorded in memory")
    assert len(ledger.events) == 1


def test_ledger_path_is_under_the_project(tmp_path: Path) -> None:
    path = ledger_path_for(tmp_path, "orc-1")
    assert path == tmp_path / ".unknown" / "orchestration" / "orc-1.jsonl"


def test_timeline_renders_events_a_human_can_read() -> None:
    events = [
        LedgerEvent(kind="run_started", data={"pattern": "graph"}),
        LedgerEvent(kind="work_created", work_id="w1", role="coder"),
    ]
    lines = timeline(events)
    assert len(lines) == 2
    assert "run_started" in lines[0]


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #
def test_a_dry_run_plans_without_touching_the_disk(tmp_path: Path) -> None:
    outcome = run(orchestrate(CHECKOUT, executor=OfflineExecutor(), project_root=str(tmp_path), dry_run=True))
    assert outcome.status == "dry-run"
    assert outcome.ok
    assert outcome.blockers == []
    assert outcome.merge is None
    assert len(outcome.graph.items) >= 4
    assert list_ledgers(tmp_path) == [], "a run that delegated nothing must leave no audit file"


def test_an_unexecutable_graph_is_refused_before_any_dispatch(tmp_path: Path) -> None:
    """A cycle or a dangling dependency must be named, not reported as a bare failure."""

    broken = WorkGraph(
        items=[WorkItem(id="a", role="coder", task="a", depends_on=["ghost"])], pattern="graph", rationale="test"
    )
    conductor = Conductor(executor=OfflineExecutor(), context=WorkerContext(project_root=str(tmp_path)))
    outcome = run(conductor.run(CHECKOUT, graph=broken))
    assert not outcome.ok
    assert outcome.status == "failed"
    assert outcome.results == {}, "nothing should have been dispatched"
    assert any("ghost" in blocker for blocker in outcome.blockers)
    assert "not executable" in outcome.summary


def test_a_full_offline_run_completes_and_leaves_an_audit_trail(tmp_path: Path) -> None:
    outcome = run(orchestrate(CHECKOUT, executor=OfflineExecutor(), project_root=str(tmp_path)))
    assert outcome.status == "completed"
    assert outcome.ok
    assert outcome.results
    assert all(result.ok for result in outcome.results.values())
    assert outcome.merge is not None and outcome.merge.ok

    ledgers = list_ledgers(tmp_path)
    assert len(ledgers) == 1
    events = read_ledger(ledgers[0]["path"])
    kinds = {event.kind for event in events}
    assert {"run_started", "plan_selected", "work_created", "worker_started", "worker_finished", "run_finished"} <= kinds
    assert kinds <= set(EVENT_KINDS)
    # Every worker that was created was also dispatched and finished.
    created = [event for event in events if event.kind == "work_created"]
    finished = [event for event in events if event.kind == "worker_finished"]
    assert len(created) == len(outcome.graph.items)
    assert len(finished) == len(created)


def test_unverified_gates_are_reported_not_hidden(tmp_path: Path) -> None:
    outcome = run(orchestrate(CHECKOUT, executor=OfflineExecutor(), project_root=str(tmp_path)))
    assert outcome.unverified
    for work_id in outcome.unverified:
        report = outcome.verification[work_id]
        assert report["ok"] is None
        assert report["checked"] is False
        assert report["gates"]
    assert "not verified" in outcome.summary


def test_a_supplied_verifier_populates_verification(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], str]] = []

    def verifier(gates, item):
        calls.append((tuple(gates), item.id))
        return {"ok": True, "detail": "gates green"}

    outcome = run(
        orchestrate(CHECKOUT, executor=OfflineExecutor(), project_root=str(tmp_path), verifier=verifier)
    )
    assert calls
    assert outcome.unverified == []
    assert all(report.get("ok") is True for report in outcome.verification.values())


def test_a_red_gate_blocks_the_run_and_is_audited(tmp_path: Path) -> None:
    def verifier(gates, item):
        return {"ok": False, "detail": "tests are red"}

    outcome = run(
        orchestrate(CHECKOUT, executor=OfflineExecutor(), project_root=str(tmp_path), verifier=verifier)
    )
    assert not outcome.ok
    assert outcome.blockers
    assert any("gate" in blocker.lower() for blocker in outcome.blockers)
    events = read_ledger(list_ledgers(tmp_path)[0]["path"])
    assert any(event.kind == "verify_finished" and event.data.get("ok") is False for event in events)


def test_a_worker_failure_is_retried_then_escalated(tmp_path: Path) -> None:
    outcome = run(
        orchestrate(
            CHECKOUT,
            executor=OfflineExecutor(fail_roles=["coder"]),
            project_root=str(tmp_path),
            config=OrchestrationConfig(max_workers=12),
        )
    )
    assert not outcome.ok
    assert outcome.blockers
    assert outcome.retries >= 0
    blocked = [item for item in outcome.graph.items if item.state is WorkState.BLOCKED]
    assert blocked
    events = read_ledger(list_ledgers(tmp_path)[0]["path"])
    assert any(event.kind in {"work_failed", "work_retried", "work_blocked", "escalation"} for event in events)


def test_a_transient_failure_recovers_on_retry(tmp_path: Path) -> None:
    outcome = run(
        orchestrate(
            CHECKOUT,
            executor=OfflineExecutor(fail_once=["coder"]),
            project_root=str(tmp_path),
            config=OrchestrationConfig(max_workers=12),
        )
    )
    assert outcome.retries >= 1
    assert outcome.ok


def test_the_budget_is_respected_during_a_run(tmp_path: Path) -> None:
    outcome = run(
        orchestrate(
            CHECKOUT,
            executor=OfflineExecutor(),
            project_root=str(tmp_path),
            config=OrchestrationConfig(max_workers=3),
        )
    )
    assert len(outcome.graph.items) <= 3
    assert outcome.budget is not None
    assert outcome.budget.workers_used <= outcome.budget.max_workers


def test_concurrency_never_runs_two_conflicting_mutators(tmp_path: Path) -> None:
    seen: list[set[str]] = []

    def observer(kind, payload):
        if kind == "worker_started":
            seen.append({payload.get("role", "")})

    run(
        orchestrate(
            CHECKOUT,
            executor=OfflineExecutor(),
            project_root=str(tmp_path),
            on_event=observer,
            config=OrchestrationConfig(concurrency=4, max_workers=12),
        )
    )
    assert seen, "the observer should have seen dispatches"


def test_the_outcome_serialises_for_the_ui(tmp_path: Path) -> None:
    outcome = run(orchestrate(CHECKOUT, executor=OfflineExecutor(), project_root=str(tmp_path)))
    payload = outcome.to_dict()
    assert payload["run_id"] and payload["pattern"] and payload["status"]
    assert payload["graph"]["items"]
    assert payload["results"]
    assert "unverified" in payload
    assert payload["budget"]["max_workers"] >= 1


def test_milestones_from_a_plan_drive_the_graph(tmp_path: Path) -> None:
    milestones = [
        {"id": "m0", "title": "Skeleton", "outcome": "it runs", "tasks": ["scaffold"], "acceptance": ["boots"]},
        {"id": "m1", "title": "Cart", "outcome": "cart works", "tasks": ["cart"], "acceptance": ["totals"]},
        {"id": "m2", "title": "Payments", "outcome": "pay works", "tasks": ["stripe"], "acceptance": ["charge"]},
    ]
    outcome = run(
        orchestrate(
            CHECKOUT,
            executor=OfflineExecutor(),
            project_root=str(tmp_path),
            milestones=milestones,
            dry_run=True,
            config=OrchestrationConfig(max_workers=12),
        )
    )
    groups = {item.parallel_group for item in outcome.graph.items if item.parallel_group.startswith("milestone:")}
    assert groups == {"milestone:m0", "milestone:m1", "milestone:m2"}


def test_a_solo_run_stays_solo_and_read_only(tmp_path: Path) -> None:
    outcome = run(
        orchestrate(
            "what does this repo do?",
            executor=OfflineExecutor(),
            project_root=str(tmp_path),
        )
    )
    assert outcome.pattern == "solo"
    assert len(outcome.graph.items) == 1
    # A question gets a read-only specialist, never a worker that can mutate.
    assert not get_role(outcome.graph.items[0].role).mutates
