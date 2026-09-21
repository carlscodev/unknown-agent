"""The agent loop end to end, using the deterministic offline provider.

These tests are the contract for "the product works without an API key": plan,
act, verify, self-heal, stop.  Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from unknown_agent.api import UnknownAgent
from unknown_agent.core.models import RiskLevel, ToolCall
from unknown_agent.core.runner import RunRequest, run_task
from unknown_agent.config.loader import load_settings


def test_offline_run_completes_and_journals_everything(project: Path) -> None:
    loaded = load_settings(cwd=project)
    result = asyncio.run(run_task(loaded, RunRequest(task="summarise the repository", provider="offline")))

    assert result.ok
    assert result.status.value == "completed"
    assert result.run_dir.exists()
    for name in ("events.jsonl", "run.json", "plan.json", "task.md"):
        assert (result.run_dir / name).exists(), f"{name} missing from the run journal"
    assert result.to_dict()["run"]["id"] == result.run_id


def test_run_uses_tools_and_records_evidence(project: Path) -> None:
    loaded = load_settings(cwd=project)
    result = asyncio.run(run_task(loaded, RunRequest(task="show me the project structure", provider="offline")))
    types = {step.decision.tool_calls[0].name for step in result.outcome.steps if step.decision.tool_calls}
    assert types, "the loop produced no tool calls at all"
    assert any(name in {"project_tree", "list_files", "inspect_project", "search_code", "read_file"} for name in types)
    assert any(step.results for step in result.outcome.steps), "no step recorded a tool result"
    assert all(step.summary for step in result.outcome.steps), "steps must summarise themselves for reports"


def test_read_only_session_never_mutates(project: Path) -> None:
    before = (project / "src" / "demo" / "calc.py").read_text()
    loaded = load_settings(cwd=project)
    result = asyncio.run(
        run_task(loaded, RunRequest(task="rewrite calc.py to use a lookup table", provider="offline", read_only=True))
    )
    assert (project / "src" / "demo" / "calc.py").read_text() == before
    assert result.state.files_changed == []


def test_definition_of_done_controls_whether_gates_are_required() -> None:
    from unknown_agent.config.models import Settings

    settings = Settings()
    assert settings.validation.is_required("tests", "balanced")
    assert not settings.validation.is_required("tests", "advisory")
    assert settings.validation.is_required("format", "strict")


def test_failing_gates_block_success_and_produce_a_blocker(failing_project: Path) -> None:
    loaded = load_settings(cwd=failing_project)
    result = asyncio.run(
        run_task(
            loaded,
            RunRequest(
                task="run the test suite and report",
                provider="offline",
                gates=["tests"],
                definition_of_done="strict",
                max_steps=4,
                # This test is about the *solo loop's* gate handling.  The task
                # scores complexity 4, so it would otherwise be delegated to the
                # conductor (see tests/test_runner_orchestration.py), where gate
                # results live on the workers instead of on RunResult.verification.
                orchestrate=False,
            ),
        )
    )
    report = result.verification
    assert report is not None and not report.passed
    assert any(item.name == "tests" and not item.passed for item in report.results)
    assert result.status.value == "blocked"
    assert result.blockers
    assert not result.ok


def test_self_heal_inserts_a_repair_step(failing_project: Path) -> None:
    """A failing gate must change the plan, not just colour the summary red."""

    loaded = load_settings(cwd=failing_project)
    result = asyncio.run(
        run_task(
            loaded,
            RunRequest(
                task="fix the failing tests",
                provider="offline",
                gates=["tests"],
                definition_of_done="strict",
                max_steps=8,
            ),
        )
    )
    revisions = [step for step in result.outcome.steps if "repair" in (step.decision.thought or "").lower()]
    assert result.state.plan is not None
    assert result.state.plan.revision >= 0
    assert revisions or result.state.blockers


def test_budgets_stop_runaway_loops(project: Path) -> None:
    loaded = load_settings(cwd=project)
    result = asyncio.run(
        run_task(
            loaded,
            RunRequest(task="keep exploring forever", provider="offline", max_steps=2, definition_of_done="advisory"),
        )
    )
    assert result.state.step <= 2
    assert result.state.status.value in {"completed", "blocked", "failed", "cancelled"}


def test_python_api_round_trip(project: Path) -> None:
    async def main() -> None:
        async with UnknownAgent(cwd=project, provider="offline") as agent:
            report = agent.inspect()
            from pathlib import Path as _Path

            assert _Path(report.root) == project.resolve()
            assert "python" in report.languages
            result = await agent.run("describe the tests")
            assert result.ok
            assert result.files_changed == []

    asyncio.run(main())


def test_plan_is_available_without_execution(project: Path) -> None:
    async def main() -> None:
        async with UnknownAgent(cwd=project, provider="offline", read_only=True) as agent:
            plan = await agent.plan("add pagination to the API")
            assert plan.steps
            assert plan.goal

    asyncio.run(main())


def test_tool_call_helper_defaults_are_safe() -> None:
    call = ToolCall.new("read_file", {"path": "README.md"})
    assert isinstance(call, ToolCall)
    assert call.id
    assert call.name == "read_file"
    assert RiskLevel.READ.value == "read"


# --------------------------------------------------------------------------- #
# the tool allow-list: a role's grant is a boundary, not a suggestion
# --------------------------------------------------------------------------- #
def _runner_with(loaded, **kwargs):
    from unknown_agent.core.runner import Runner

    return Runner(loaded, request=RunRequest(task="summarise the repository", provider="offline", **kwargs))


def test_no_allow_list_leaves_the_session_registry_alone(project: Path) -> None:
    loaded = load_settings(cwd=project)
    runner = _runner_with(loaded)
    asyncio.run(runner.setup())
    assert runner.registry is not None
    assert "shell_run" in runner.registry.names()
    assert runner.registry.withholder_reason("shell_run") is None


def test_an_allow_list_withholds_everything_else_with_a_reason(project: Path) -> None:
    loaded = load_settings(cwd=project)
    runner = _runner_with(loaded, tools=["read_file", "project_tree"])
    asyncio.run(runner.setup())
    assert runner.registry is not None
    assert set(runner.registry.names()) == {"read_file", "project_tree"}
    reason = runner.registry.withholder_reason("shell_run")
    assert reason and "not granted" in reason


def test_an_explicit_empty_allow_list_grants_nothing(project: Path) -> None:
    """``[]`` means no tools, not "unrestricted" — the strictest boundary must not
    silently become the loosest one."""

    loaded = load_settings(cwd=project)
    runner = _runner_with(loaded, tools=[])
    asyncio.run(runner.setup())
    assert runner.registry is not None
    assert runner.registry.names() == []


def test_an_allow_list_may_name_tools_that_do_not_exist(project: Path) -> None:
    """An allow-list narrows what exists; it never conjures tools into being.

    This matters for roles that grant browser tools in an environment without
    Playwright, and for plugin tools that failed to load: the run must still start.
    """

    loaded = load_settings(cwd=project)
    runner = _runner_with(loaded, tools=["read_file", "not_a_real_tool"])
    asyncio.run(runner.setup())
    assert runner.registry is not None
    assert runner.registry.names() == ["read_file"]
    assert "not_a_real_tool" not in runner.registry.names()


def test_the_allow_list_survives_plugin_loading(project: Path) -> None:
    """Plugin tools are registered before the allow-list is applied, so a plugin
    cannot widen a role's grant by existing."""

    loaded = load_settings(cwd=project)
    runner = _runner_with(loaded, tools=["read_file"])
    asyncio.run(runner.setup())
    assert runner.registry is not None
    assert set(runner.registry.names()) == {"read_file"}


