from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from unknown_agent.automation import (
    ActionResult,
    ExecutionStatus,
    JobSpec,
    Scheduler,
)


def once(action: str = "test.action", **changes: object) -> JobSpec:
    values: dict[str, object] = {
        "name": "one shot",
        "trigger": "once",
        "action": action,
        "run_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    values.update(changes)
    return JobSpec.model_validate(values)


@pytest.mark.asyncio
async def test_one_shot_is_persisted_and_dispatched(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db")
    seen: list[dict[str, object]] = []

    async def action(context: object) -> ActionResult:
        seen.append(context.payload)  # type: ignore[attr-defined]
        return ActionResult(output={"accepted": True})

    scheduler.register_action("test.action", action)
    job = scheduler.create_job(once(payload={"answer": 42}))
    records = await scheduler.run_due()

    assert seen == [{"answer": 42}]
    assert records[0].status is ExecutionStatus.SUCCEEDED
    assert records[0].result == {"ok": True, "output": {"accepted": True}, "message": ""}
    assert scheduler.get_job(job.id).next_run_at is None


def test_action_registry_metadata_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.db"
    scheduler = Scheduler(path)

    async def action(_context: object) -> None:
        return None

    scheduler.register_action("release.publish", action, source="tests")
    current = scheduler.list_actions()[0]
    assert current.source == "tests"
    assert current.available

    restarted = Scheduler(path)
    persisted = restarted.list_actions()[0]
    assert persisted.name == "release.publish"
    assert not persisted.available


@pytest.mark.asyncio
async def test_claims_are_atomic_across_workers(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.db"
    first = Scheduler(path, owner="first")
    second = Scheduler(path, owner="second")
    calls = 0

    async def action(_context: object) -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)

    first.register_action("test.action", action)
    second.register_action("test.action", action)
    first.create_job(once())
    await asyncio.gather(first.run_due(), second.run_due())

    assert calls == 1
    assert first.list_executions()[0].status is ExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_retry_backoff_and_dead_letter(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db")

    async def broken(_context: object) -> None:
        raise RuntimeError("deliberate failure")

    scheduler.register_action("test.action", broken)
    scheduler.create_job(once(max_attempts=2, backoff_seconds=0.1))
    first = (await scheduler.run_due())[0]
    assert first.status is ExecutionStatus.PENDING
    assert first.attempts == 1

    second = (await scheduler.run_due(now=datetime.now(UTC) + timedelta(seconds=1)))[0]
    assert second.status is ExecutionStatus.DEAD
    assert second.attempts == 2
    assert "deliberate failure" in (second.error or "")


@pytest.mark.asyncio
async def test_expired_lease_recovers_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.db"
    crashed = Scheduler(path, owner="crashed")
    job = crashed.create_job(once())
    crashed._materialize_due(datetime.now(UTC), 10)
    execution = crashed.list_executions(job_id=job.id)[0]
    assert crashed._claim(execution.id, datetime.now(UTC)) == execution.id
    with crashed._connect() as db:
        db.execute(
            "UPDATE scheduler_executions SET lease_until=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), execution.id),
        )

    recovered = Scheduler(path, owner="recovered")

    async def action(_context: object) -> dict[str, bool]:
        return {"recovered": True}

    recovered.register_action("test.action", action)
    records = await recovered.run_due()
    assert records[0].status is ExecutionStatus.SUCCEEDED
    assert records[0].attempts == 2


def test_webhook_signature_idempotency_and_secret_reference(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db", secret_resolver=lambda _ref: "secret")
    job = scheduler.create_job(
        JobSpec(
            name="webhook",
            trigger="webhook",
            action="test.action",
            webhook_secret_ref="env:AUTOMATION_SECRET",
        )
    )
    body = json.dumps({"event": "push"}).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    first = scheduler.ingest_webhook(
        job.id, body, signature=signature, idempotency_key="delivery-1"
    )
    duplicate = scheduler.ingest_webhook(
        job.id, body, signature=signature, idempotency_key="delivery-1"
    )
    assert first.id == duplicate.id
    assert "secret" not in (tmp_path / "scheduler.db").read_bytes().decode(errors="ignore")

    with pytest.raises(PermissionError, match="signature"):
        scheduler.ingest_webhook(
            job.id, body, signature="sha256=" + "0" * 64, idempotency_key="delivery-2"
        )


@pytest.mark.asyncio
async def test_webhook_payload_reaches_action(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db", secret_resolver=lambda _ref: "key")
    payloads: list[dict[str, object]] = []

    async def action(context: object) -> None:
        payloads.append(context.payload)  # type: ignore[attr-defined]

    scheduler.register_action("test.action", action)
    job = scheduler.create_job(
        JobSpec(
            name="webhook",
            trigger="webhook",
            action="test.action",
            payload={"configured": True},
            webhook_secret_ref="store:hook-key",
        )
    )
    body = b'{"event":"release"}'
    signature = "sha256=" + hmac.new(b"key", body, hashlib.sha256).hexdigest()
    scheduler.ingest_webhook(job.id, body, signature=signature, idempotency_key="1")
    await scheduler.run_due()
    assert payloads == [{"configured": True, "webhook": {"event": "release"}}]


@pytest.mark.asyncio
async def test_pause_resume_manual_trigger_and_replay(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db")

    async def action(_context: object) -> None:
        return None

    scheduler.register_action("test.action", action)
    job = scheduler.create_job(once(run_at=datetime.now(UTC) + timedelta(hours=1)))
    scheduler.set_enabled(job.id, False)
    with pytest.raises(ValueError, match="paused"):
        scheduler.trigger(job.id)
    scheduler.set_enabled(job.id, True)
    execution = scheduler.trigger(job.id, idempotency_key="manual-1")
    duplicate = scheduler.trigger(job.id, idempotency_key="manual-1")
    assert duplicate.id == execution.id
    await scheduler.run_due()
    replay = scheduler.replay(execution.id)
    await scheduler.run_due()
    assert replay.id != execution.id
    assert all(record.status is ExecutionStatus.SUCCEEDED for record in scheduler.list_executions())


def test_strict_trigger_contracts_and_timezone_validation() -> None:
    with pytest.raises(ValidationError, match="required setting"):
        JobSpec(name="bad", trigger="interval", action="test.action")
    with pytest.raises(ValidationError, match="may not be mixed"):
        JobSpec(
            name="bad",
            trigger="once",
            action="test.action",
            run_at=datetime.now(UTC),
            cron="* * * * *",
        )
    with pytest.raises(ValidationError, match="timezone"):
        once(timezone="Mars/Olympus_Mons")
    with pytest.raises(ValidationError, match="timezone"):
        once(run_at=datetime.now())
    with pytest.raises(ValidationError, match="secret reference"):
        JobSpec(
            name="unsafe webhook",
            trigger="webhook",
            action="test.action",
            webhook_secret_ref="raw-secret-must-not-be-persisted",
        )


def test_cron_and_interval_calculate_future_occurrences(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db")
    interval = scheduler.create_job(
        JobSpec(name="interval", trigger="interval", action="test.action", interval_seconds=30)
    )
    cron = scheduler.create_job(
        JobSpec(
            name="cron",
            trigger="cron",
            action="test.action",
            cron="*/5 * * * *",
            timezone="America/Caracas",
        )
    )
    assert interval.next_run_at is not None
    assert cron.next_run_at is not None
    assert cron.next_run_at.tzinfo is not None


def test_audit_records_form_hash_chain(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db")
    job = scheduler.create_job(once())
    scheduler.set_enabled(job.id, False)
    with scheduler._connect() as db:
        rows = db.execute("SELECT * FROM scheduler_audit ORDER BY seq").fetchall()
    assert rows[0]["previous_hash"] == "0" * 64
    assert rows[1]["previous_hash"] == rows[0]["record_hash"]
    assert scheduler.verify_audit()
    with scheduler._connect() as db:
        db.execute("UPDATE scheduler_audit SET detail='{}' WHERE seq=1")
    assert not scheduler.verify_audit()


@pytest.mark.asyncio
async def test_timeout_retries_and_terminal_cancellation(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db")

    async def slow(_context: object) -> None:
        await asyncio.sleep(0.2)

    scheduler.register_action("test.action", slow)
    scheduler.create_job(once(timeout_seconds=0.1, max_attempts=1))
    record = (await scheduler.run_due())[0]
    assert record.status is ExecutionStatus.DEAD
    assert "TimeoutError" in (record.error or "")

    future = scheduler.create_job(once(run_at=datetime.now(UTC) + timedelta(hours=1)))
    pending = scheduler.trigger(future.id)
    cancelled = await scheduler.cancel(pending.id)
    assert cancelled.status is ExecutionStatus.CANCELLED


def test_sync_actions_and_oversized_payloads_are_rejected(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path / "scheduler.db")
    with pytest.raises(TypeError, match="async"):
        scheduler.register_action("sync", lambda _context: None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="exceeds"):
        once(payload={"large": "x" * 1_048_576})
