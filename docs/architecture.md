# Unknown Agent — Architecture

*Linux-first, Windows-capable. Python-first. CLI/TUI-first.
147 modules, 42 built-in tools, 536 tests. Everything below is implemented and verified unless explicitly marked "planned".*

---

## 1. System overview

```
                                ┌────────────────────────────────┐
                                │       ua  (Typer CLI)          │
                                │  setup · run · config · mcp    │
                                │  tools · vision · doctor · …   │
                                └───────────────┬────────────────┘
                                                │ Runtime (lazy settings, palette, output mode)
       ┌────────────────────────────────────────┼─────────────────────────────────────────┐
       │                                        │                                         │
┌──────▼──────┐   ┌──────────────┐   ┌──────────▼──────────┐   ┌──────────────┐   ┌───────▼───────┐
│ Onboarding  │   │ Config       │   │  Agent core         │   │ Providers    │   │ Observability │
│ flow.py     │   │ layered load │   │  runner + executor  │   │ 4 dialects + │   │ events, logs, │
│ screens.py  │   │ validation   │   │  planning, healing  │   │ capabilities │   │ run journals  │
└─────────────┘   └──────────────┘   └──────────┬──────────┘   └──────────────┘   └───────────────┘
                                                │ ToolContext (everything injected, nothing global)
              ┌───────────────┬─────────────────┼───────────────────┬───────────────────┐
              │               │                 │                   │                   │
       ┌──────▼─────┐  ┌──────▼──────┐  ┌───────▼───────┐  ┌────────▼────────┐  ┌───────▼───────┐
       │ Tools      │  │ MCP         │  │ Vision        │  │ Computer use    │  │ Browser       │
       │ 42 builtin │  │ manager +   │  │ service +     │  │ session ladder  │  │ Playwright    │
       │ + plugins  │  │ bridge      │  │ ingestion     │  │ + drivers       │  │ flows/gates   │
       └────────────┘  └─────────────┘  └───────────────┘  └─────────────────┘  └───────────────┘
```

One rule holds everywhere: **capability gates before advertising, policy gates before executing, journals after everything.** A tool the session cannot actually run is withheld with a reason, not registered and left to fail.

---

## 2. The six subsystems

### 2.1 Onboarding (`onboarding/`)

An eight-step guided flow (`ua setup`), fully scriptable for CI (`--provider … --api-key-env …`):

1. **Provider** — catalog-driven picker (vendor entries with defaults, env-var hints, base-URL requirements)
2. **Endpoint** — only for resource-specific providers (Azure-style)
3. **Credential** — env ref / file ref / keyring; secrets never land in config files, only references
4. **Validation** — live credential check with actionable failures
5. **Model** — ranked listing (tool-capable > context > cost), real metadata from the provider
6. **Persist** — writes the profile; *persists `supports_vision` from live model metadata*, so later capability checks run on truth, not guesses; drops the stock placeholder profile
7. **Tool-call proof** — a real round-trip before the user depends on it
8. **Capabilities** — one line of fact about vision ("your model can/cannot see" + the exact knob to fix it), plus an opt-in MCP quick-start that adds `filesystem` + `fetch` from the catalog

Design rule: cancellation at any step writes nothing; non-interactive mode either has enough flags or fails with the exact command to run.

### 2.2 Config system (`config/`)

**Layers** (later wins): built-in defaults → user config (`~/.config/unknown-agent/config.toml`) → project config (`<repo>/.unknown/config.toml`) → environment → CLI flags. `LoadedSettings` remembers which file contributed what, so `ua config show` can attribute every value.

**Schema** is one Pydantic tree (`config/models.py`): `Settings` holds `providers.{name}` (multi-profile), `agent`, `policy`, `browser`, `memory`, `reasoning`, `skills`, `orchestration`, `mcp`, `vision`, `computer_use`, `appearance`. Validation happens at load *and* before every write (`_write_server` in the MCP CLI validates the merged tree before touching disk — a bad `ua mcp add` cannot brick the config).

**Editing UX**: `ua config show/get/set/unset/validate/init/edit` — users never need to hand-edit TOML, but the files stay human-readable if they want to.

