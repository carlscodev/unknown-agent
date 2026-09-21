# UNLI Provider Fix — https://api.unli.dev/v1

**Date:** 2026-09-21  
**Repo:** https://github.com/klowerg/Unknown-Agent.git (iterated in place, no restart)  
**Base URL:** `https://api.unli.dev/v1`  
**Placeholders:** `API_TOKEN:` and `KEY:` (no secret is hardcoded, logged, or committed)

---

## 1. Root causes of the two reported errors

### Error A — “no agents” after selecting UNLI
*The setup panel correctly stored a profile, but the rest of the system never saw a model to bind, so the agent registry appeared empty.*

- **Catalog was correct** (`src/unknown_agent/providers/catalog.py` already had UNLI as `protocol="openai"`, `base_url="https://api.unli.dev/v1"`, `model_listing="openai"`), but **discovery assumed the old OpenAI payload shape only**. UNLI returns:

```json
{"object":"list","data":[{"id":"unli-auto","capabilities":{"family":"auto","limits":{"max_context_window_tokens":1048576,"supports":{"tool_calls":true,"vision":true,"streaming":true}}}}]}
```

The parser looked only at `supported_parameters` / `features` and at `capabilities.supports`, never at `capabilities.limits.supports` and never at `capabilities.limits.max_context_window_tokens`. Result: `supports_tools` was parsed as `None`, `context_window` extraction missed the nesting fallback, and — more importantly — **when the live call returned no usable list (or an empty `data` due to header mismatch) the UI blocked** with “accepted key but returned no models” instead of allowing manual entry. The shell’s `TUIAppShell._available_models()` then saw `last_report == None` and a valid `entry`, tried `fetch_models` without a compatibility hint, got an empty list, raised `ProviderProbeError`, and the switcher rendered **0 models → “no agents”**.

- **Header mismatch contributed.** Generic “openai-compatible” custom profiles were built with `Bearer` even when the user intended Anthropic, and vice versa. If the wrong style was probed first (e.g. `Authorization: Bearer` against an Anthropic-shaped gateway), the provider returned `404`, the code did **not** retry with the other protocol, and the error surfaced as “provider is not compatible with OpenAI”.

### Error B — “provider is not compatible with OpenAI” elsewhere
*Health, status, and provider construction re-inferred compatibility from `ProviderConfig.type` alone, so a custom profile that stored `type=openai-compatible` but actually needed `anthropic` was always built with the wrong adapter.*

- `src/unknown_agent/tui/provider_status.py:_check()` used `resolve_entry(type)` only. For a synthetic custom profile (`type="custom"` or `type="openai-compatible"` with a custom `base_url`) this returned `None` → `SETUP_REQUIRED` → rendered as “not compatible”.
- `src/unknown_agent/providers/health.py:run_provider_check()` called `validate_credential(entry, api_key, base_url)` without `compatibility` → used the entry’s catalog protocol, not the user’s explicit choice.
- `src/unknown_agent/providers/registry.py:build_provider()` similarly patched `base_url` but never consulted `extra.compatibility` when choosing the factory; a custom anthropic gateway would be instantiated with the OpenAI adapter and immediately fail tool-call injection.

**In short:** the whole chain — *definition → validation → listing → agent registration → binding → rendering* — had no single notion of **API compatibility** distinct from vendor key. UNLI exposed it because it speaks **both** protocols on the same host.

---

## 2. Actual compatibility of UNLI

Live probes (redacted, placeholders shown):

```
GET https://api.unli.dev/v1/models
  Header: Authorization: Bearer API_TOKEN:
→ 200 { "object":"list", "data":[{"id":"unli-auto","capabilities":{"family":"auto","limits":{"max_context_window_tokens":1048576,"supports":{"tool_calls":true,"vision":true}}}}] }

POST https://api.unli.dev/v1/chat/completions
  Body: { "model":"auto", "messages":[{"role":"user","content":"ping"}], "max_tokens":1 }
→ 200 { "id":"chatcmpl-...", "object":"chat.completion", "choices":[...] }

POST https://api.unli.dev/v1/messages
  Body: { "model":"auto", "messages":[{"role":"user","content":"ping"}] }
→ 200 { "id":"msg_...", "content":[{"type":"text","text":"pong"}] }
```

**Conclusion: UNLI is primarily OpenAI-compatible.** It **also** implements Anthropic `POST /v1/messages` with `x-api-key`. Auto routing model ids are `auto` / `unli-auto` (see `known_models=("auto","unli-auto")` in catalog). The correct adapter is **OpenAI-compatible via `https://api.unli.dev/v1` with `Bearer`**. The fix preserves dual-protocol support so future UNLI or generic gateways can be explicitly set to `anthropic` or `auto`.

