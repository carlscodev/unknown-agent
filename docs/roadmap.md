# Platform roadmap

The four-stage sovereign runtime program is complete. Future releases are maintenance and incremental product evolution rather than unfinished platform foundations.

## Phase 1 — coding core · complete

- persistent LSP supervision and repository intelligence;
- mission-linked isolated worktrees;
- durable session forks with ancestry;
- tiered, provenance-preserving context compaction;
- transactional mutation queues and restart-safe execution.

## Phase 2 — extensible runtime · complete

- signed capability manifests and publisher trust;
- durable hook/event bus;
- persistent scheduler and signed webhooks;
- native sandbox profiles and fail-closed containment;
- stable JSON-RPC 2.0 and ACP editor integration.

## Phase 3 — omnichannel gateway · complete

- channel-neutral durable inbox and transactional outbox;
- ACL roles, one-time pairing, revocation and secret redaction;
- Telegram, Discord, Slack, WhatsApp and Matrix adapters;
- content-addressed media and cached voice transcription;
- Ed25519 device enrollment, replay protection and leased delegation.

## Phase 4 — product ecosystem · complete

- checksum-locked migrations and integrity-checked restoration;
- SPDX SBOM and signed DSSE/in-toto SLSA provenance;
- reproducible benchmark and regression gates;
- signed marketplace with revocation and atomic upgrades;
- encrypted offline-first shared client runtime;
- installable PWA and packaged `ua client` distribution;
- sandboxed Electron desktop applications for Linux, macOS and Windows;
- Capacitor Android and iOS projects;
- fail-closed native signing, notarization, checksums, SBOM and provenance workflows.

## Continuing release policy

Every change must preserve the 300-second timeout ceiling, machine-safe CLI behavior, signed extension boundaries, restart safety, strict Pydantic contracts, and full validation across supported Python and client platforms. Native store publication requires platform credentials; release workflows refuse to substitute unsigned artifacts when those credentials are absent.