### 2.3 MCP (`mcp/`) — the expansion system

Official Python SDK (2.x), spec-current transports: **stdio**, **streamable HTTP**, **SSE**.

- `client.py` — one server connection. Survived two real-world traps, both fixed and pinned by tests: anyio cancel-scopes (deadline goes on `initialize()`, never around transport entry) and the SDK 2.x snake_case rename (`_attr()` reads both spellings; `_normalise_annotations` restores spec camelCase).
- `manager.py` — the fleet: connect/test/snapshot/call, fail-soft (a dead server is a red row, never a traceback).
- `bridge.py` — `McpProxyTool` turns each remote tool into a first-class product tool named `mcp.<server>.<tool>`, with risk derived from **trust × annotations**:

  | trust | readOnlyHint honoured? | resulting risk |
  |---|---|---|
  | `full` | yes | READ |
  | `trusted` | as WRITE floor | WRITE |
  | `sandboxed` | no | EXECUTE |
  | `blocked` | — | never registered |

  Unannotated tools are DESTRUCTIVE by default. Selection order: deny > allow > per-server cap (48) > global cap (120).
- `catalog.py` — curated ecosystem (filesystem, git, memory, fetch, sequential-thinking, time, everything-server) with one-command add.
- CLI: `ua mcp catalog/add/remove/list/show/test/enable/disable/trust/tools` — exit 4 on failed probes, `--json` everywhere.
- Runner integration: MCP tools load at session start, skip cleanly under read-only or a missing SDK, close in `_post_run`.

### 2.4 CLI/TUI (`cli/`, `ui/`)

Typer + Rich. Command groups are panel-organised in `--help`: **Agent** (run/build/fix/plan/ask/shell), **Setup** (setup/providers/models), **Project** (init/doctor/version/inspect), **Quality** (verify/browser/**vision**), **Knowledge** (context/memory), **History** (runs/sessions), **Tools** (tools/**mcp**/plugins/skills), **Configuration** (config).

Shared conventions enforced across every group: `_guarded` (domain errors render, never traceback), `--json` flag flips the output manager to machine mode, global flags hoist (`ua verify --json` ≡ `ua --json verify`), themable palette with `--ascii`/`--no-color` degradation, errors carry hints with the exact next command.

### 2.5 Tools (`tools/`, 42 built-in)

Every tool declares: name, category, **risk** (READ/WRITE/EXECUTE/NETWORK/DESTRUCTIVE), tags, typed `ParameterSpec`s (JSON-schema out), timeout, concurrency-safety, `requires_extra`. Execution is wrapped: argument cleaning, timeout, duration, structured `ToolResult` (ok/output/data/error/artifacts/metadata).

The factory (`tools/factory.py`) is the single assembly point: category filtering, browser availability gating, **vision gating** (`[vision] enabled`), **computer-use double gating** (config + category, withheld with reason when disabled), read-only stripping of mutating tools, then plugins, then MCP proxies.

Advanced tools (`builtin/advanced_tools.py`) follow three rules — deterministic first, structured out, read-mostly:

| tool | what it computes |
|---|---|
| `architecture_map` | module import graph, fan-in/out hotspots, **cycle detection** (AST-based, src-layout aware) |
| `dependency_audit` | declared-vs-imported cross-check, unpinned specs, alias-aware (`PyYAML`→`yaml`) — found a real bug in this very repo on first run |
| `repo_health` | hygiene score (tests/CI/README/license/lockfile), TODO debt, secrets-shaped strings |
| `app_scaffold` | python-cli/api/lib skeletons with tests + packaging; refuses to overwrite |
| `doc_query` | passage retrieval over the project's own docs |
| `plan_review` | deterministic plan critique: vagueness, missing verification, missing rollback |
| `mcp_discover` | session MCP state + catalog suggestions |
| `skill_distill` | writes `.draft.toml` skills — **inert until a human renames them** |
| `ui_validate` | scripted UI hygiene via the browser: alt text, labels, heading order, console errors |

### 2.6 Vision + computer use