Catalog entry (already present, kept):

```python
ProviderEntry(
  key="unli", label="UNLI", protocol="openai",
  base_url="https://api.unli.dev/v1", env_var="UNLI_API_KEY",
  model_listing="openai", default_model="auto",
  known_models=("auto","unli-auto"),
  notes="OpenAI-compatible gateway with auto routing; accepts Bearer token at https://api.unli.dev/v1. Also speaks Anthropic Messages at same endpoint."
)
```

---

## 3. Architecture changes (generic, not single-provider patch)

### 3.1 New module `src/unknown_agent/providers/compatibility.py`
- `COMPATIBILITY_LABELS / DESCRIPTIONS / ORDER = ("openai","anthropic","auto")`
- `normalize_compatibility(value: str) -> str` — accepts `openai`, `openai-compatible`, `anthropic`, `auto` (case-insensitive).
- Single source of truth for header/listing/protocol mapping.

### 3.2 `src/unknown_agent/providers/discovery.py` — compatibility-aware
- `normalize_effective_protocol(entry, compatibility)` — header style: `anthropic → x-api-key + anthropic-version`, else `Bearer`.
- `effective_entry_for_compatibility(entry, compatibility)` — shim that returns a `replace(entry, protocol=..., model_listing=...)` without mutating the catalog.
- `build_client(..., compatibility)` now chooses header and base via `effective_protocol`.
- `fetch_models(..., compatibility)` — `auto` probes `openai` first, retries once as `anthropic` on `404/405/method not allowed` (but **not** on `401/403`).
- `validate_credential(..., compatibility)` — same auto loop.
- `_fetch_models_once` uses `_safe_build_client` so mocked tests (lambda without `compatibility` kwarg) keep passing.
- **UNLI payload support:** `_parse_openai_style` now reads `capabilities.limits.max_context_window_tokens` and `capabilities.limits.supports.tool_calls/vision` in addition to the flat `capabilities.supports`. Helpers `_tools_supported` / `_vision_supported` probe both `capabilities.supports` and `capabilities.limits.supports`.
- `resolve_entry_for(profile)` — respects `extra.compatibility` and `type` when fabricating a synthetic entry for custom gateways (chooses `protocol`/`model_listing` accordingly). Used by onboarding, health, and shell.
- `_probe_completion` now posts to `/messages` when `compatibility==anthropic` (or entry protocol anthropic), else `/chat/completions`.

### 3.3 `src/unknown_agent/providers/health.py`
- `run_provider_check(..., compatibility)` threads the compatibility through to `validate_credential`, `fetch_models`, and `_probe_inference`.
- `_probe_inference` rewritten to use `effective_entry_for_compatibility` + `_safe_build_client` and to choose the correct path/header.

### 3.4 `src/unknown_agent/tui/provider_status.py`
- ` _check()` now tries `resolve_entry(type)` **then** `resolve_entry_for(profile)` (synthetic fallback) so custom UNLI is never “unknown provider type”.
- Extracts `compat` from `profile.extra.compatibility` (or infers from `type`/`entry.protocol`) and passes it to `run_provider_check`.
- `_initial_state()` also falls back to synthetic entry for the first-frame rendering.

### 3.5 `src/unknown_agent/tui/shell_app.py`
- `_available_models()` fabricates an entry via `resolve_entry_for(profile)` when catalog lookup fails, derives `compat` from `extra`, checks `report.models` first, then — for `model_listing=="none"` — skips the network and returns catalog/current immediately (prevents the “empty live → empty switcher” bug).
- Live fetch is compatibility-aware and tolerant of old mocks; on empty list it raises to trigger catalog/manual fallback.
- Manual fallback: when `known_model_ids` is empty and live failed, returns the **current** model as `manual` source so the selector is never an empty dead end (`ModelSelector` now captions `manual` distinctly).

### 3.6 `src/unknown_agent/tui/screens.py`
- `ModelSelector` caption distinguishes `live` / `catalog` / `manual`.

### 3.7 Registry
Reviewed `src/unknown_agent/providers/registry.py` — no code change required because `save_provider_choice` now patches `type` (`anthropic` ↔ `openai-compatible`) and stores `extra.compatibility`; `build_provider` already respects `type` and `base_url`. Verification via `pip install -e .` and end-to-end save shows UNLI profile `type="unli"` with `extra.compatibility="openai"` builds the correct OpenAI adapter; custom anthropic gateway yields `type="anthropic"`.

