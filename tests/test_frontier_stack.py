"""Tests for the frontier hyper-agent stack.

Covers the router, the pre-action reasoning protocol, the long-horizon engine
(including crash recovery and drift detection), the AST tools and the
background test runner.  The engine tests deliberately exercise the paths that
only matter when something goes wrong, because those are the ones nobody
notices are broken until a three-week mission dies.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from unknown_agent.config.loader import load_settings
from unknown_agent.core.engine import (
    EngineError,
    EntropyDetector,
    EntropyVerdict,
    LongHorizonEngine,
    MissionNode,
    MissionStatus,
    MissionStore,
    NodeOutcome,
    NodeStatus,
)
from unknown_agent.core.models import RiskLevel, ToolCall
from unknown_agent.observability.events import EventBus
from unknown_agent.orchestration.router import (
    Complexity,
    ModelRouter,
    RoutingError,
    RoutingRequest,
    Tier,
    card_for,
)
from unknown_agent.reasoning.frontier_protocol import (
    ActionClass,
    ContextAssembler,
    FrontierProtocol,
    Phase,
    ThoughtGraph,
    Verdict,
    analyse_failure,
    classify_action,
    verify_patch_syntax,
)
from unknown_agent.tools.base import ToolContext
from unknown_agent.tools.builtin import ast_tools, test_runner_tools


# --------------------------------------------------------------------------- #
# router
# --------------------------------------------------------------------------- #
class TestModelRouter:
    def test_routes_each_role_to_its_tier(self) -> None:
        router = ModelRouter()
        planner = router.route(RoutingRequest(role_id="planner", complexity=Complexity.HIGH))
        verifier = router.route(RoutingRequest(role_id="verifier", complexity=Complexity.LOW))

        assert planner.tier is Tier.FRONTIER_REASONING
        assert verifier.tier is Tier.FAST
        # The QA loop must not be routed to the most expensive brain available.
        assert verifier.model.speed >= planner.model.speed

    def test_is_deterministic(self) -> None:
        picks = {
            ModelRouter().route(RoutingRequest(role_id="coder")).model_id for _ in range(10)
        }
        assert len(picks) == 1

    def test_never_routes_to_an_unconfirmed_provisional_model(self) -> None:
        # With no inventory, a forward-declared model must not be selected.
        decision = ModelRouter().route(RoutingRequest(role_id="planner"))
        assert card_for(decision.model_id) is not None
        assert not decision.model.provisional

    def test_routes_to_provisional_only_when_host_confirms_it(self) -> None:
        router = ModelRouter(available=["claude-opus-5-5"])
        decision = router.route(RoutingRequest(role_id="planner"))
        assert decision.model_id == "claude-opus-5-5"
        assert decision.model.provisional

    def test_vision_requirement_degrades_loudly_rather_than_silently(self) -> None:
        router = ModelRouter(available=["deepseek-reasoner"])
        decision = router.route(RoutingRequest(role_id="ux-finisher", needs_vision=True))
        assert decision.degraded
        assert "vision" in decision.degraded_reason

    def test_raises_when_nothing_is_reachable(self) -> None:
        with pytest.raises(RoutingError, match="no model is reachable"):
            ModelRouter(available=["does-not-exist"]).route(RoutingRequest(role_id="coder"))

    def test_complexity_moves_the_thinking_budget(self) -> None:
        router = ModelRouter()
        low = router.route(RoutingRequest(role_id="planner", complexity=Complexity.TRIVIAL))
        high = router.route(RoutingRequest(role_id="planner", complexity=Complexity.CRITICAL))
        assert high.thinking_budget_tokens > low.thinking_budget_tokens

    def test_refuses_to_invent_a_price_it_does_not_know(self) -> None:
        router = ModelRouter()
        unpriced = router.estimate_cost(
            card_for("claude-opus-5-5"), input_tokens=1000, output_tokens=1000
        )
        priced = router.estimate_cost(card_for("gpt-5"), input_tokens=1_000_000, output_tokens=0)

        assert unpriced.usd is None and not unpriced.priced
        assert "no published tariff" in unpriced.reason
        assert priced.priced and priced.usd == pytest.approx(1.25)

    def test_context_window_excludes_models_that_cannot_hold_the_prompt(self) -> None:
        router = ModelRouter(available=["qwen2.5-coder:32b", "gemini-2.5-pro"])
        decision = router.route(
            RoutingRequest(role_id="coder", estimated_input_tokens=500_000)
        )
        assert decision.model_id == "gemini-2.5-pro"

    def test_excluded_models_are_not_returned(self) -> None:
        router = ModelRouter()
        first = router.route(RoutingRequest(role_id="coder"))
        second = router.route(RoutingRequest(role_id="coder", exclude=(first.model_id,)))
        assert second.model_id != first.model_id


# --------------------------------------------------------------------------- #
# frontier protocol
# --------------------------------------------------------------------------- #
def _valid_graph(**overrides: object) -> ThoughtGraph:
    payload: dict[str, object] = {
        "goal": "Remove the deprecated billing adapter",
        "action_summary": "Delete src/billing/legacy.py and its registration",
        "invariants": [
            {"description": "The billing suite stays green", "check": "pytest tests/billing"}
        ],
        "counterfactuals": [
            {"approach": "Keep it behind a feature flag", "why_rejected": "Doubles the test matrix"}
        ],
        "rollback": {
            "strategy": "git_revert",
            "steps": ["git revert HEAD"],
            "verified_possible": True,
        },
        "confidence": 0.85,
    }
    payload.update(overrides)
    return ThoughtGraph.model_validate(payload)


class TestActionClassification:
    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            (ToolCall.new("read_file", {"path": "a.py"}), ActionClass.READ),
            (ToolCall.new("write_file", {"path": "a.py"}), ActionClass.MUTATE),
            (ToolCall.new("write_file", {"path": "pyproject.toml"}), ActionClass.STRUCTURAL),
            (ToolCall.new("delete_path", {"path": "a.py"}), ActionClass.STRUCTURAL),
            (ToolCall.new("shell_run", {"command": "rm -rf build"}), ActionClass.DESTRUCTIVE),
            (ToolCall.new("shell_run", {"command": "git push origin main"}), ActionClass.DESTRUCTIVE),
            (ToolCall.new("shell_run", {"command": "pytest -q"}), ActionClass.MUTATE),
        ],
    )
    def test_classifies_by_consequence(self, call: ToolCall, expected: ActionClass) -> None:
        assert classify_action(call, project_root="/repo") is expected

    def test_path_escape_is_destructive(self) -> None:
        call = ToolCall.new("write_file", {"path": "../../etc/passwd"})
        assert classify_action(call, project_root="/repo") is ActionClass.DESTRUCTIVE

    def test_read_risk_short_circuits(self) -> None:
        call = ToolCall.new("custom_probe", {})
        assert classify_action(call, risk=RiskLevel.READ) is ActionClass.READ


class TestAdjudication:
    def test_complete_reflection_proceeds(self) -> None:
        protocol = FrontierProtocol(strictness=1)
        ruling = protocol.adjudicate(ToolCall.new("git_push", {}), _valid_graph())
        assert ruling.verdict is Verdict.PROCEED and ruling.ok

    def test_destructive_without_rollback_aborts(self) -> None:
        protocol = FrontierProtocol(strictness=1)
        graph = _valid_graph(rollback={"strategy": "none", "steps": []})
        ruling = protocol.adjudicate(ToolCall.new("git_push", {}), graph)
        assert ruling.verdict is Verdict.ABORT
        assert "no rollback" in " ".join(ruling.reasons)

    def test_low_confidence_on_irreversible_action_aborts(self) -> None:
        protocol = FrontierProtocol(strictness=1)
        ruling = protocol.adjudicate(ToolCall.new("git_push", {}), _valid_graph(confidence=0.4))
        assert ruling.verdict is Verdict.ABORT

    def test_missing_alternatives_asks_for_revision(self) -> None:
        protocol = FrontierProtocol(strictness=1)
        ruling = protocol.adjudicate(
            ToolCall.new("delete_path", {"path": "a.py"}), _valid_graph(counterfactuals=[])
        )
        assert ruling.verdict is Verdict.REVISE
        assert ruling.required_before_retry

    def test_already_failing_invariant_aborts(self) -> None:
        protocol = FrontierProtocol(strictness=1)
        graph = _valid_graph(
            invariants=[
                {"description": "Suite is green", "check": "pytest", "holds_before": False}
            ]
        )
        ruling = protocol.adjudicate(ToolCall.new("delete_path", {"path": "a.py"}), graph)
        assert ruling.verdict is Verdict.ABORT
        assert "baseline" in " ".join(ruling.reasons)

    def test_repeated_identical_reasoning_is_treated_as_a_loop(self) -> None:
        protocol = FrontierProtocol(strictness=1)
        graph = _valid_graph()
        call = ToolCall.new("delete_path", {"path": "a.py"})
        verdicts = [protocol.adjudicate(call, graph).verdict for _ in range(3)]
        assert verdicts[-1] is Verdict.ABORT

    def test_reads_are_never_gated(self) -> None:
        protocol = FrontierProtocol(strictness=1)
        ruling = protocol.adjudicate(ToolCall.new("read_file", {"path": "a.py"}), None)
        assert ruling.verdict is Verdict.PROCEED

    def test_strictness_two_gates_plain_mutations(self) -> None:
        protocol = FrontierProtocol(strictness=2)
        ruling = protocol.adjudicate(ToolCall.new("write_file", {"path": "a.py"}), None)
        assert ruling.verdict is Verdict.REVISE


class TestThoughtGraphParsing:
    def test_rejects_invalid_json_with_an_actionable_reason(self) -> None:
        result = FrontierProtocol.parse_thought_graph("sure thing: {not json}")
        assert isinstance(result, str) and "not valid JSON" in result

    def test_names_the_offending_field(self) -> None:
        result = FrontierProtocol.parse_thought_graph('```json\n{"goal":"x","action_summary":"y"}\n```')
        assert isinstance(result, str) and "goal" in result

    def test_accepts_a_fenced_graph(self) -> None:
        body = json.dumps(_valid_graph().model_dump())
        result = FrontierProtocol.parse_thought_graph(f"thinking...\n```json\n{body}\n```")
        assert isinstance(result, ThoughtGraph)

    def test_verified_assumption_must_cite_evidence(self) -> None:
        result = FrontierProtocol.parse_thought_graph(
            json.dumps(
                {
                    **_valid_graph().model_dump(),
                    "assumptions": [{"statement": "The adapter is unused", "verified": True}],
                }
            )
        )
        assert isinstance(result, str) and "evidence" in result


class TestThinkingBudget:
    def test_architecture_gets_more_than_verification(self) -> None:
        protocol = FrontierProtocol(thinking_ceiling=32_000)
        assert protocol.thinking_budget(Phase.ARCHITECTURE) > protocol.thinking_budget(
            Phase.VERIFICATION
        ) * 10

    def test_budget_is_clamped_to_model_output_limit(self) -> None:
        protocol = FrontierProtocol(thinking_ceiling=100_000)
        assert protocol.thinking_budget(Phase.ARCHITECTURE, max_output_tokens=4096) <= 3072


class TestFailureAnalysis:
    def test_finds_the_innermost_project_frame(self) -> None:
        traceback = (
            'Traceback (most recent call last):\n'
            '  File "/repo/src/app.py", line 42, in handler\n'
            '    return compute(payload)\n'
            '  File "/usr/lib/python3.13/json/__init__.py", line 346, in loads\n'
            '    return _default_decoder.decode(s)\n'
            '  File "/repo/src/core/calc.py", line 88, in compute\n'
            '    return data["total"] / data["count"]\n'
            'ZeroDivisionError: division by zero'
        )
        analysis = analyse_failure(traceback, project_root="/repo", exit_code=1)
        assert analysis.exception_type == "ZeroDivisionError"
        assert analysis.culprit == ("/repo/src/core/calc.py", 88, "compute")
        # The stdlib frame must not be blamed.
        assert all("site-packages" not in frame[0] for frame in analysis.project_frames)


class TestPatchVerification:
    @pytest.mark.parametrize(
        ("path", "content", "ok"),
        [
            ("a.py", "def f():\n    return 1", True),
            ("a.py", "def f(:\n  pass", False),
            ("a.json", '{"a": 1}', True),
            ("a.json", '{"a":}', False),
            ("a.ts", "function x() { return 1; }", True),
            ("a.ts", "function x() { if (a) {", False),
        ],
    )
    def test_catches_broken_content_before_it_is_written(
        self, path: str, content: str, ok: bool
    ) -> None:
        assert verify_patch_syntax(path, content).ok is ok


class TestContextAssembler:
    def test_stable_content_is_ordered_ahead_of_volatile(self) -> None:
        assembled = (
            ContextAssembler()
            .add("volatile_tail", "x" * 4000, volatility="volatile")
            .add("system", "y" * 8000, volatility="stable")
            .add("plan", "z" * 4000, volatility="semi")
            .assemble()
        )
        order = [segment.volatility for segment in assembled.segments]
        assert order == sorted(order, key=lambda item: {"stable": 0, "semi": 1, "volatile": 2}[item])
        assert assembled.cache_ratio > 0.5
        assert assembled.breakpoints >= 1


# --------------------------------------------------------------------------- #
# long-horizon engine
# --------------------------------------------------------------------------- #
class _ScriptedExecutor:
    """Executor driven by a per-node script, for deterministic engine tests."""

    def __init__(self, outcomes: dict[str, NodeOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    async def execute(self, node: MissionNode, mission: object) -> NodeOutcome:
        self.calls.append(node.key)
        return self.outcomes.get(node.key, NodeOutcome(ok=True, summary=f"{node.key} done"))


class _FileWritingExecutor:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []

    async def execute(self, node: MissionNode, mission: object) -> NodeOutcome:
        self.calls.append(node.key)
        (self.root / f"{node.key}.txt").write_text(f"output of {node.key}\n", encoding="utf-8")
        return NodeOutcome(ok=True, summary="done", touched_files=(f"{node.key}.txt",))


class TestMissionDag:
    def test_rejects_a_cycle_before_spending_anything(self, tmp_path: Path) -> None:
        engine = LongHorizonEngine(project_root=tmp_path, install_signal_handlers=False)
        with pytest.raises(EngineError, match="cycle"):
            engine.start_mission(
                "cyclic",
                [
                    MissionNode(key="a", title="A", depends_on=("c",)),
                    MissionNode(key="b", title="B", depends_on=("a",)),
                    MissionNode(key="c", title="C", depends_on=("b",)),
                ],
            )
        engine.close()

    def test_rejects_an_unknown_dependency(self, tmp_path: Path) -> None:
        engine = LongHorizonEngine(project_root=tmp_path, install_signal_handlers=False)
        with pytest.raises(EngineError, match="unknown node"):
            engine.start_mission("ghost", [MissionNode(key="a", title="A", depends_on=("nope",))])
        engine.close()

    def test_respects_dependency_order(self, tmp_path: Path) -> None:
        executor = _ScriptedExecutor()
        engine = LongHorizonEngine(
            project_root=tmp_path, executor=executor, max_parallel=1, install_signal_handlers=False
        )
        engine.start_mission(
            "ordered",
            [
                MissionNode(key="a", title="A"),
                MissionNode(key="b", title="B", depends_on=("a",)),
                MissionNode(key="c", title="C", depends_on=("b",)),
            ],
        )
        mission = asyncio.run(engine.run())
        assert executor.calls == ["a", "b", "c"]
        assert mission.status is MissionStatus.COMPLETED
        engine.close()


class TestCrashRecovery:
    def test_running_node_is_reopened_and_consolidated_work_is_skipped(
        self, tmp_path: Path
    ) -> None:
        executor = _FileWritingExecutor(tmp_path)
        engine = LongHorizonEngine(
            project_root=tmp_path, executor=executor, max_parallel=1, install_signal_handlers=False
        )
        mission = engine.start_mission(
            "resumable",
            [
                MissionNode(key="a", title="A"),
                MissionNode(key="b", title="B", depends_on=("a",)),
            ],
        )
        asyncio.run(engine.run())
        engine.close()

        # Simulate the process dying inside node b.
        db = tmp_path / ".unknown" / "memory" / "memory.db"
        connection = sqlite3.connect(str(db))
        connection.execute(
            "UPDATE mission_nodes SET status='running' WHERE mission_id=? AND key='b'",
            (mission.id,),
        )
        connection.commit()
        connection.close()

        second = _FileWritingExecutor(tmp_path)
        engine2 = LongHorizonEngine(
            project_root=tmp_path, executor=second, max_parallel=1, install_signal_handlers=False
        )
        report = engine2.resume_mission(mission.id)

        assert report.reopened_crashed == ["b"]
        assert report.skipped_consolidated == ["a"]
        asyncio.run(engine2.run())
        # 'a' was never re-run: its work was verified on disk instead.
        assert second.calls == ["b"]
        engine2.close()

    def test_drifted_output_reopens_its_node(self, tmp_path: Path) -> None:
        executor = _FileWritingExecutor(tmp_path)
        engine = LongHorizonEngine(
            project_root=tmp_path, executor=executor, max_parallel=1, install_signal_handlers=False
        )
        mission = engine.start_mission("drift", [MissionNode(key="a", title="A")])
        asyncio.run(engine.run())
        engine.close()

        (tmp_path / "a.txt").write_text("a human changed this while we were down\n", encoding="utf-8")

        engine2 = LongHorizonEngine(
            project_root=tmp_path,
            executor=_FileWritingExecutor(tmp_path),
            install_signal_handlers=False,
        )
        report = engine2.resume_mission(mission.id)
        assert report.reopened_drifted == ["a"]
        assert not report.drift.clean
        engine2.close()

    def test_refuses_to_steal_a_mission_from_a_live_owner(self, tmp_path: Path) -> None:
        engine = LongHorizonEngine(project_root=tmp_path, install_signal_handlers=False)
        mission = engine.start_mission("owned", [MissionNode(key="a", title="A")])
        engine.store.beat(mission.id, pid=1, phase="node:a")  # pid 1 always exists
        engine.close()

        engine2 = LongHorizonEngine(project_root=tmp_path, install_signal_handlers=False)
        with pytest.raises(EngineError, match="still owned by live pid"):
            engine2.resume_mission(mission.id)
        engine2.close()

    def test_resumes_when_the_previous_owner_is_gone(self, tmp_path: Path) -> None:
        engine = LongHorizonEngine(project_root=tmp_path, install_signal_handlers=False)
        mission = engine.start_mission("orphan", [MissionNode(key="a", title="A")])
        engine.store.beat(mission.id, pid=999_999, phase="node:a")
        engine.close()

        engine2 = LongHorizonEngine(project_root=tmp_path, install_signal_handlers=False)
        report = engine2.resume_mission(mission.id)
        assert report.resumed and report.previous_owner_pid == 999_999
        engine2.close()


class TestEntropyDetector:
    def test_identical_observations_are_flagged_as_stalled(self) -> None:
        detector = EntropyDetector(threshold=2)
        outcome = NodeOutcome(ok=False, error="same failure", signals={"tests": 0})
        verdicts = [detector.observe("n", outcome)[0] for _ in range(3)]
        assert verdicts == [
            EntropyVerdict.PROGRESSING,
            EntropyVerdict.REPEATING,
            EntropyVerdict.STALLED,
        ]

    def test_changing_observations_keep_progressing(self) -> None:
        detector = EntropyDetector(threshold=2)
        for index in range(4):
            verdict, _ = detector.observe(
                "n", NodeOutcome(ok=False, error=f"failure {index}", signals={"i": index})
            )
            assert verdict is EntropyVerdict.PROGRESSING

    def test_engine_freezes_a_looping_node_instead_of_burning_retries(
        self, tmp_path: Path
    ) -> None:
        executor = _ScriptedExecutor(
            {"loop": NodeOutcome(ok=False, error="ImportError: no module named foo")}
        )
        engine = LongHorizonEngine(
            project_root=tmp_path,
            executor=executor,
            max_parallel=1,
            entropy_threshold=2,
            install_signal_handlers=False,
        )
        engine.start_mission("stuck", [MissionNode(key="loop", title="Fix import", max_attempts=99)])
        mission = asyncio.run(engine.run())

        # 99 attempts were allowed; the detector stopped it after 3.
        assert len(executor.calls) == 3
        assert mission.nodes["loop"].status is NodeStatus.FROZEN
        engine.close()

    def test_replan_hook_routes_around_a_frozen_node(self, tmp_path: Path) -> None:
        executor = _ScriptedExecutor(
            {"orig": NodeOutcome(ok=False, error="always the same failure")}
        )
        engine = LongHorizonEngine(
            project_root=tmp_path,
            executor=executor,
            max_parallel=1,
            entropy_threshold=2,
            install_signal_handlers=False,
        )
        engine.set_replan_hook(
            lambda node, mission, history: [MissionNode(key="alt", title="Alternative path")]
        )
        engine.start_mission("replan", [MissionNode(key="orig", title="Original", max_attempts=99)])
        mission = asyncio.run(engine.run())

        assert "alt" in mission.nodes
        assert mission.nodes["alt"].status is NodeStatus.CONSOLIDATED
        assert mission.nodes["orig"].status is NodeStatus.SKIPPED
        assert mission.status is MissionStatus.COMPLETED
        engine.close()


class TestMissionStore:
    def test_uses_wal_so_readers_never_block_the_writer(self, tmp_path: Path) -> None:
        store = MissionStore(tmp_path / "m.db")
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        store.close()
        assert mode.lower() == "wal"

    def test_events_are_readable_with_a_cursor(self, tmp_path: Path) -> None:
        store = MissionStore(tmp_path / "m.db")
        engine_mission = MissionNode(key="a", title="A")
        engine = LongHorizonEngine(
            project_root=tmp_path, db_path=tmp_path / "m.db", install_signal_handlers=False
        )
        mission = engine.start_mission("events", [engine_mission])
        for index in range(5):
            engine.store.record_event(mission.id, "tick", node_key="a", index=index)

        first = store.events(mission.id, after_id=0, limit=3)
        second = store.events(mission.id, after_id=first[-1]["id"], limit=10)
        assert len(first) == 3
        assert all(event["id"] > first[-1]["id"] for event in second)
        engine.close()
        store.close()


# --------------------------------------------------------------------------- #
# AST tools
# --------------------------------------------------------------------------- #
SAMPLE = '''"""Sample module."""
import os

TOTAL = 0

def total(items):
    """Sum things. The word total appears in this docstring."""
    # total also appears in this comment
    result = 0
    for item in items:
        result += item
    return result


class Order:
    @property
    def process(self):
        """Order's process."""
        return "order"

    def ship(self):
        label = "do not touch this total string"
        return other.total + label


