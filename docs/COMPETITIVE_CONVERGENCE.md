# Competitive Convergence Program

Unknown Agent is not going to become competitive by copying six repositories
into one tree. It becomes competitive by taking the strongest *capability
boundaries* from each MIT project, implementing them in its own architecture,
and making the combined system more coherent than any source product.

## Legal boundary

| Reference | License decision | Treatment |
|---|---:|---|
| anomalyco/opencode | MIT | Behaviour and architecture may be studied; compatible ideas may be independently integrated. |
| openclaw/openclaw | MIT | Gateway, channel, scheduler, device and plugin concepts are eligible. Third-party notices must be retained for any adapted code. |
| NousResearch/hermes-agent | MIT | Context, LSP, learning, provider resilience and delegation concepts are eligible. |
| earendil-works/pi | MIT | Durable sessions, forks, event reduction, tool durability and extension concepts are eligible. |
| hangsman/claude-code-source | Proprietary, all rights reserved | Excluded. No source inspection, adaptation or copying into Unknown Agent. |
| Twigpine/openclaude | Mixed/proprietary-derived | Excluded. No source inspection, adaptation or copying into Unknown Agent. |

No implementation in this program copies proprietary Claude Code material.

## Capability matrix

| Domain | OpenCode | OpenClaw | Hermes | Pi | Unknown Agent target |
|---|---|---|---|---|---|
| Coding tools | Strong | Secondary | Strong | Strong | AST + LSP + transactional patch engine |
| Durable runs | Sessions | Gateway sessions | Compression/recovery | Event-sourced forks | WAL mission DAG + event-sourced session branches |
| Parallel isolation | Workspaces | Agent routing | Delegation | Named branch lanes | Git worktrees tied to mission nodes |
| Context control | Session context UI | Memory services | Multi-layer compaction | Branch summarisation | Cache-aware tiered compaction with provenance |
| Extensions | Plugins/SDK | Large plugin marketplace | Skills/tools | Facets/extensions | Signed capability manifests and sandboxed hooks |
| Protocols | Client/server, MCP | Gateway, A2A | ACP, MCP | RPC, MCP | MCP + ACP + stable local RPC contract |
| Security | Permissions | Pairing/sandbox/access control | File safety/redaction | Container guidance | Capability policy, secret redaction, process and network sandbox |
| Channels | IDE/desktop/web | Broad messaging/device ecosystem | Messaging gateway | RPC clients | Optional channel packs over one gateway core |
| Automation | Session actions | Cron/hooks/webhooks | Scheduler/webhooks | Durable jobs | Persistent scheduler with idempotency and audit ledger |
| Observability | Session timeline | Diagnostics/OTel | OTLP/activity | Typed telemetry | Unified event schema, traces, cost and replay |
| UX | Desktop/web/TUI | Native/mobile/web | CLI/TUI | High-performance TUI | One semantic UI model across CLI/TUI/web/mobile |

## Program phases

### Phase 1 — Coding Core Supremacy

1. Persistent LSP supervisor and typed tools: symbols, definition, references,
   hover, diagnostics and workspace rename.
2. Mission worktree manager: one isolated branch per parallel worker, collision
   detection, semantic commits, merge previews and deterministic cleanup.
3. Event-sourced session forks with named lanes and reproducible snapshots.
4. Tiered context compaction: immutable prefix, repository map, active working
   set, archive summary and volatile tail.
5. Transactional mutation queue: patches are staged, parsed, tested and only
   then consolidated.

### Phase 2 — Extensible Autonomous Runtime

1. Signed capability manifests and plugin compatibility contracts.
2. Hook/event bus with timeouts, circuit breakers and secret redaction.
3. Persistent cron/webhook scheduler with idempotency keys and dead-letter queue.
4. Process, filesystem and network sandbox profiles.
5. Stable local RPC/ACP service for IDEs, desktop clients and remote workers.

### Phase 3 — Omnichannel Gateway

1. Channel-neutral message envelope and identity mapping.
2. Telegram, Discord and Slack packs first; WhatsApp and Matrix second.
3. Pairing, ACLs, group policy and loop prevention.
4. Media ingestion, transcription and voice response routing.
5. Device/node registry and secure remote tool delegation.

### Phase 4 — Product Surface and Ecosystem

1. Desktop and mobile clients over the same gateway API.
2. Extension marketplace with signatures, permissions and provenance.
3. Public benchmark harness for coding, autonomy, recovery and security.
4. Migration adapters for OpenAI-compatible agents, MCP servers and skill packs.
5. Reproducible release pipeline, SBOM and supply-chain attestations.

## Competitive acceptance criteria

A capability is not complete because a command exists. It is complete when:

- it has a typed public contract and machine-readable errors;
- permissions are evaluated before side effects;
- state survives process death and can be replayed;
- cancellation terminates child processes, not only the parent coroutine;
- telemetry names latency, tokens, cache and cost by worker/model;
- malformed external output cannot corrupt the mission ledger;
- a focused test proves the failure path, not only the happy path;
- the CLI, TUI and web surfaces consume the same underlying state model;
- the implementation has a documented license/provenance boundary.