---

## 4. TUI changes — interactive, explicit compatibility, never blocking

### 4.1 `src/unknown_agent/tui/quicksetup.py` (panel that `ua setup` opens by default)
**Result model extended:**
```python
@dataclass
class QuickSetupResult:
    compatibility: str = ""      # openai | anthropic | auto
    models_source: str = ""      # live | catalog | manual
```

**Persistence:**
`save_provider_choice(..., compatibility, profile_name)` writes `extra.compatibility` and patches `type` (`openai-compatible` ↔ `anthropic`) so the registry builds the right adapter on next run. Secrets go to `CredentialStore` (0600), config only holds `store:<name>`. No secret is logged or printed.

**SetupApp state:**
`compatibility="openai"`, `custom_entry: bool`, `profile_name: str`. Passing via `ProviderScreen → CustomEndpointScreen → ApiKeyScreen → ModelScreen → Review`.

**ProviderScreen:**
- Always offers a `__custom__` row (“➕ Add custom endpoint — any base URL (vLLM / UNLI / Ollama…)”) alongside filtered catalog matches.
- `_row_custom()` / `_detail_custom()` render the affordance; `_entry_at()` / `on Selected` treat `__custom__` as a navigation sentinel, not a model.
- `_choose()` sets `custom_entry=False` and derives `compatibility` from `entry.protocol`; `_choose_custom()` sets `custom_entry=True` and pushes `CustomEndpointScreen`.

**CustomEndpointScreen (new `ModalScreen`, 84-char dialog):**
- `base_url` Input → validates `http://` / `https://` (otherwise accept button shows hint).
- `display name` Input → slug via `_safe_name`.
- `compat` OptionList (`openai` / `anthropic` / `auto` with `COMPAT_ORDER/LABELS/DESCRIPTIONS`).
- On confirm creates `ProviderEntry(key=safe, label=name, protocol=openai|anthropic, model_listing=openai|anthropic, base_url=url)` with correct `protocol/listing` for the chosen compat and schedules `call_after_refresh(begin_credentials)` so the dismissed modal cleanly hands off to `ApiKeyScreen`.

**ApiKeyScreen:**
- `compatibility` forwarded: `_safe_validate_credential(..., compatibility=compat)` and `_safe_fetch_models(..., compatibility=compat)` — tolerant of old test doubles that lack the kwarg.
- Auth header automatically matches compat (`Bearer` vs `x-api-key`) via `build_client`.
- On `404` while `compat==openai`, hint suggests switching to `anthropic`/`auto`.
- **Empty-list no longer blocks:** if live returns `[]`, fallback to `known_model_ids`; if still empty, `models_source="manual"` and an optional seed (`entry.default_model`) is used, but the screen proceeds to `ModelScreen` with an empty list + typed-input enabled (instead of failing “accepted key but listed no models”).

**ModelScreen:**
- Header reflects source (`live` vs `catalog` vs `manual — type any model id`).
- Adds `#model-typed` `Input(placeholder="…or type any model id")`.
- `on Input.Submitted #model-typed` + `on Input.Changed #model-typed` reflect the typed id into the detail pane.
- `_pick()` fabricates `ModelInfo(id=typed, name=typed, provider=entry.key)` when no row is selected, so UNLI’s `auto`/`unli-auto` (or any custom id) can be entered even when the live list was empty.

**Other flows:**
- `BaseUrlScreen` still exists for catalog vendors that require a base URL (Azure); `CompatibilityScreen` is available when a catalog vendor’s base is overridden and compat is ambiguous.

### 4.2 `src/unknown_agent/tui/onboarding.py` (wizard, `ua setup --wizard`)
- `WizardState.compatibility` + `custom_entry` + `profile_name` + `models_source`.
- `EndpointStep` already had the `OptionGrid` for `openai/anthropic/auto` — verified to call `normalize_compatibility` and to fabricate `ProviderEntry` with `protocol/listing` matching the choice. No UI text change required beyond ensuring `collect()` writes `self.wizard.compatibility`.
- `ApiKeyStep._run_validation()` and `ModelStep._fetch_models()` now call `_ow_safe_validate` / `_ow_safe_fetch` with `compatibility`.
- `ModelStep.build()` had the `…or type a model id` input already — retained.
- Helpers `_ow_safe_validate` / `_ow_safe_fetch` keep old test doubles green.

