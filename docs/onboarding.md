# Onboarding & Provider Configuration

Onboarding is not a wizard bolted onto the CLI — it is the front door of the product.
This document is the reference for how a fresh install travels from `pipx install` to a
running agent, and how every provider is wired so the agent's tool calling works the
same on all of them.

A captured transcript of the real screens lives in [`onboarding-preview.txt`](onboarding-preview.txt).

---

## 1. The ten-second version

```bash
pipx install unknown-agent        # or: pip install -e .
ua                                # first run → setup panel → agent shell
```

After install, typing the command name is enough. If a provider is configured you land in
the agent shell; if not, **the interactive setup panel opens automatically**; if the config
is broken you get a repair path that names the exact problem.

### The setup panel (`unknown_agent/tui/quicksetup.py`)

The default configuration experience is a four-panel Textual app. No flags, no follow-up
commands:

| panel | what happens |
| ----- | ------------ |
| `ProviderScreen` | every catalog provider in a filterable selector; ↑↓ + Enter |
| `ApiKeyScreen` | modal dialog, masked input; **Enter validates the key and, on success, fetches the model catalog** in one step |
| `ModelScreen` | the fetched models, ranked tool-capable first, with context window / tools / vision / price; Enter selects |
| `ReviewScreen` | one confirm, then the profile is written and the credential stored at `0600` |

Failure handling is part of the UI: an invalid key keeps you in the dialog with the
provider's reason in plain language and the field re-focused for a retry; a provider that
accepts the key but cannot list models falls back to the curated catalog and says so;
`Esc` at any point leaves the existing configuration untouched.

Changing your mind later uses the same screen: `ua models use` with no argument (or
`/model` in the shell) reopens the model selector with your current choice highlighted,
so switching models never means remembering an id either. Passing an id still works for
scripts and muscle memory.

`unknown_agent/onboarding/panel.py:launch_setup` is the single entry point every caller
uses. It picks the panel when there is a real TTY and `textual` is importable, and the
Rich prompt flow (`onboarding/flow.py`) for CI, `--json`, `--non-interactive`, piped
stdin, or `ua setup --classic`. Any explicit flag (`--provider`, `--api-key-env`, …)
routes to the scripted flow so automation is unchanged.

Scripted installs skip the UI entirely:

```bash
ua setup --provider groq --api-key-env GROQ_API_KEY      # key from the environment
ua setup --provider openrouter -m openai/gpt-4o-mini --api-key sk-or-v1-...
ua doctor --network                                      # prove it works end to end
```

---

## 2. Startup flow behaviour

```
ua
 │
 ├─ parse flags / route bare text  →  cli/app.py:_route_default (flag-aware)
 ├─ load settings (defaults < user < project < env < flags)  →  config/loader.py
 └─ detect_setup_state(settings)   →  onboarding/detect.py
      │
      ├─ READY         → agent shell (or the requested command) · no ceremony
      ├─ NEEDS_SETUP   → onboarding (interactive) | exit 4 with `ua setup` hint (machine)
      └─ NEEDS_REPAIR  → repair path: the failing profile, the reason, the fix
```

`detect_setup_state` never touches the network — it runs on every CLI start and only
inspects config, credential references and the environment. It distinguishes three
things a naive implementation conflates:

| Situation | State | What the user sees |
| --- | --- | --- |
| No profile configured yet (the stock `[providers.default]` placeholder) | `needs_setup` | Welcome, provider chooser |
| The active profile has no key / no model / no tool support | `needs_repair` | "Profile `groq` has no API key" + the fix |
| Active profile resolves and is complete | `ready` | The command runs |

Live checks (key accepted, model listing) belong to onboarding and to
`ua doctor --network`, never to startup.

Non-interactive sessions (no TTY: pipes, CI, `ua < file`) never reach a prompt:

```bash
ua "add a healthcheck endpoint"      # one task, then exit
echo "fix the failing test" | ua     # the task comes from stdin
ua --provider offline "say hello"    # provider/profile overrides apply to the session
ua </dev/null                        # prints the usage hint, exit 2 - never hangs
```

---

## 3. Provider abstraction

One internal contract, many vendors. Adapters are **dialects**, vendors are **data**.

