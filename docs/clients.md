# Desktop and mobile client runtime

Unknown Agent provides one offline-first Python client core for desktop and mobile shells. UI frameworks remain replaceable; encryption, synchronization, remote approval, request signing, and replay-safe device identity are shared.

## Secure local state

`SecureClientStore` keeps metadata in SQLite/WAL while encrypting every event and outbound operation with ChaCha20-Poly1305. A fresh 96-bit nonce and domain-separated associated data bind ciphertext to its table and identifier. Moving ciphertext between records or modifying a byte fails authentication.

The 32-byte storage key is loaded from `env:`, `file:`, `store:`, or `keyring:` and is never stored in SQLite. Payloads are capped at 1 MiB. Queued operations have stable idempotency identifiers, so offline retries cannot silently change an existing action.

## Durable synchronization

Clients retain a server cursor and up to 100 pending operations per batch. Applying a response atomically:

- inserts uniquely sequenced events,
- detects identifier/sequence conflicts,
- acknowledges known operations,
- advances the cursor.

Reapplying the same response is safe. An acknowledgement for an unknown operation fails closed. Events support messages, approvals, device commands, and status updates.

## Signed transport

`ClientTransport` requires HTTPS and signs every request with the enrolled device's Ed25519 key. The signature covers a current timestamp, fresh nonce, and SHA-256 body digest using the same proof format as device delegation. The server's durable nonce ledger supplies replay protection.

Responses are streamed with a 1 MiB bound. HTTP and transport retries are bounded to five, use capped exponential delay, and never apply to ordinary client errors. Request and total timeouts cannot exceed 300 seconds.

## Remote approvals

A client resolves an approval by queuing an immutable `approval.resolve` operation containing the approval identifier, decision, and bounded reason. Its deterministic operation identifier prevents duplicate approval side effects after disconnects or process termination.

```python
store.resolve_approval("approval-id", approved=True, reason="Reviewed on mobile")
await runtime.sync_once()
```

## Installable client shell

The `client/` package is a responsive Vue PWA for desktop and mobile. It provides an offline inbox, message composer, approval queue, device identity screen, endpoint enrollment, install manifest, and network-first service worker fallback.

```bash
cd client
npm install
npm run build
```

The shell generates its device identity in WebCrypto. Private Ed25519 and AES-GCM keys are non-exportable before they are persisted in IndexedDB. Event and operation payloads are encrypted at rest using record-bound associated data. Use `ua gateway device-enroll-issue`, enter the HTTPS agent endpoint and one-time token in Settings, then synchronize.

The FastAPI control server exposes `/v1/client/enroll` and `/v1/client/sync`. Enrollment delegates to the cryptographic device controller. Sync verifies timestamp, nonce, body digest, and Ed25519 signature before entering the durable client ledger. The server journals operations idempotently, stores approval decisions, and pages targeted or broadcast events by sequence cursor.

This package can run as an installed PWA today and remains suitable for wrapping with native WebView containers and platform signing.