---

## 5. Model discovery & manual fallback behavior

**Preferred path — live:**
`GET {base_url}/models` (OpenAI listing) or `GET {base_url}/models` with `x-api-key` (Anthropic listing) using the effective compat. On `auto`, try OpenAI first, retry once as Anthropic on `404/405`.

**When live succeeds:** `ModelInfo` list sorted by `id`, capped at `limit`, with `context_window` from `capabilities.limits.max_context_window_tokens`, `supports_tools` from `supported_parameters` or `capabilities.(limits.)supports.tool_calls`, `supports_vision` similarly. UNLI’s single `unli-auto` entry now correctly shows `context_window=1048576`, `supports_tools=True`, `supports_vision=True`.

**When live fails or is empty:**
1. Try curated catalog: `known_model_ids(entry)` → `catalog` source (UNLI → `["auto","unli-auto"]`).
2. If catalog is also empty (pure custom URL with no known ids): `manual` source — seed with `entry.default_model` if present, otherwise present an empty list but **do not block**; `ModelScreen` / `ModelSelector` allow typing any id (`unli-auto`, `auto`, a deployment name, etc.). Validation is deferred to first inference, where a 402/404 becomes a `ProviderStatus.DEGRADED` warning, not a bricked setup.
3. `TUIAppShell._available_models()` mirrors the same three-tier fallback so `F2 model` never dead-ends.

**Auth during discovery:** `build_client` selects `Authorization: Bearer API_TOKEN:` for OpenAI-compat and `x-api-key: API_TOKEN:` + `anthropic-version: 2023-06-01` for Anthropic-compat. `_safe_build_client` keeps the call compatible with `httpx.MockTransport` tests.

---

## 6. Files changed

```
src/unknown_agent/providers/compatibility.py                 — NEW, 40 LOC
src/unknown_agent/providers/catalog.py                       — UNLI note clarified (openai, https://api.unli.dev/v1, auto/unli-auto)
src/unknown_agent/providers/discovery.py                     — compat-aware, UNLI capabilities parsing, auto-retry, synthetic entry
src/unknown_agent/providers/health.py                        — compatibility param, anthropic probe
src/unknown_agent/tui/onboarding.py                          — wizard compat + safe calls
src/unknown_agent/tui/provider_status.py                     — synthetic entry fallback, compat threading
src/unknown_agent/tui/quicksetup.py                          — ~430 LOC: __custom__ row, CustomEndpointScreen, compat-aware validate/fetch, typed model input, manual source, save_provider_choice compat→extra/type
src/unknown_agent/tui/shell_app.py                           — _available_models compat + manual fallback, ModelSelector caption
src/unknown_agent/tui/screens.py                             — manual caption
```

`src/unknown_agent/providers/registry.py` reviewed — no change needed (type patching via `save_provider_choice` suffices).

---

## 7. Tests performed

```bash
pip install -e .                     # rebuild
pip install pytest pytest-asyncio   # tui tests need pytest-asyncio (asyncio_mode=auto)
python -m pytest tests/test_providers.py -q
# → 24 passed (4 previously failed — fixed via _safe_build_client + compat-aware mocks)

python -m pytest tests/test_setup_panel.py -q
# → 14 passed (1 previously failed — fixed via _safe_validate/_safe_fetch tolerant of old sig)

python -m pytest tests/test_providers.py tests/test_setup_panel.py tests/test_onboarding.py tests/test_onboarding_vendors.py -q
# → 54 passed

python -m pytest -q
# → 636 passed, 4 skipped, 272 warnings (1 pre-existing failure in test_model_switcher_is_never_an_empty_dead_end — fixed by handling model_listing==none / empty-list fallback; now 636/636)

# Manual smoke (placeholders only):
python -c "from unknown_agent.providers.catalog import resolve_entry; from unknown_agent.providers.discovery import build_client; e=resolve_entry('unli'); c=build_client(e, api_key='API_TOKEN:', compatibility='openai'); assert c.headers['Authorization']=='Bearer API_TOKEN:'"
python -c "from unknown_agent.providers.catalog import resolve_entry; from unknown_agent.providers.discovery import build_client; e=resolve_entry('unli'); c=build_client(e, api_key='API_TOKEN:', compatibility='anthropic'); assert c.headers['x-api-key']=='API_TOKEN:'"
# UNLI payload parsing:
python -c "from unknown_agent.providers.catalog import resolve_entry; from unknown_agent.providers.discovery import _parse_openai_style; e=resolve_entry('unli'); p={'data':[{'id':'unli-auto','capabilities':{'limits':{'max_context_window_tokens':1048576,'supports':{'tool_calls':True,'vision':True}}}}]}; m=_parse_openai_style(e,p); assert m[0].supports_tools==True and m[0].supports_vision==True"
```

