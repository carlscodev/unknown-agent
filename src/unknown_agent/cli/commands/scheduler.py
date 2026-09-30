"""``ua scheduler`` — durable cron, interval, one-shot, and webhook automation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from unknown_agent.automation import JobSpec, Scheduler, register_agent_action
from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError

app = typer.Typer(name="scheduler", no_args_is_help=True)


def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if not isinstance(runtime, Runtime):
        raise UsageError("CLI runtime is not initialised.")
    return runtime


def _scheduler(ctx: typer.Context) -> Scheduler:
    loaded = _runtime(ctx).load()
    return Scheduler(loaded.paths.project_dir / "scheduler.db")


def _emit(ctx: typer.Context, value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    _runtime(ctx).emit_json(value)


def _payload(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise UsageError(f"Invalid --payload JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise UsageError("--payload must be a JSON object.")
    return decoded


def _date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise UsageError("--run-at must be an ISO 8601 timestamp with timezone.") from exc


@app.command("create")
def create_job(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name"),
    action: str = typer.Option("agent.run", "--action"),
    trigger: str = typer.Option(..., "--trigger"),
    payload: str = typer.Option("{}", "--payload"),
    run_at: str | None = typer.Option(None, "--run-at"),
    interval: int | None = typer.Option(None, "--interval"),
    cron: str | None = typer.Option(None, "--cron"),
    timezone: str = typer.Option("UTC", "--timezone"),
    webhook_secret_ref: str | None = typer.Option(None, "--webhook-secret-ref"),
    max_attempts: int = typer.Option(3, "--max-attempts"),
    backoff: float = typer.Option(5, "--backoff"),
    timeout: float = typer.Option(300, "--timeout", max=300),
    max_concurrency: int = typer.Option(1, "--max-concurrency"),
) -> None:
    try:
        spec = JobSpec.model_validate(
            {
                "name": name,
                "action": action,
                "trigger": trigger,
                "payload": _payload(payload),
                "run_at": _date(run_at),
                "interval_seconds": interval,
                "cron": cron,
                "timezone": timezone,
                "webhook_secret_ref": webhook_secret_ref,
                "max_attempts": max_attempts,
                "backoff_seconds": backoff,
                "timeout_seconds": timeout,
                "max_concurrency": max_concurrency,
            }
        )
    except ValidationError as exc:
        raise UsageError(str(exc)) from exc
    _emit(ctx, _scheduler(ctx).create_job(spec))


@app.command("actions")
def list_actions(ctx: typer.Context) -> None:
    scheduler = _scheduler(ctx)
    runtime = _runtime(ctx)
    register_agent_action(scheduler, lambda: runtime.load(force=True))
    _emit(ctx, {"actions": [item.model_dump(mode="json") for item in scheduler.list_actions()]})


@app.command("list")
def list_jobs(ctx: typer.Context) -> None:
    _emit(ctx, {"jobs": [job.model_dump(mode="json") for job in _scheduler(ctx).list_jobs()]})


@app.command("inspect")
def inspect_job(ctx: typer.Context, job_id: str) -> None:
    scheduler = _scheduler(ctx)
    _emit(
        ctx,
        {
            "job": scheduler.get_job(job_id).model_dump(mode="json"),
            "audit_valid": scheduler.verify_audit(),
            "executions": [
                item.model_dump(mode="json") for item in scheduler.list_executions(job_id=job_id)
            ],
        },
    )


@app.command("pause")
def pause_job(ctx: typer.Context, job_id: str) -> None:
    _emit(ctx, _scheduler(ctx).set_enabled(job_id, False))


@app.command("resume")
def resume_job(ctx: typer.Context, job_id: str) -> None:
    _emit(ctx, _scheduler(ctx).set_enabled(job_id, True))


@app.command("trigger")
def trigger_job(
    ctx: typer.Context,
    job_id: str,
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    _emit(ctx, _scheduler(ctx).trigger(job_id, idempotency_key=idempotency_key))


@app.command("runs")
def list_runs(ctx: typer.Context, job_id: str | None = typer.Option(None, "--job")) -> None:
    records = _scheduler(ctx).list_executions(job_id=job_id)
    _emit(ctx, {"executions": [item.model_dump(mode="json") for item in records]})


@app.command("cancel")
def cancel_run(ctx: typer.Context, execution_id: str) -> None:
    _emit(ctx, asyncio.run(_scheduler(ctx).cancel(execution_id)))


@app.command("replay")
def replay_run(ctx: typer.Context, execution_id: str) -> None:
    _emit(ctx, _scheduler(ctx).replay(execution_id))


@app.command("webhook")
def accept_webhook(
    ctx: typer.Context,
    job_id: str,
    signature: str = typer.Option(..., "--signature"),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
    body: Path = typer.Option(..., "--body", exists=True, dir_okay=False, readable=True),
) -> None:
    _emit(
        ctx,
        _scheduler(ctx).ingest_webhook(
            job_id,
            body.read_bytes(),
            signature=signature,
            idempotency_key=idempotency_key,
        ),
    )


@app.command("worker")
def run_worker(
    ctx: typer.Context,
    once: bool = typer.Option(False, "--once"),
    poll: float = typer.Option(1, "--poll", min=0.1, max=300),
) -> None:
    runtime = _runtime(ctx)
    scheduler = _scheduler(ctx)
    register_agent_action(scheduler, lambda: runtime.load(force=True))
    if once:
        records = asyncio.run(scheduler.run_due())
        _emit(ctx, {"executions": [item.model_dump(mode="json") for item in records]})
        return
    try:
        asyncio.run(scheduler.serve(poll_seconds=poll))
    except KeyboardInterrupt:
        return