class Invoice:
    def process(self):
        """Invoice's process - must not change."""
        return "invoice"
'''


@pytest.fixture()
def ast_ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "sample.py").write_text(SAMPLE, encoding="utf-8")
    loaded = load_settings(cwd=tmp_path)
    return ToolContext(
        settings=loaded.settings,
        paths=loaded.paths,
        project_root=tmp_path,
        events=EventBus(),
    )


class TestAstTools:
    def test_outline_lists_qualified_symbols(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstOutlineTool()
        result = asyncio.run(tool.run(ast_ctx, path="sample.py"))
        names = {item["qualname"] for item in result.data["symbols"]}
        assert {"total", "Order", "Order.process", "Invoice.process"} <= names

    def test_ambiguous_symbol_is_refused_not_guessed(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstReplaceSymbolTool()
        result = asyncio.run(
            tool.run(ast_ctx, path="sample.py", symbol="process", code="def process(self): ...")
        )
        assert not result.ok
        assert "ambiguous" in (result.error or "")
        assert "Order.process" in (result.error or "")

    def test_replace_touches_only_the_qualified_target(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstReplaceSymbolTool()
        asyncio.run(
            tool.run(
                ast_ctx,
                path="sample.py",
                symbol="Order.process",
                code='@property\ndef process(self):\n    return "order-v2"',
            )
        )
        text = (ast_ctx.project_root / "sample.py").read_text(encoding="utf-8")
        assert "order-v2" in text
        assert "Invoice's process - must not change" in text

    def test_rename_ignores_strings_comments_and_attributes(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstRenameSymbolTool()
        result = asyncio.run(
            tool.run(ast_ctx, path="sample.py", old_name="total", new_name="sumar")
        )
        text = (ast_ctx.project_root / "sample.py").read_text(encoding="utf-8")

        assert result.ok
        assert "def sumar(items):" in text
        # Everything a naive replace would have destroyed:
        assert "The word total appears in this docstring" in text
        assert "# total also appears in this comment" in text
        assert "do not touch this total string" in text
        assert "other.total" in text
        assert "TOTAL = 0" in text

    def test_broken_replacement_is_rejected_and_file_untouched(
        self, ast_ctx: ToolContext
    ) -> None:
        path = ast_ctx.project_root / "sample.py"
        before = path.read_text(encoding="utf-8")
        tool = ast_tools.AstReplaceSymbolTool()
        result = asyncio.run(
            tool.run(ast_ctx, path="sample.py", symbol="total", code="def total(:\n  broken(")
        )
        assert not result.ok
        assert "would not parse" in (result.error or "")
        assert path.read_text(encoding="utf-8") == before

    def test_insert_lands_at_class_indentation(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstInsertSymbolTool()
        result = asyncio.run(
            tool.run(
                ast_ctx,
                path="sample.py",
                into_class="Invoice",
                code="def audit(self):\n    return True",
            )
        )
        text = (ast_ctx.project_root / "sample.py").read_text(encoding="utf-8")
        assert result.ok
        assert "\n    def audit(self):" in text

    def test_insert_refuses_to_shadow_without_permission(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstInsertSymbolTool()
        result = asyncio.run(
            tool.run(ast_ctx, path="sample.py", code="def total(items):\n    return 0")
        )
        assert not result.ok and "already exists" in (result.error or "")

    def test_add_import_is_idempotent(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstAddImportTool()
        first = asyncio.run(
            tool.run(ast_ctx, path="sample.py", statement="from pathlib import Path")
        )
        second = asyncio.run(
            tool.run(ast_ctx, path="sample.py", statement="from pathlib import Path")
        )
        text = (ast_ctx.project_root / "sample.py").read_text(encoding="utf-8")
        assert first.ok and second.ok
        assert text.count("from pathlib import Path") == 1

    def test_remove_deletes_the_whole_declaration(self, ast_ctx: ToolContext) -> None:
        tool = ast_tools.AstRemoveSymbolTool()
        result = asyncio.run(tool.run(ast_ctx, path="sample.py", symbol="Invoice.process"))
        text = (ast_ctx.project_root / "sample.py").read_text(encoding="utf-8")
        assert result.ok
        assert "Invoice's process" not in text
        assert "class Invoice:" in text


class TestTsStructureIndex:
    def test_braces_inside_strings_do_not_confuse_the_scanner(self) -> None:
        source = (
            "class Widget {\n"
            '  render() {\n'
            '    const template = "} not a real close {";\n'
            "    return template;\n"
            "  }\n"
            "}\n"
            "function after() { return 1; }\n"
        )
        index = ast_tools.TsStructureIndex(source)
        widget = index.find("Widget")
        after = index.find("after")
        assert widget is not None and after is not None
        # Widget must end at its real closing brace, not the one in the string.
        assert widget.end_line == 6
        assert after.start_line == 7


# --------------------------------------------------------------------------- #
# test runner
# --------------------------------------------------------------------------- #
PYTEST_OUTPUT = """\
=================================== FAILURES ===================================
________________________________ test_division _________________________________

    def test_division():
        data = {"total": 10, "count": 0}
>       assert data["total"] / data["count"] == 5
E       ZeroDivisionError: division by zero

tests/test_demo.py:6: ZeroDivisionError
=========================== short test summary info ============================
FAILED tests/test_demo.py::test_division - ZeroDivisionError: division by zero
1 failed, 3 passed in 0.42s
"""


class TestPytestParsing:
    def test_extracts_counts_from_a_quiet_run(self) -> None:
        summary, _ = test_runner_tools.parse_pytest_output(PYTEST_OUTPUT)
        assert summary.passed == 3
        assert summary.failed == 1
        assert summary.duration_s == pytest.approx(0.42)
        assert not summary.green

    def test_reports_one_record_per_failing_test(self) -> None:
        _, failures = test_runner_tools.parse_pytest_output(PYTEST_OUTPUT)
        assert len(failures) == 1
        failure = failures[0]
        assert failure.test_id == "tests/test_demo.py::test_division"
        assert failure.file == "tests/test_demo.py"
        assert failure.line == 6
        assert failure.exception_type == "ZeroDivisionError"

    def test_digest_does_not_repeat_the_exception_type(self) -> None:
        _, failures = test_runner_tools.parse_pytest_output(PYTEST_OUTPUT)
        assert failures[0].digest().count("ZeroDivisionError") == 1


class TestFrameworkDetection:
    def test_detects_pytest_from_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths=['tests']\n", encoding="utf-8"
        )
        framework = test_runner_tools.detect_framework(tmp_path)
        assert framework.name == "pytest" and "pyproject" in framework.evidence

    def test_detects_vitest_from_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"vitest": "^2"}}), encoding="utf-8"
        )
        framework = test_runner_tools.detect_framework(tmp_path)
        assert framework.name == "vitest"

    def test_says_when_it_is_guessing(self, tmp_path: Path) -> None:
        framework = test_runner_tools.detect_framework(tmp_path)
        assert "no conclusive evidence" in framework.evidence


class TestBackgroundExecution:
    def test_runs_a_real_suite_and_reports_structured_failures(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8"
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_demo.py").write_text(
            "def test_ok():\n    assert True\n\n"
            "def test_bad():\n    assert 1 == 2, 'numbers differ'\n",
            encoding="utf-8",
        )
        loaded = load_settings(cwd=tmp_path)
        ctx = ToolContext(
            settings=loaded.settings,
            paths=loaded.paths,
            project_root=tmp_path,
            events=EventBus(),
        )

        async def scenario() -> None:
            runner = test_runner_tools.TestRunBackgroundTool()
            report = test_runner_tools.TestReportTool()
            started = await runner.run(ctx, extra_args="-p no:cacheprovider", wait=True)
            assert not started.ok  # a failing suite is a failed tool call
            assert started.data["summary"]["passed"] == 1
            assert started.data["summary"]["failed"] == 1

            detail = await report.run(ctx, group_by="file")
            assert detail.ok
            assert detail.data["files_affected"] == ["tests/test_demo.py"]

        asyncio.run(scenario())

    def test_missing_binary_is_reported_not_raised(self, tmp_path: Path) -> None:
        loaded = load_settings(cwd=tmp_path)
        ctx = ToolContext(
            settings=loaded.settings,
            paths=loaded.paths,
            project_root=tmp_path,
            events=EventBus(),
        )

        async def scenario() -> None:
            registry = test_runner_tools.TestSessionRegistry.for_context(ctx)
            job = await registry.start(
                command=["definitely-not-a-real-binary-xyz"],
                cwd=tmp_path,
                framework="fake",
                parser="generic",
                timeout_s=5.0,
            )
            assert job.state is test_runner_tools.JobState.ERROR
            assert "not installed" in job.error

        asyncio.run(scenario())