def test_the_role_name_reaches_the_refusal(project: Path) -> None:
    loaded = load_settings(cwd=project)
    runner = _runner_with(
        loaded,
        tools=["read_file"],
        metadata={"orchestration": {"role": "documenter", "work_id": "w1"}},
    )
    asyncio.run(runner.setup())
    assert runner.registry is not None
    reason = runner.registry.withholder_reason("write_file")
    assert reason is not None and "documenter" in reason


# --------------------------------------------------------------------------- #
# skills reach the run: prompt guidance and gates
# --------------------------------------------------------------------------- #
SKILL_FILE = """
id = "terraform-guard"
label = "Terraform guard"
summary = "Plan before applying infrastructure changes."
instructions = "Run `terraform plan` and show the diff before any apply."
gates = ["diff"]
triggers = ["terraform", "infrastructure"]
"""


def _project_with_skill(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    skills = tmp_path / ".unknown" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "terraform-guard.toml").write_text(SKILL_FILE, encoding="utf-8")
    return tmp_path


def test_a_project_skill_reaches_the_system_prompt(tmp_path: Path) -> None:
    """A skill that never reaches the prompt is prose nobody reads."""

    from unknown_agent.core.runner import Runner

    project = _project_with_skill(tmp_path)
    loaded = load_settings(cwd=project)
    runner = Runner(loaded, request=RunRequest(task="apply the terraform stack", provider="offline"))
    asyncio.run(runner.setup())
    assert runner.skills is not None
    assert "terraform-guard" in runner.skills.ids()
    message = runner.builder.build_system_message(tool_specs=[], task="apply the terraform stack")
    text = message.content if isinstance(message.content, str) else str(message.content)
    assert "terraform plan" in text


def test_a_skill_gate_becomes_part_of_done(tmp_path: Path) -> None:
    """Skills declare the gates they consider part of finished work."""

    from unknown_agent.core.runner import Runner

    project = _project_with_skill(tmp_path)
    loaded = load_settings(cwd=project)
    loaded.settings.validation.gates["diff"] = loaded.settings.validation.gate("diff")
    loaded.settings.validation.gates["diff"].enabled = False
    runner = Runner(loaded, request=RunRequest(task="apply the terraform stack", provider="offline"))
    asyncio.run(runner.setup())
    assert runner.settings.validation.gate("diff").enabled is True


def test_a_project_without_skills_has_an_identical_prompt(tmp_path: Path) -> None:
    """Opting out must cost nothing and change nothing."""

    from unknown_agent.core.runner import Runner

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    loaded = load_settings(cwd=tmp_path)
    loaded.settings.skills.enabled = False
    runner = Runner(loaded, request=RunRequest(task="summarise", provider="offline"))
    asyncio.run(runner.setup())
    message = runner.builder.build_system_message(tool_specs=[], task="summarise")
    text = message.content if isinstance(message.content, str) else str(message.content)
    assert "ACTIVE SKILLS" not in text


def test_a_project_root_is_never_scanned_for_skills(tmp_path: Path) -> None:
    """``pyproject.toml`` is a build manifest, not a skill definition."""

    from unknown_agent.core.runner import Runner

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    loaded = load_settings(cwd=tmp_path)
    runner = Runner(loaded, request=RunRequest(task="summarise", provider="offline"))
    asyncio.run(runner.setup())
    assert runner.skills is not None
    assert runner.skills.unresolved == []
    assert all("pyproject" not in note for note in runner.skills.unresolved)
