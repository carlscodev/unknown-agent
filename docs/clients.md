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

This package is the secure shared foundation for native desktop and mobile presentation layers and can also be embedded by alternative clients.