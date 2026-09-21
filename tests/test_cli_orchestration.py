"""``ua orchestrate``: the product surface for multi-worker delegation.

These tests exist because the bugs they cover were found by *running* the command,
not by the unit tests: a plan that called itself "solo" while dispatching four
workers, a budget trim that kept the planner and dropped the coder, and unbalanced
Rich markup that raised at print time.  A layer that is only tested through its
Python API keeps those failures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app

runner = CliRunner()

CHECKOUT = "build a checkout flow with cart, stripe payments, responsive UI, docs and release notes"


@pytest.fixture(autouse=True)
def _cwd(cli_env: None) -> None:
    """Every command runs inside the generated project."""


def invoke(*args: str, expect: int | None = 0) -> str:
    result = runner.invoke(app, list(args))
    if expect is not None:
        assert result.exit_code == expect, f"`ua {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
    return result.output


def ledgers(project: Path) -> list[Path]:
    return sorted((project / ".unknown" / "orchestration").glob("*.jsonl"))


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def test_the_group_is_reachable_from_the_top_level() -> None:
    output = invoke("--help")
    assert "orchestrate" in output


def test_the_group_lists_its_commands() -> None:
    output = invoke("orchestrate", "--help")
    for command in ("plan", "run", "roles", "list", "show", "ledger", "worker"):
        assert command in output


def test_the_group_without_a_command_shows_help_not_a_crash() -> None:
    """Typer's convention for a group with no default action: usage, exit 2."""

    result = runner.invoke(app, ["orchestrate"])
    assert result.exit_code == 2
    assert "Usage:" in result.output
    assert "plan" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_plan_shows_the_shape_the_workers_and_why() -> None:
    output = invoke("orchestrate", "plan", CHECKOUT, "--max-workers", "6")
    assert "orchestration plan" in output
    assert "Code generator" in output
    assert "greenfield" in output or "graph" in output


def test_plan_delegates_nothing_and_writes_no_ledger(project: Path) -> None:
    invoke("orchestrate", "plan", CHECKOUT, "--max-workers", "6")
    assert ledgers(project) == [], "a preview must not leave an audit file behind"


def test_plan_reports_what_the_budget_trimmed() -> None:
    output = invoke("orchestrate", "plan", CHECKOUT, "--max-workers", "2")
    assert "dropped" in output or "collapsed" in output


def test_plan_acceptance_criteria_are_opt_in() -> None:
    plain = invoke("orchestrate", "plan", CHECKOUT, "--max-workers", "6")
    detailed = invoke("orchestrate", "plan", CHECKOUT, "--max-workers", "6", "--acceptance")
    assert len(detailed) >= len(plain)


def test_plan_json_is_machine_readable() -> None:
    payload = json.loads(invoke("--json", "orchestrate", "plan", CHECKOUT, "--max-workers", "6"))
    assert payload["pattern"]
    assert payload["rationale"]
    assert payload["graph"]["items"]


def test_a_forced_shape_is_honoured() -> None:
    payload = json.loads(invoke("--json", "orchestrate", "plan", CHECKOUT, "--pattern", "solo"))
    assert payload["pattern"] == "solo"


def test_a_tight_budget_keeps_the_worker_that_does_the_work() -> None:
    """The trim that kept planner+inspector and dropped the coder produced nothing."""

    payload = json.loads(invoke("--json", "orchestrate", "plan", CHECKOUT, "--max-workers", "2"))
    roles = {item["role"] for item in payload["graph"]["workers"]} if "workers" in payload["graph"] else set()
    if not roles:  # the graph payload reports a count; fall back to the human table
        output = invoke("orchestrate", "plan", CHECKOUT, "--max-workers", "2")
        assert "Code generator" in output
    else:
        assert "coder" in roles


def test_solo_really_means_one_worker() -> None:
    output = invoke("orchestrate", "plan", CHECKOUT, "--max-workers", "1")
    assert "solo" in output
    assert "collapsed to one worker" in output


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def test_run_completes_offline_and_leaves_an_audit_trail(project: Path) -> None:
    output = invoke("orchestrate", "run", CHECKOUT, "--max-workers", "4", "--executor", "offline")
    assert "orchestration completed" in output
    files = ledgers(project)
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    kinds = {line["kind"] for line in lines}
    assert {"run_started", "work_created", "worker_finished", "run_finished"} <= kinds


def test_run_says_when_gates_were_not_verified() -> None:
    output = invoke("orchestrate", "run", CHECKOUT, "--max-workers", "4", "--executor", "offline")
    assert "not verified" in output


def test_run_dry_run_writes_nothing(project: Path) -> None:
    invoke("orchestrate", "run", CHECKOUT, "--max-workers", "4", "--dry-run", "--executor", "offline")
    assert ledgers(project) == []