**Vision** (`vision/`, `providers/capabilities.py`) — three layers:

1. *Capability resolution* — per (profile, model): profile override > live model metadata > catalog > model-id heuristic > default **false**. Biased to false negatives: a false positive means sending images to a blind model. Every verdict carries its source; `ua vision capabilities` shows the whole matrix.
2. *Ingestion* (`images.py`) — magic-byte sniffing (png/jpeg/gif/webp only), header-parsed dimensions without Pillow, Pillow-optional downscaling at the size cap, and the **exfiltration guard**: tool-initiated remote URLs are refused unless `allow_remote_urls_in_tools = true` (a model that can embed arbitrary URLs into provider requests is a data channel out).
3. *Routing* (`service.py`) — active model sees → use it; else `[vision] fallback_profile` (itself capability-checked, built per-request, closed by owner); else `VisionUnavailable` naming the blind model and the exact knobs. **Live-verified end-to-end against OpenRouter, including the fallback path.**

Provider encoding is per-dialect: OpenAI-compatible `image_url` parts, Anthropic `source` blocks (base64/url), Ollama raw-base64 `images` arrays. `Message.images` stays provider-neutral.

**Computer use** (`computer_use/`) — deliberately distinct from browser automation: browser flows are *deterministic verification*; computer use is an *adaptive observe→decide→act loop*.

