"""``ua benchmark`` — reproducible performance and reliability gates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from unknown_agent.benchmarks import BenchmarkRunner, BenchmarkSuite
from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError

app = typer.Typer(name="benchmark", no_args_is_help=True)


def _runtime(ctx: typer.Context) -> Runtime:
    if not isinstance(ctx.obj, Runtime):
        raise UsageError("CLI runtime is not initialised.")
    return ctx.obj


def _runner(ctx: typer.Context) -> BenchmarkRunner:
    runtime = _runtime(ctx)
    loaded = runtime.load()
    return BenchmarkRunner(loaded.paths.cwd, loaded.paths.project_dir / "benchmarks.db")


@app.command("run")
def run_suite(
    ctx: typer.Context,
    suite_file: Path,
    revision: str = typer.Option("unknown", "--revision"),
) -> None:
    """Execute a strict YAML benchmark suite and persist every sample."""
    runtime = _runtime(ctx)
    root = runtime.load().paths.cwd
    source = suite_file if suite_file.is_absolute() else root / suite_file
    suite = BenchmarkSuite.from_yaml(source)
    result = asyncio.run(_runner(ctx).run(suite, revision=revision))
    runtime.emit_json(result.model_dump(mode="json"))
    if any(summary.pass_rate < 1 for summary in result.summaries):
        raise typer.Exit(1)


@app.command("show")
def show(ctx: typer.Context, run_id: str) -> None:
    """Show a persisted benchmark run and aggregate statistics."""
    _runtime(ctx).emit_json(_runner(ctx).get_run(run_id).model_dump(mode="json"))


@app.command("compare")
def compare(
    ctx: typer.Context,
    baseline_run: str,
    candidate_run: str,
    threshold: float = typer.Option(10, "--threshold", min=0, max=1000),
) -> None:
    """Fail when candidate latency or reliability regresses beyond a threshold."""
    report = _runner(ctx).compare(baseline_run, candidate_run, threshold_percent=threshold)
    _runtime(ctx).emit_json(report.model_dump(mode="json"))
    if not report.passed:
        raise typer.Exit(1)
