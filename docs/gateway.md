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

All output is JSON-safe for pipes and service supervisors. Real channel adapters register a transport and call `Gateway.receive`; they never write directly to agent internals. The integrated ACL layer binds `(channel, account, sender)` identities through one-time six-digit pairing codes. Codes are salted, hashed, single-use, expire within five minutes, and lock after five failed attempts. Roles (`viewer`, `operator`, `owner`) grant explicit permissions; revocation is immediate. Use `ua gateway pair-issue`, `pair-claim`, `identities`, and `revoke`.

## Messaging adapters

Production adapters now normalize Telegram Bot API updates, Slack Events API messages, and Discord interactions into the same envelope. Telegram webhook tokens use constant-time comparison; Slack verifies `v0` HMAC signatures and rejects requests older than five minutes; Discord verifies Ed25519 signatures before JSON parsing. Outbound transports call each platform's official HTTPS API with a 30-second client timeout. Bot/self messages are ignored.

Adapter tokens and signing secrets must be `env:`, `file:`, `store:`, or `keyring:` references—raw secrets fail validation. Telegram media is represented as typed `telegram-file:` references for the media pipeline rather than embedding bytes. Instantiate `TelegramAdapter`, `SlackAdapter`, or `DiscordAdapter`, call `normalize`, pass the resulting envelope to `Gateway.receive`, and register each adapter's async `send` method as the channel transport.

WhatsApp Cloud API support verifies Meta's SHA-256 webhook signature, implements constant-time subscription challenge verification, normalizes batched text/media notifications, and sends through the versioned Graph API. Matrix support consumes authenticated incremental `/sync` batches, preserves `next_batch` cursors for restart-safe polling, ignores the agent's own events, maps replies and threads, and sends idempotent transaction-scoped room events. Matrix long-poll and HTTP timeouts are bounded at five minutes.

## Media and voice pipeline

`MediaStore` hydrates adapter media references through explicitly registered resolvers into a content-addressed store. Streaming ingestion enforces declared and actual size limits, MIME policy, optional upstream SHA-256, atomic publication, deduplication, and verification whenever an asset is read. Stored envelopes use portable `cas:<sha256>` references rather than platform credentials or temporary URLs.

`TranscriptionService` accepts audio or video assets, calls a bounded transcriber, and caches immutable results by content digest, engine identity, and language. `SpeechAPITranscriber` provides a concrete OpenAI-compatible HTTPS multipart implementation with secret-reference-only authentication and a timeout ceiling of five minutes. Empty and oversized transcripts fail closed.

## Security

- Process-wide secret redaction occurs before envelope persistence.
- A repeated external id with different content is rejected rather than silently replaced.
- Working data is bounded to 1 MiB per envelope; media bytes are referenced, not embedded.
- Tampered envelopes are contained and retried/dead-lettered without reaching handlers.
- Missing routes or transports fail visibly; no delivery is simulated.
- At-least-once transports receive a stable outbound id and must use it as their provider idempotency key where supported.