- `actions.py` — a **closed vocabulary** (navigate/click/type/key/scroll/hover/wait/back/screenshot/read_dom). Unknown verbs and unknown fields are rejected at parse time; you cannot confirm what you cannot classify.
- `driver.py` — the `Driver` seam (perform one action, report one observation). Shipped: `BrowserDriver` over the managed Playwright session. Native-OS / provider-hosted drivers plug in here without touching the safety layer. *(planned: provider-native computer-use adapters, e.g. Anthropic's tool type, mapped onto the same Action vocabulary)*
- `session.py` — the **safety ladder**, enforced for every action in order: enabled? → action/time budget → host allowlist → denied keys → confirmation policy (`all`/`mutating`/`never`; **fails closed** with no prompter) → JSONL journal + before/after screenshots. Drivers are dumb by design so no new backend can skip the ladder.
- Off by default (`[computer_use] enabled = false`); tools are withheld with the reason, and `computer_act` takes exactly one action per call so confirmations stay meaningful.

---

## 3. Command surface (final)

```
ua setup                      guided 8-step onboarding (scriptable)
ua run/build/fix/plan/ask     the agent
ua shell                      interactive session
ua config show|get|set|unset|validate|init|edit
ua providers list|add|catalog|use …      ua models list|capabilities
ua mcp catalog|add|remove|list|show|test|enable|disable|trust|tools
ua tools list|describe|call
ua browser status|flows|screenshot …
ua vision status|capabilities|analyze
ua skills list|show|scaffold|audit       ua plugins …
ua inspect · ua verify · ua doctor [--network|--install-browsers]
ua context · ua memory · ua runs · ua sessions
ua scheduler create|list|inspect|pause|resume|trigger|runs|cancel|replay|webhook|worker
```

### Persistent automation

`automation/scheduler.py` is the durable trigger and delivery layer. It stores
strict `JobSpec` definitions and execution records in project-local SQLite/WAL,
materializes deterministic occurrence keys, and uses transactional claims with
owner leases and heartbeats. Expired leases recover after process death. Each
job bounds concurrency, attempts, exponential backoff, timeout, input and output;
terminal failures remain queryable as dead letters and replay creates a new
audited occurrence.

The scheduler depends on an async action registry rather than knowing how work is
performed. `automation/actions.py` registers the production `agent.run` bridge,
which invokes `core.runner.run_task` with a strict Pydantic payload and scheduler
identity in run metadata. This preserves one policy, provider, tool, mission,
verification and persistence architecture. Webhook definitions persist only a
secret reference and verify constant-time HMAC-SHA256 before durable enqueue.
See [`scheduler.md`](scheduler.md) for the delivery and operational contract.

### Native subprocess containment

`sandbox/profiles.py` converts strict profile requests into platform-native launch
plans. Linux uses Bubblewrap namespaces and mount bindings, macOS uses Seatbelt,
and Windows uses a restricted-token broker. Shell tools and validation gates pass
through this layer before `host/process.py` starts a process. Required mode fails
closed; auto mode records an explicit degradation. Canonical path checks keep the
working directory and writable mounts inside the project. The process boundary
also enforces the platform-wide 300-second timeout ceiling.

### Stable RPC and ACP

`rpc/jsonrpc.py` provides strict, bounded JSON-RPC 2.0 dispatch and concurrent
NDJSON stdio transport. `rpc/acp.py` implements ACP v1 initialization, durable
session create/load, prompt turns, streaming updates, mode changes, cancellation,
and status. ACP prompts invoke `core.runner.run_task`; they do not bypass policy,
sandboxing, hooks, tools, providers, mission journals, or verification. The CLI
entry point is `ua acp`. See [`rpc-acp.md`](rpc-acp.md).


Doctor check groups: environment, config, toolchain, project, browser, provider, **mcp** (SDK presence, per-server launcher/URL sanity, pointer to `ua mcp test` for live probes), **vision** (routing verdict with the fixing knob, Pillow presence), credentials (`--network`).

---

## 4. Module map (created/changed this cycle)

```
src/unknown_agent/
├── mcp/                      __init__ (SDK gate) · models · client · manager · bridge · catalog
├── vision/                   __init__ · images (ingestion) · service (routing) · tools (3 agent tools)
├── computer_use/             __init__ · actions (vocabulary) · driver (seam + BrowserDriver)
│                             · session (safety ladder) · tools (observe/act pair)
├── providers/
│   ├── capabilities.py       NEW — Capability{value,source} resolution model
│   ├── anthropic.py          image source-blocks in _payload
│   ├── openai_compatible.py  image_url parts (pre-existing, verified)
│   └── ollama.py             raw-base64 images arrays
├── tools/
│   ├── factory.py            vision/computer-use/advanced category wiring + gating
│   └── builtin/advanced_tools.py   NEW — 9 hyperagent tools
├── cli/commands/
│   ├── mcp_cmd.py            full ua mcp group
│   ├── vision_cmd.py         NEW — ua vision group
│   └── doctor.py             _mcp_checks + _vision_checks
├── onboarding/flow.py        step 8 (_capabilities_step) + supports_vision persistence
├── core/runner.py            MCP + VisionService + computer-use session lifecycle
└── config/models.py          McpConfig/McpServerConfig · VisionConfig · ComputerUseConfig

tests/  test_mcp.py (31, incl. 4 live stdio integration) · test_vision.py (38) · test_advanced_tools.py (21)
pyproject.toml  extras: mcp = ["mcp>=1.2"] · vision = ["pillow>=10.4"] · all includes both
```

---

## 5. Implementation order (as executed, for the record)

1. Config schema for MCP/vision/computer-use (validation before any behaviour)
2. MCP client → manager → bridge → runner wiring → CLI → tests (live stdio round-trip proved the SDK integration before anything depended on it)
3. Capability resolution model (the routing substrate both vision and computer use consume)
4. Vision ingestion → service → provider encodings → tools → CLI (live OpenRouter verification, including fallback routing)
5. Computer-use vocabulary → driver seam → safety ladder → tools (offline ladder proofs)
6. Advanced tools + factory gating
7. Onboarding step 8, doctor groups, packaging extras
8. Test suites pinning every behaviour contract — **536 passed**

## 6. What remains open (honest ledger)

- Provider-native computer-use adapters (Anthropic computer-use tool type → Action vocabulary mapping); the seam exists, no adapter is shipped.
- `auto_attach_browser_evidence` (config flag exists; the runner does not yet attach failed-gate screenshots to the model turn automatically — `screenshot_analyze` covers this manually).
- Textual TUI screens for MCP/vision management (CLI surface is complete; `[tui]` extra is scaffolded).
- Image support in `file_read`-style flows for file-ID providers (OpenAI Files API); data-URL and URL paths cover today's dialects.
