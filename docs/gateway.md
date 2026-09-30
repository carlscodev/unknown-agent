# Channel-neutral gateway

The durable gateway is the first layer of Phase 3. Channel adapters normalize inbound messages into strict envelopes and consume strict outbound envelopes; the agent runtime never depends on Telegram-, Discord-, Slack-, WhatsApp-, or Matrix-specific payloads.

## Contracts

An inbound envelope carries a stable external delivery id, channel address, sender identity, timestamp, reply reference, bounded metadata, and typed text/media parts. Media parts require a URI and may include MIME type, filename, size, and SHA-256 digest. Unknown fields and malformed addresses are rejected.

The project-local `.unknown/gateway.db` uses SQLite/WAL and contains:

- a deduplicated inbox keyed by channel and external delivery id;
- a transactional outbox created in the same transaction that completes routing;
- durable route and transport registration metadata;
- status, attempt, lease, retry, result, and bounded error records.

Handlers and transports are async-only. Workers claim records atomically, recover expired leases after process death, verify SHA-256 envelope integrity, retry with bounded exponential backoff, and dead-letter after five attempts. Every route, transport, lease, retry, and poll timeout is at most 300 seconds.

## Agent integration

`register_agent_route` maps channel-neutral text into the existing `RunRequest` and invokes `core.runner.run_task`. Gateway identity is attached to run metadata. Responses contain the canonical run id and status. Provider routing, tools, signed extensions, hooks, scheduler, policy, native sandbox, journals, and verification remain unchanged.

## CLI

```bash
ua gateway receive \
  --channel test --account bot --conversation room \
  --sender user-1 --external-id delivery-1 --text "Review this repository"
ua gateway list
ua gateway drain
ua gateway worker --poll 1
```

All output is JSON-safe for pipes and service supervisors. Real channel adapters register a transport and call `Gateway.receive`; they never write directly to agent internals. The integrated ACL layer binds `(channel, account, sender)` identities through one-time six-digit pairing codes. Codes are salted, hashed, single-use, expire within five minutes, and lock after five failed attempts. Roles (`viewer`, `operator`, `owner`) grant explicit permissions; revocation is immediate. Use `ua gateway pair-issue`, `pair-claim`, `identities`, and `revoke`. Concrete messaging adapters are the next Phase 3 layer.

## Security

- Process-wide secret redaction occurs before envelope persistence.
- A repeated external id with different content is rejected rather than silently replaced.
- Working data is bounded to 1 MiB per envelope; media bytes are referenced, not embedded.
- Tampered envelopes are contained and retried/dead-lettered without reaching handlers.
- Missing routes or transports fail visibly; no delivery is simulated.
- At-least-once transports receive a stable outbound id and must use it as their provider idempotency key where supported.