def test_run_json_carries_the_whole_outcome() -> None:
    payload = json.loads(
        invoke("--json", "orchestrate", "run", CHECKOUT, "--max-workers", "4", "--executor", "offline")
    )
    assert payload["status"] == "completed"
    assert payload["ok"] is True
    assert payload["results"]
    assert "unverified" in payload
    assert payload["budget"]["max_workers"] >= 1


def test_an_unknown_executor_is_a_usage_error() -> None:
    result = runner.invoke(app, ["orchestrate", "run", CHECKOUT, "--executor", "magic"])
    assert result.exit_code != 0


def test_a_question_runs_solo_and_read_only() -> None:
    payload = json.loads(
        invoke("--json", "orchestrate", "run", "what does this repo do?", "--executor", "offline")
    )
    assert payload["pattern"] == "solo"


def test_the_real_executor_is_the_default() -> None:
    """`ua orchestrate run` does real work unless you ask it not to."""

    output = invoke("orchestrate", "run", "--help")
    assert "agent" in output
    assert "--executor" in output


def test_a_provider_failure_is_contained_per_worker(project: Path) -> None:
    """No credentials must produce a clear blocked run, never a stack trace."""

    result = runner.invoke(
        app,
        ["--json", "--provider", "offline", "orchestrate", "run", CHECKOUT, "--max-workers", "2"],
    )
    assert result.exit_code in (0, 3, 8), result.output
    assert "Traceback" not in result.output
    # CliRunner mixes stderr into ``output``; the JSON payload starts at the first
    # brace.  (In a real shell the payload is stdout-only and warnings are stderr.)
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["status"] in {"completed", "blocked", "cancelled"}
    if not payload["ok"]:
        assert payload["blockers"], "a blocked run must say what blocked it"


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #
def test_roles_lists_every_specialist() -> None:
    output = invoke("orchestrate", "roles")
    for label in ("Planner", "Code generator", "Verifier", "Release"):
        assert label in output


def test_roles_json_is_ordered_by_stage() -> None:
    payload = json.loads(invoke("--json", "orchestrate", "roles"))
    stages = [entry["stage"] for entry in payload["roles"]]
    assert stages == sorted(stages)
    assert len(payload["roles"]) == 10


def test_one_role_shows_its_boundaries() -> None:
    output = invoke("orchestrate", "roles", "coder")
    assert "mission" in output.lower()
    assert "syntax" in output and "tests" in output


def test_an_unknown_role_is_a_usage_error() -> None:
    output = invoke("orchestrate", "roles", "not-a-role", expect=2)
    assert "unknown role" in output


# --------------------------------------------------------------------------- #
# audit trail
# --------------------------------------------------------------------------- #
def test_list_is_empty_before_any_run(project: Path) -> None:
    output = invoke("orchestrate", "list")
    assert "No orchestration runs" in output


def test_list_show_and_ledger_read_back_a_finished_run(project: Path) -> None:
    invoke("orchestrate", "run", CHECKOUT, "--max-workers", "4", "--executor", "offline")
    listing = invoke("orchestrate", "list")
    assert "orc-" in listing

    shown = invoke("orchestrate", "show")
    assert "run_started" in shown

    raw = invoke("orchestrate", "ledger", "--kind", "run_finished")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert events and events[0]["kind"] == "run_finished"


def test_show_an_unknown_run_is_a_usage_error(project: Path) -> None:
    invoke("orchestrate", "run", CHECKOUT, "--max-workers", "4", "--executor", "offline")
    output = invoke("orchestrate", "show", "orc-does-not-exist", expect=2)
    assert "no run" in output.lower()


def test_ledger_with_no_runs_explains_itself(project: Path) -> None:
    output = invoke("orchestrate", "ledger", expect=2)
    assert "no orchestration runs" in output.lower()


# --------------------------------------------------------------------------- #
# single worker
# --------------------------------------------------------------------------- #
def test_worker_runs_one_specialist_in_isolation() -> None:
    output = invoke("orchestrate", "worker", "inspector", "map this repository", "--executor", "offline")
    assert "Repo inspector" in output
    assert "denied" in output.lower()


def test_worker_refuses_a_role_that_cannot_touch_the_tree() -> None:
    """The point of the panel: show the grant *and* the denial."""

    output = invoke("orchestrate", "worker", "inspector", "map this repository", "--executor", "offline")
    assert "write_file" in output


def test_worker_json_reports_the_prompt_and_the_result() -> None:
    payload = json.loads(
        invoke(
            "--json",
            "orchestrate",
            "worker",
            "coder",
            "add a healthcheck endpoint",
            "--executor",
            "offline",
        )
    )
    assert payload["role"] == "coder"
    assert payload["prompt_chars"] > 0
    assert payload["result"]["ok"] is False or payload["result"]["artifacts"]


def test_worker_with_an_unknown_role_is_a_usage_error() -> None:
    output = invoke("orchestrate", "worker", "wizard", "do magic", expect=2)
    assert "unknown role" in output