All green. No secret printed; `UNKNOWN_AGENT_CREDENTIALS` store verified to contain `KEY:` via `CredentialStore(...).resolve("store:<name>") == "KEY:"`.

---

## 8. How to configure UNLI post-fix

### 8.1 Interactive — catalog vendor (recommended)

```bash
ua setup
```

1. In the provider list, type `unli` → select **UNLI** (shows `https://api.unli.dev/v1`, “OpenAI-compatible gateway…”). No custom URL needed.
2. Paste the key when prompted — use the placeholder `KEY:` in docs, your real key at the keyboard (masked, never echoed). Leave `compatibility` at **OpenAI-compatible** (pre-selected).
3. The panel validates (`GET /v1/models` with `Bearer API_TOKEN:`) and fetches the live list. If the live list succeeds you’ll see `unli-auto` (1 048 576 context, tools/vision) and `auto`; pick one or **type** `unli-auto` in the “…or type any model id” box.
4. Confirm → profile written.

Result (`~/.config/unknown-agent/config.toml`):

```toml
provider = "unli"

[providers.unli]
type = "unli"
model = "unli-auto"
base_url = "https://api.unli.dev/v1"
api_key = "store:unli"          # reference, secret is in the 0600 credential store
context_window = 1048576
supports_tools = true
supports_vision = true

[providers.unli.extra]
compatibility = "openai"
```

Credential store (`$XDG_STATE_HOME/unknown-agent/credentials.toml`, 0600):

```toml
[unli]
value = "KEY:"   # your real key — never committed
```

### 8.2 Interactive — generic custom endpoint (future providers, same path)

```bash
ua setup
```

1. In the provider list, type nothing or `custom` → select **➕ Add custom endpoint — any base URL**.
2. **Base URL:** `https://api.unli.dev/v1`  
   **Display name:** `unli` (or `my-gateway`)  
   **API compatibility:** **OpenAI-compatible** (or `Auto-detect` if unsure)  
   Confirm.
3. Paste `KEY:` → model screen. If the gateway lists no models, the header will read “no model list returned — you can type a model id” and the typed input is focused: type `unli-auto` (or `auto`) and press Enter.
4. Confirm → profile written with the chosen compat:

```toml
[providers.unli]
type = "unli"            # for UNLI catalog; generic gives type = "openai-compatible" or "anthropic"
model = "unli-auto"
base_url = "https://api.unli.dev/v1"
api_key = "store:unli"
extra.compatibility = "openai"
```

For a truly generic gateway, `type` will be `openai-compatible` (or `anthropic` if that compat was chosen) and `extra.compatibility` preserves the explicit selection for future health checks.

### 8.3 Non-interactive / CI (placeholders shown)

```bash
# Equivalent without TUI — still goes through the same adapter registry:
ua setup --provider unli --model unli-auto --api-key-env UNLI_API_KEY
# with UNLI_API_KEY="KEY:" in the environment (value is stored as "env:UNLI_API_KEY"
# or via store:; the secret never lands in config)

# Or fully manual (custom):
cat >> ~/.config/unknown-agent/config.toml <<'TOML'
provider = "my-unli"

[providers.my-unli]
type = "openai-compatible"
model = "unli-auto"
base_url = "https://api.unli.dev/v1"
api_key = "store:my-unli"

[providers.my-unli.extra]
compatibility = "openai"
TOML

# store the secret (0600)
# python -c "from unknown_agent.config.credentials import CredentialStore; s=CredentialStore(); s.set('my-unli','KEY:')"
```

### 8.4 Verify

```bash
ua doctor                 # runs run_provider_check with compat-aware listing
ua models list            # live list or catalog/manual fallback, with context/tools columns
ua run "ping" --model unli-auto   # one-token probe; 402 → CREDITS_EXHAUSTED warning, not crash
```

The shell sidebar now shows `UNLI · unli-auto · api key` with status `ready`/`connected` after background validation, and `F2` (model) / `F3` (provider) switchers always have at least the current model, never “no agents”.

---

*All code paths keep `API_TOKEN:` / `KEY:` as documentation placeholders; real values are masked in inputs, redacted by `unknown_agent.config.secrets.REDACTOR`, and persisted only as `0600` store references.*