```
ProviderEntry (catalog.py)                     # vendor metadata, one row per provider
   key, label, protocol, base_url, env_var, model_listing,
   supports_tools/streaming/json/reasoning/vision, known_models, ...
        │  profile_defaults  →  [providers.<name>] block
        ▼
ProviderConfig (config/models.py)              # what the user owns; type = vendor key or dialect
        │  protocol_for(type) → "openai-compatible" | "anthropic" | "ollama" | "offline"
        ▼
ModelProvider (providers/base.py)              # the adapter: complete/stream/health
        │  encode_tools · canonical_tool_response · parse_tool_calls
        ▼
ToolCall / Message / ModelResponse (core/models.py)   # single internal shape
```

Each adapter is responsible for exactly one dialect and reports its capabilities:

* auth requirements (Bearer, `x-api-key` + `anthropic-version`, none for local);
* base URL handling and required-root validation (`azure-openai` demands your endpoint);
* headers and vendor query parameters (`azure` needs `api-version`, OpenRouter wants
  `HTTP-Referer`/`X-Title` for attribution);
* model listing — `/models`, `/api/tags`, or "not supported, use the curated list";
* request/response formatting, streaming parsing, error normalisation;
* capability reporting: tools, streaming, JSON mode, reasoning, vision, listing.

Because the vendor key is stored as the profile `type`, a profile reads
`type = "groq"` rather than `type = "openai-compatible"` — which is what users see in
`ua providers list`, doctor output and logs. The registry maps vendor → dialect when it
builds the adapter, so vendor knowledge stays in one place.

### Adding a provider (no copy-paste)

1. Append a `ProviderEntry` to `CATALOG` in `providers/catalog.py` (metadata only);
2. if the vendor speaks a new dialect, add one adapter module implementing
   `complete`, `stream`, `encode_tools`, `canonical_tool_response`, `parse_tool_calls`
   and `health`, then `register_provider("dialect", factory)`.

Nothing else changes: the catalog feeds `ua providers catalog`, the onboarding chooser,
discovery, doctor and the model chooser automatically. `tests/test_providers.py` proves
every entry builds a working adapter and passes the tool-call round trip, so a dead
integration cannot be merged.

### The shipped catalog (21 entries)

`openrouter · openai · anthropic · mistral · deepseek · xai (grok) · groq · google
(gemini) · together · fireworks · cerebras · nvidia · moonshot (kimi) · qwen (dashscope)
· zhipu (glm) · cohere · perplexity · azure-openai · ollama · lmstudio · offline`

Aliases (`grok`, `gemini`, `kimi`, `glm`, `dashscope`, `azure`, `local`, `lm-studio`)
resolve to the same entries. **OpenRouter is the recommended default**: one key, one
OpenAI-compatible schema, and a live listing of 400+ models with pricing, context windows
and modality metadata.

---

## 4. Model discovery strategy

Per provider, in this order:

1. **Live listing** where the vendor supports it — `GET {base}/models` parsed by a
   vendor-aware parser (OpenRouter's `supported_parameters` → tools, `architecture.
   input_modalities` → vision, `pricing` → $/Mtok; Ollama's `/api/tags`; Anthropic; …);
2. **Curated list** when listing is unsupported (`fireworks`, `perplexity`, `azure-openai`
   deployments) — `known_models` in the catalog, so onboarding is never empty-handed;
3. **Cache** — the last successful listing per provider is stored in
   `<project>/.unknown/cache/models.json`, so `ua models list` is instant and
   `--refresh` is explicit.

The chooser ranks before it shows: tool-capable first, embeddings and `:batch` variants
last, cheaper and larger-context earlier. A prompt asking for a fragment, a number, or
`all` re-filters instead of dumping hundreds of rows in your face.

---

## 5. Tool-call normalisation

The agent loop only ever sees `ToolCall(name, arguments, id)`. Dialect differences are
absorbed at the adapter boundary:

| Concern | OpenAI-compatible | Anthropic |
| --- | --- | --- |
| Tool declaration | `tools: [{type: function, function: {...}}]` | `tools: [{name, description, input_schema}]` |
| Model's request | `message.tool_calls[].function` (+ JSON string args) | `content[]` block of `type: tool_use` |
| Result fed back | `role: tool` message with `tool_call_id` | `user` message with `tool_result` block |
| Streaming deltas | indexed `delta.tool_calls` fragments, merged | `content_block_delta` events |

`providers/toolkit.py` holds the pure translation helpers plus
`check_tool_roundtrip(provider)`, which asserts three things: the provider claims tool
support, it encodes a schema, and it decodes a canonical tool call. Onboarding runs it
right after saving (`step 7`), `ua providers test` runs it on demand, and `ua doctor`
reports it as a check — so "tools don't work on that provider" is a *tested* claim, not a
hope.

When a model genuinely cannot call tools (some free/vision-only models), the product says
so — `capability_line` in onboarding, a `warn` in doctor, tool schemas withheld from the
request — instead of failing silently mid-run.

---

## 6. Configuration & secrets storage

Precedence: **defaults < user config < project config < `UNKNOWN_AGENT_*` < CLI flags**.

```
~/.config/unknown-agent/config.toml        Linux: $XDG_CONFIG_HOME/unknown-agent/  (Windows: %APPDATA%\unknown-agent\)
~/.config/unknown-agent/credentials.toml   secrets only, chmod 0600
```

```toml
[providers.openrouter]
type = "openrouter"
model = "openai/gpt-4o-mini"
base_url = "https://openrouter.ai/api/v1"
api_key = "store:openrouter"          # → credentials.toml (0600)
# api_key = "env:OPENROUTER_API_KEY"  # → environment variable
# api_key = "keyring:unknown-agent/openrouter"
```

Rules the code enforces:

* a raw secret is never written into `config.toml` — onboarding normalises what it
  receives to `env:` or `store:` references;
* pasted keys are registered with the process-wide `REDACTOR` before anything else can
  print, log or journal them, and are displayed only as a fingerprint (`…9b0, 73 chars`);
* `ua config show` prints the *reference*, never the value; `--explain` shows which
  source produced each value;
* `credential_audit`/`ua doctor` warn when a config file contains something that looks
  like a live credential.

The offline provider needs no key at all, so CI and evaluation environments never require
secrets — which is also how the test suite stays hermetic.

---

## 7. Error handling

Every failure mode has a designed answer:

| Failure | What the user gets |
| --- | --- |
| Key rejected (401/403) | "the provider rejected that key", hint to re-paste or switch provider, 3 attempts, then a clean cancel |
| Insufficient credit / quota (402) | Provider's own remedy, plus the adapter automatically retries once with a smaller `max_tokens` instead of failing your task |
| Rate limit (429) | "rate-limited", `retry-after` seconds, exponential backoff already applied |
| Unknown model (404) | `difflib` suggestion of the closest real model + `ua models list` hint |
| Listing unsupported | Falls back to the curated catalog, and says so |
| Network unreachable | Human hint ("check connection, or pick another provider"), retry / continue-with-catalog / abort |
| Empty prompt (Enter, EOF, Ctrl-D) | `setup cancelled` panel: which step, and that nothing was written |
| Abort anywhere | Config untouched; re-run anytime |

Error normalisation lives in `providers/toolkit.py` (`normalise_provider_error`) and is
what the agent loop puts in run blockers — raw HTTP bodies are evidence for the journal,
not the message on screen.

---

## 8. CLI surface

```
ua                              First run → onboarding; afterwards → agent shell
ua setup                        Full flow: provider → key (masked) → validate → models → model → save → tool check
  -p, --provider                Preselect a provider (name, alias or number)
  -m, --model                   Preselect a model (auto-picks the best ranked when omitted)
      --api-key                 Non-interactive key (stored, never echoed)
      --api-key-env             Read the key from an environment variable (recommended)
      --base-url                Override the endpoint (Azure, self-hosted gateways)
      --as                      Profile name (default: provider key)
      --skip-check              Offline/air-gapped setups
      --force                   Reconfigure even if already ready

ua providers list               Configured profiles, credential state, which one is active
ua providers add <provider>     Add a profile without touching the existing ones
ua providers use <name>         Switch the active profile
ua providers test [name]        Live: key + endpoint + model listing + tool-call round trip
ua providers remove <name>      Remove a profile (--keep-credential to keep the key)
ua providers catalog            Every supported provider with capabilities and docs links
ua providers detect             Readiness state (json: needs_setup | needs_repair | ready)

ua models list [-s TEXT]        Models for the active provider (--tools, --limit, --refresh)
ua models use <model-id>        Switch the default model (--no-check to skip validation)
ua models refresh               Re-fetch and re-cache the listing

ua doctor [--network]           Environment, config, provider, credentials, toolchain, project, browser
ua config show [--explain]      Effective configuration and where each value came from
```

Exit codes: `0` ok · `4` configuration (needs setup) · `5` provider failure · `6` policy ·
`7` gate failed · `130` interrupted. Machine output (`--json`, `--ndjson`) is written
raw, never through the Rich renderer.

Inside the shell: `/setup`, `/provider`, `/model`, `/models`, `/tools`, `/verify`,
`/doctor`, `/runs`, `/clear`, `/exit`.

---

## 9. Module structure

```
src/unknown_agent/
├── providers/
│   ├── catalog.py        ProviderEntry × 21, aliases, protocol map, profile defaults
│   ├── base.py           ModelProvider contract: complete/stream/health + tool-call dialect hooks
│   ├── openai_compatible.py   OpenAI dialect (+ OpenRouter/Groq/DeepSeek/... via metadata)
│   ├── anthropic.py      Anthropic Messages dialect (tool_use / tool_result blocks)
│   ├── ollama.py         Local models, no key, /api/tags
│   ├── offline.py        Deterministic provider for CI and evaluation
│   ├── discovery.py      build_client, fetch_models (+ vendor parsers), validate_credential/profile
│   ├── toolkit.py        Tool translation, round-trip check, capability report, error normalisation
│   └── registry.py       protocol_for(vendor) → adapter factory, build_provider, profile materialisation
├── onboarding/
│   ├── detect.py         SetupState + detect_setup_state (no network)
│   ├── screens.py        banner, step headers, provider/model tables, key prompt, success/failure/info panels
│   └── flow.py           7-step machine, non-interactive flags, persistence, tool verification
├── config/
│   ├── models.py         Typed settings; ProviderConfig.type accepts a vendor key or a dialect
│   ├── credentials.py    CredentialStore — 0600 TOML, audit, store:<name> references
│   ├── secrets.py        env: / file: / keyring: / store: resolution + REDACTOR
│   └── loader.py         Precedence, provenance, validation warnings
└── cli/
    ├── commands/providers_cmd.py   setup, providers *, models *
    ├── shell.py          The agent shell (slash commands, streams events, Ctrl-C cancels a task)
    └── app.py            Routing: bare text → shell, first-run → onboarding
```

## 10. Implementation order (what was built, in order)

1. `providers/catalog.py` — vendors as data (the extension point everything else reads).
2. `config/credentials.py` + the `store:` branch in `config/secrets.py` — secure storage first.
3. `providers/discovery.py` — validation, listing, normalised probe errors.
4. `providers/toolkit.py` + the tool-call hooks on `ModelProvider`/Anthropic — the contract.
5. `config/models.py` — vendor-key profile types + placeholder detection.
6. `onboarding/*` — detection, screens, the 7-step flow.
7. `cli/commands/providers_cmd.py` + `cli/shell.py` + `cli/app.py` — the surface, routing.
8. `ua doctor` provider group — diagnostics that match what onboarding promised.
9. Tests (`test_providers.py`, `test_onboarding.py`) — including "every provider works"
   and "keys never leak", then live verification against OpenRouter.

## 11. Verification (how this was proven, not assumed)

```
119 tests pass (tests/)                    pytest -q
21/21 catalog entries build + round-trip   tests/test_providers.py::test_every_provider_builds_a_working_adapter
live key accepted, 446 models listed       ua setup --provider openrouter …  (OpenRouter)
real tool-calling run end to end            ua run "Read tests/test_main.py with read_file …"  → completed, 10 steps
secrets absent from config and output       tests/test_onboarding.py::test_setup_flow_persists_config_and_hides_the_key
```

Known limits, stated rather than hidden: `azure-openai` requires your deployment name as
the model; `lmstudio`/`perplexity` do not advertise tool support for every model (the
capability line shows it); free OpenRouter models can reject long prompts — the adapter's
credit-aware retry exists because that is a real failure mode, not a hypothetical one.
