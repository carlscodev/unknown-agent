# Durable hook and event bus

The hook bus lets signed extensions react to runtime events without blocking or
weakening the agent loop. It uses the existing structured `Event` contract,
redacts secrets before persistence, and stores matching events and deliveries in
SQLite WAL under `.unknown/hooks.db`.

## Registering a hook

Hooks must use `async def`. Synchronous handlers are rejected because Python
cannot safely terminate a blocked thread; async handlers provide real timeout and
cancellation semantics.

```python
from unknown_agent.extensions import HookContext, HookResult, HookSpec

async def on_gate(context: HookContext) -> HookResult:
    # context contains the already-redacted event and delivery attempt.
    return HookResult(ok=True, data={"observed": context.event_type})

class Plugin:
    name = "quality-observer"
    version = "1.0.0"

    def register(self, context):
        context.add_hook(
            HookSpec(
                name="quality-observer.gates",
                event_patterns=["gate.*"],
                priority=20,
                timeout_seconds=5,
                max_attempts=3,
                retry_backoff_seconds=1,
                circuit_failure_threshold=3,
                circuit_reset_seconds=60,
            ),
            on_gate,
        )
```

The signed capability manifest must grant `hooks.register` and declare the exact
hook name:

```json
{
  "permissions": ["hooks.register"],
  "capabilities": [
    {"kind": "hook", "name": "quality-observer.gates", "risk": null}
  ]
}
```

## Delivery semantics

- Events are matched with shell-style patterns such as `tool.*`, `gate.*`, or `*`.
- Lower numeric priority runs first; ties are ordered by hook name.
- Matching events are persisted before dispatch.
- Deliveries are **at least once**. Handlers should use `delivery_id` as an
  idempotency key when contacting external systems.
- A process that terminates during `running` resets that delivery to `pending` on
  restart without losing the attempt count.
- Failures retry with exponential backoff until `max_attempts`, then become `dead`.
- A successful terminal delivery can be explicitly replayed.
- Non-matching events are not duplicated into the hook database.

## Circuit breaker

Consecutive failures are durable per hook. At the configured threshold the
circuit opens for `circuit_reset_seconds`; new deliveries remain pending rather
than repeatedly hitting a known-broken dependency. A successful delivery closes
the circuit and clears the failure count.

## Cancellation and shutdown

Each handler runs in its own named `asyncio.Task`. `cancel(delivery_id)` cancels
the active task and records `cancelled`. `close()` cancels and awaits all active
handler and dispatcher tasks before closing SQLite.

Timeout uses `asyncio.wait_for`, which cancels the handler coroutine before the
delivery is retried or dead-lettered.

## Security and containment

`Event.to_json()` performs recursive secret redaction before the hook bus receives
the payload. The redacted representation is what is hashed, stored and passed to
handlers; raw credentials are never available to hooks through this channel.

Hook output must be `None`, a `HookResult`, or a mapping accepted by the strict
Pydantic schema. Unknown fields, oversized output, exceptions, corrupted event
hashes and malformed durable payloads become failed/dead deliveries and cannot
crash `EventBus.emit()` or the mission.

Signed plugin registration is atomic. Hooks are staged with tools and other
capabilities; if any declaration or permission check fails, registered hooks are
removed and no partial plugin remains active.
