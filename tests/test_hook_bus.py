from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from unknown_agent.config.secrets import REDACTOR
from unknown_agent.extensions.hooks import HookBus, HookContext, HookResult, HookSpec
from unknown_agent.observability.events import Event, EventBus


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_priority_filtering_and_durable_success(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")
    calls: list[str] = []

    async def later(context: HookContext) -> HookResult:
        calls.append("later")
        return HookResult(message=context.event_type)

    async def first(_context: HookContext) -> dict[str, object]:
        calls.append("first")
        return {"ok": True, "data": {"seen": True}}

    bus.register(HookSpec(name="later", event_patterns=["tool.*"], priority=20), later)
    bus.register(HookSpec(name="first", event_patterns=["tool.finished"], priority=1), first)
    event_id = bus.publish(Event(type="tool.finished", run_id="run-1", message="done"))
    assert run(bus.drain()) == 2
    assert calls == ["first", "later"]
    deliveries = bus.deliveries(event_id)
    assert [item.status for item in deliveries] == ["succeeded", "succeeded"]
    assert bus.verify_event(event_id) is True
    assert bus.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    run(bus.close())


def test_non_matching_hook_has_no_delivery(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")

    async def handler(_context: HookContext) -> None:
        raise AssertionError("must not run")

    bus.register(HookSpec(name="browser", event_patterns=["browser.*"]), handler)
    event_id = bus.publish(Event(type="tool.finished"))
    assert bus.deliveries(event_id) == []
    run(bus.close())


def test_secrets_are_redacted_before_handler_and_disk(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")
    received: list[HookContext] = []

    async def handler(context: HookContext) -> None:
        received.append(context)

    bus.register(HookSpec(name="audit", event_patterns=["*"]), handler)
    secret = "github_pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    REDACTOR.register(secret)
    event_id = bus.publish(Event(type="log", message=f"token={secret}", data={"nested": {"key": secret}}))
    run(bus.drain())
    assert received and secret not in received[0].message
    raw = bus.connection.execute(
        "SELECT payload_json FROM hook_events WHERE id=?", (event_id,)
    ).fetchone()[0]
    assert secret not in raw
    assert "********" in raw
    run(bus.close())


def test_retry_then_success_is_at_least_once(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")
    attempts = 0

    async def flaky(_context: HookContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")

    bus.register(
        HookSpec(
            name="flaky",
            event_patterns=["run.*"],
            max_attempts=3,
            retry_backoff_seconds=0,
        ),
        flaky,
    )
    event_id = bus.publish(Event(type="run.start"))
    run(bus.drain())
    assert bus.deliveries(event_id)[0].status == "pending"
    run(bus.drain())
    delivery = bus.deliveries(event_id)[0]
    assert delivery.status == "succeeded"
    assert delivery.attempts == 2
    run(bus.close())


def test_timeout_cancels_handler_and_dead_letters(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")
    cancelled = asyncio.Event()

    async def slow(_context: HookContext) -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    bus.register(
        HookSpec(
            name="slow",
            event_patterns=["*"],
            timeout_seconds=0.01,
            max_attempts=1,
        ),
        slow,
    )
    event_id = bus.publish(Event(type="log"))
    run(bus.drain())
    delivery = bus.deliveries(event_id)[0]
    assert delivery.status == "dead"
    assert "timed out" in delivery.error
    assert cancelled.is_set()
    run(bus.close())


@pytest.mark.asyncio
async def test_active_delivery_can_be_cancelled(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def waiting(_context: HookContext) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    bus.register(HookSpec(name="waiting", event_patterns=["*"], timeout_seconds=120), waiting)
    event_id = bus.publish(Event(type="log"))
    delivery_id = bus.deliveries(event_id)[0].id
    await asyncio.wait_for(started.wait(), timeout=1)
    bus.cancel(delivery_id)
    await asyncio.wait_for(stopped.wait(), timeout=1)
    await asyncio.sleep(0)
    assert bus.delivery(delivery_id).status == "cancelled"
    await bus.close()


def test_circuit_breaker_defers_new_deliveries(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")

    async def broken(_context: HookContext) -> None:
        raise RuntimeError("downstream unavailable")

    spec = HookSpec(
        name="broken",
        event_patterns=["*"],
        max_attempts=1,
        circuit_failure_threshold=2,
        circuit_reset_seconds=30,
    )
    bus.register(spec, broken)
    first = bus.publish(Event(type="log"))
    run(bus.drain())
    second = bus.publish(Event(type="log"))
    run(bus.drain())
    third = bus.publish(Event(type="log"))
    run(bus.drain())
    assert bus.deliveries(first)[0].status == "dead"
    assert bus.deliveries(second)[0].status == "dead"
    deferred = bus.deliveries(third)[0]
    assert deferred.status == "pending"
    assert deferred.next_attempt_at > deferred.created_at
    run(bus.close())


def test_interrupted_running_delivery_recovers_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "hooks.db"
    first = HookBus(path)

    async def handler(_context: HookContext) -> None:
        return None

    spec = HookSpec(name="recover", event_patterns=["*"])
    first.register(spec, handler)
    event_id = first.publish(Event(type="log"))
    delivery_id = first.deliveries(event_id)[0].id
    first.connection.execute(
        "UPDATE hook_deliveries SET status='running' WHERE id=?", (delivery_id,)
    )
    first.connection.commit()
    first.connection.close()

    second = HookBus(path)
    second.register(spec, handler)
    assert second.delivery(delivery_id).status == "pending"
    run(second.start())
    assert second.delivery(delivery_id).status == "succeeded"
    run(second.close())


def test_replay_runs_a_terminal_delivery_again(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")
    count = 0

    async def handler(_context: HookContext) -> None:
        nonlocal count
        count += 1

    bus.register(HookSpec(name="replay", event_patterns=["*"]), handler)
    event_id = bus.publish(Event(type="log"))
    run(bus.drain())
    delivery_id = bus.deliveries(event_id)[0].id
    bus.replay(delivery_id)
    run(bus.drain())
    assert count == 2
    assert bus.delivery(delivery_id).attempts == 1
    run(bus.close())


def test_malformed_hook_output_is_contained(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")

    async def malformed(_context: HookContext) -> dict[str, object]:
        return {"ok": True, "unexpected": "not allowed"}

    bus.register(
        HookSpec(name="malformed", event_patterns=["*"], max_attempts=1), malformed
    )
    event_id = bus.publish(Event(type="log"))
    run(bus.drain())
    delivery = bus.deliveries(event_id)[0]
    assert delivery.status == "dead"
    assert "ValidationError" in delivery.error
    run(bus.close())


def test_tampered_durable_event_is_dead_lettered_not_raised(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")

    async def handler(_context: HookContext) -> None:
        return None

    bus.register(HookSpec(name="integrity", event_patterns=["*"], max_attempts=1), handler)
    event_id = bus.publish(Event(type="log"))
    bus.connection.execute(
        "UPDATE hook_events SET payload_json='{}' WHERE id=?", (event_id,)
    )
    bus.connection.commit()
    run(bus.drain())
    assert bus.verify_event(event_id) is False
    delivery = bus.deliveries(event_id)[0]
    assert delivery.status == "dead"
    assert "integrity" in delivery.error
    run(bus.close())


def test_sync_handlers_are_rejected(tmp_path: Path) -> None:
    bus = HookBus(tmp_path / "hooks.db")

    def sync_handler(_context: HookContext) -> None:
        return None

    with pytest.raises(TypeError, match="must be async"):
        bus.register(HookSpec(name="sync", event_patterns=["*"]), sync_handler)  # type: ignore[arg-type]
    run(bus.close())


def test_event_bus_integration_publishes_to_hook_bus(tmp_path: Path) -> None:
    hooks = HookBus(tmp_path / "hooks.db")
    seen: list[str] = []

    async def handler(context: HookContext) -> None:
        seen.append(context.event_type)

    hooks.register(HookSpec(name="bridge", event_patterns=["gate.*"]), handler)
    events = EventBus(hook_bus=hooks)
    events.emit(Event(type="gate.finished"))
    run(hooks.drain())
    assert seen == ["gate.finished"]
    run(hooks.close())


def test_database_errors_do_not_escape_event_bus(tmp_path: Path) -> None:
    hooks = HookBus(tmp_path / "hooks.db")
    hooks.connection.close()
    events = EventBus(hook_bus=hooks)
    assert events.emit(Event(type="log")).type == "log"
