from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from unknown_agent.benchmarks import BenchmarkRunner, BenchmarkSuite, Scenario


@pytest.mark.asyncio
async def test_benchmark_run_is_durable_and_checks_assertions(tmp_path: Path) -> None:
    suite = BenchmarkSuite(
        name="smoke",
        scenarios=(
            Scenario(
                name="python",
                command=(sys.executable, "-c", "print('ready')"),
                repetitions=2,
                warmups=1,
                stdout_contains=("ready",),
            ),
        ),
    )
    runner = BenchmarkRunner(tmp_path, tmp_path / "benchmarks.db")
    run = await runner.run(suite, revision="abc")
    assert run.revision == "abc"
    assert run.summaries[0].samples == 2
    assert run.summaries[0].pass_rate == 1
    restored = runner.get_run(run.id)
    assert restored.summaries[0].passed == 2


@pytest.mark.asyncio
async def test_timeout_and_failure_are_measured_not_hidden(tmp_path: Path) -> None:
    suite = BenchmarkSuite(
        name="failure",
        scenarios=(
            Scenario(
                name="timeout",
                command=(sys.executable, "-c", "import time; time.sleep(1)"),
                repetitions=1,
                warmups=0,
                timeout_seconds=0.05,
            ),
            Scenario(
                name="assertion",
                command=(sys.executable, "-c", "print('actual')"),
                repetitions=1,
                warmups=0,
                stdout_contains=("expected",),
            ),
        ),
    )
    run = await BenchmarkRunner(tmp_path, tmp_path / "benchmarks.db").run(suite)
    assert all(summary.pass_rate == 0 for summary in run.summaries)


@pytest.mark.asyncio
async def test_regression_comparison_detects_latency_and_reliability(tmp_path: Path) -> None:
    runner = BenchmarkRunner(tmp_path, tmp_path / "benchmarks.db")
    baseline_suite = BenchmarkSuite(
        name="comparison",
        scenarios=(
            Scenario(
                name="task",
                command=(sys.executable, "-c", "print('ok')"),
                repetitions=2,
                warmups=0,
            ),
        ),
    )
    candidate_suite = BenchmarkSuite(
        name="comparison",
        scenarios=(
            Scenario(
                name="task",
                command=(sys.executable, "-c", "import sys; sys.exit(1)"),
                repetitions=2,
                warmups=0,
            ),
        ),
    )
    baseline = await runner.run(baseline_suite)
    candidate = await runner.run(candidate_suite)
    report = runner.compare(baseline.id, candidate.id, threshold_percent=5)
    assert not report.passed
    assert any(item.metric == "pass_rate" and item.regressed for item in report.regressions)


def test_yaml_contract_and_bounds(tmp_path: Path) -> None:
    path = tmp_path / "suite.yaml"
    path.write_text(
        "name: quick\nscenarios:\n  - name: echo\n    command: [python, -c, \"print('ok')\"]\n"
    )
    assert BenchmarkSuite.from_yaml(path).scenarios[0].name == "echo"
    with pytest.raises(ValidationError):
        Scenario(name="bad", command=("echo",), timeout_seconds=301)
    with pytest.raises(ValidationError, match="unique"):
        BenchmarkSuite(
            name="duplicates",
            scenarios=(
                Scenario(name="same", command=("echo",)),
                Scenario(name="same", command=("echo",)),
            ),
        )
