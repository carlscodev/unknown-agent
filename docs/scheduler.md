# Persistent scheduler

Unknown Agent's automation scheduler turns cron expressions, fixed intervals, one-shot timestamps, signed webhooks, and manual triggers into durable action executions. It uses the existing agent runner for `agent.run`; it is not a second execution architecture.

## Storage and delivery

Each project stores scheduler state in `.unknown/scheduler.db`. SQLite runs in WAL mode. Job creation, occurrence materialization, lease claims, retries, dead-letter transitions, pause/resume, cancellation, and replay are transactional and recorded in a SHA-256-linked audit log.

Delivery is **at least once**. A scheduled occurrence has a deterministic unique key, webhook deliveries require an idempotency key, and manual callers may supply one. Workers claim executions with `BEGIN IMMEDIATE`, an owner identity, and an expiring lease. A heartbeat renews the lease while an action runs. If a process dies, a later worker recovers the expired lease and retries the same execution record. Action implementations must use `ExecutionContext.occurrence_key` when coordinating non-transactional external side effects.

Execution output is strict, bounded structured data. Actions are async-only, have a deadline, and can be cancelled. Failed attempts use bounded exponential backoff. Exhausted executions enter `dead`; `replay` creates a distinct, audited occurrence rather than mutating history.

## Create jobs

```bash
# One-shot mission
ua scheduler create \
  --name "release audit" \
  --trigger once \
  --run-at "2026-10-01T09:00:00-04:00" \
  --payload '{"task":"audit the release and run every gate","kind":"run"}'

# Every 15 minutes
ua scheduler create \
  --name "dependency watch" \
  --trigger interval \
  --interval 900 \
  --payload '{"task":"check dependency advisories and update the evidence journal","read_only":true}'

# Timezone-aware cron
ua scheduler create \
  --name "weekday maintenance" \
  --trigger cron \
  --cron "0 8 * * 1-5" \
  --timezone "America/Caracas" \
  --payload '{"task":"run repository maintenance and verification"}'
```

`agent.run` accepts the normal mission controls: `task`, `kind`, `mode`, `read_only`, `dry_run`, `model`, `provider`, `max_steps`, `definition_of_done`, `gates`, `tools`, `orchestrate`, and `extra_instructions`. Unknown fields are rejected before execution.

## Signed webhooks

Webhook jobs store a **secret reference**, never a raw signing secret. Supported references are `env:`, `file:`, `store:`, and `keyring:` and use the same secret resolver as providers.

```bash
ua scheduler create \
  --name "release webhook" \
  --trigger webhook \
  --webhook-secret-ref env:UA_RELEASE_WEBHOOK_SECRET \
  --payload '{"task":"validate the release described by the webhook"}'
```

A sender computes `HMAC-SHA256(secret, exact_request_body)` and supplies `sha256=<hex>` plus a stable delivery id. Embedders call `Scheduler.ingest_webhook(...)`; the CLI can accept a body without exposing the secret:

```bash
ua scheduler webhook JOB_ID \
  --signature "sha256=$SIGNATURE" \
  --idempotency-key "$DELIVERY_ID" \
  --body payload.json
```

Signatures use constant-time comparison. Bodies are bounded to 1 MiB and must be JSON objects. Duplicate delivery ids resolve to the existing execution.

## Operate workers

```bash
ua scheduler worker             # continuous foreground service
ua scheduler worker --once      # drain currently due work and exit
ua scheduler list
ua scheduler inspect JOB_ID
ua scheduler pause JOB_ID
ua scheduler resume JOB_ID
ua scheduler trigger JOB_ID --idempotency-key manual-2026-10-01
ua scheduler runs --job JOB_ID
ua scheduler cancel EXECUTION_ID
ua scheduler replay EXECUTION_ID
```

All scheduler command results are JSON without ANSI formatting, including in pipes and non-TTY services. Run the worker under systemd, launchd, a container supervisor, Termux, or a platform scheduler. Multiple workers may share the database; atomic claims and per-job concurrency limits prevent concurrent over-dispatch.

## Python actions

Applications may register an async action without replacing the runtime:

```python
from unknown_agent.automation import ActionResult, Scheduler

scheduler = Scheduler(project / ".unknown" / "scheduler.db")

async def publish(context):
    # Deduplicate external effects with context.occurrence_key.
    return ActionResult(output={"published": True})

scheduler.register_action("release.publish", publish)
```

An action receives immutable execution identity, attempt count, scheduled time, and validated job payload. Registration rejects synchronous handlers and duplicate names. Registration metadata (name, source, first registration, and last seen time) is durable and `ua scheduler actions` distinguishes persisted definitions from handlers available in the current worker. The production `agent.run` action bridges directly to `core.runner.run_task` and records the resulting run id, status, path, changed files, and blockers.

## Security boundaries

- Never put credentials in job payloads; use secret references.
- HMAC verification happens before webhook persistence or dispatch.
- Paused jobs cannot materialize or dispatch work.
- Limits apply to payload size, output size, attempts, timeout, concurrency, and backoff.
- Unknown action names fail visibly and retry according to policy rather than being simulated.
- Cancellation is terminal; replay is explicit and auditable.
- The audit chain exposes accidental or malicious record modification.
