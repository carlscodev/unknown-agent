# Changelog

All notable changes to Unknown Agent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **F3 (provider), F4 (mode) and ctrl+p (palette) crashed** with
  `NoActiveWorker: push_screen must be run from a worker`. Every action that opens a
  modal now runs inside a Textual worker. F2 only appeared to work because it
  early-returned when no models were configured; it had the same latent bug.
- **ctrl+c never cancelled a running task.** `get_current_worker` is a module-level
  function, not an `App` method, so a `hasattr` guard silently left the task handle
  unset and the binding was a no-op.
- The model switcher was a dead end for `openai-compatible` profiles, which resolve to
  no catalog vendor: it offered an empty list. A completed health check is now
  consulted first, and the configured model is always offered as a fallback.
- An error raised outside two hardcoded regions never reached the provider status
  controller, so genuine 402/401 failures could go unclassified.
- The suggestion popup could crash layout with
  `'NoneType' object has no attribute 'get_height'`: an empty `Static` resolves to a
  null visual that Textual cannot measure under `height: auto`.
- The help modal silently truncated its last entries; it now scrolls, with keys.
- Selected rows in the palette and selectors had no visible highlight (the class is
  `-highlight`, not `--highlight`), so Enter had no visible target.

### Added
- **Real slash commands.** Typing `/` in the composer opens an inline, filter-as-you-type
  popup: arrow keys navigate, Tab completes, Enter runs, Esc dismisses. Previously
  typing `/` did nothing at all.
- `unknown_agent.tui.design` — a design-token module (`Tokens`, `DARK`, `LIGHT`,
  `GLYPH`, and formatting helpers) that is the single source of colour and glyphs.
- `unknown_agent.tui.commands` — `Command`/`CommandRegistry` with per-command
  availability predicates, so a command that cannot run is shown disabled with the
  reason rather than failing when invoked.
- `unknown_agent.tui.screens` — palette, model/provider/mode selectors, help and
  confirm dialogs, all sharing one chooser so they behave and look identical.
- A quit confirmation when a task is running, so work is not silently discarded.
- `f6` toggles light/dark; the transcript is re-rendered under the new tokens.
- 19 regression tests covering shortcut safety, slash-command UX, availability
  handling and cancellation (`tests/test_tui_interaction.py`).

### Changed
- **Redesigned the TUI**: the neon cyan/violet/pink palette is replaced by a neutral
  slate ramp with a single muted accent. Colour is now reserved for state and focus;
  structure is carried by weight, spacing and alignment. No emoji.
- Header, sidebar cards, notice strip and footer rebuilt on the shared tokens; the
  footer is generated from the bindings so it cannot advertise a key that is missing.
- Only implemented shortcuts are bound and shown; nothing unfinished is exposed.
- `tui/palette.py` was replaced by `tui/commands.py` + `tui/screens.py`.
- The `settings` test fixture moved to `tests/conftest.py`.

## [1.1.3] - 2026-09-21

### Fixed
- **TUI `no agent attached — this is a UI-only session` when typing `Hola` despite provider/model configured.** Root cause: `src/unknown_agent/cli/commands/misc.py:395-428` `tui_command` loaded `Runtime.load().settings` then called `await run_app_shell(settings, first_task)` with `runner=None`; `src/unknown_agent/tui/shell_app.py:1165 run_app_shell` created `TUIAppShell(settings, runner=None)`; `TUIAppShell.__init__:247-256` stored `self._runner=None`; `run_task:932-941` early-returned `if self._runner is None: note UI-only` before `state.can_send` check, so valid provider/model never became live backend. No auto-attach via `cli/runtime.py:251 make_runner` / `core/runner.py:948 run_task` / `providers.registry.build_provider`. Distinction configured→resolved→attached→session-ready not enforced. Fix: `TUIAppShell` now auto-attaches via `_diagnose_attachment` / `_build_chat_runner` / `_ensure_runner` (eager validation, `_attach_error` surfacing, `active_profile_name`/`build_provider`/`LoopHooks` wiring), `on_mount` and `_apply_provider_state` retry attachment when health becomes `can_send`, `run_app_shell` eagerly attaches for headless tests, `_run_setup`/`action_switch_model`/`action_switch_provider`/`action_retry_provider` clear and re-attach so the next message is live, and `run_task` lazily ensures runner and shows precise diagnostics instead of generic yellow warning. **How `Hola` now streams correctly:** `_build_chat_runner` builds the active provider via `build_provider`, prefers `provider.stream` when `supports_streaming` (OpenAI-compatible SSE at `providers/openai_compatible.py:186`, Anthropic SSE at `providers/anthropic.py:178`, Ollama chunked JSON at `providers/ollama.py:115`) with fallback to `provider.complete` (offline at `providers/offline.py:89`), streaming incremental `chunk.delta` via `hooks.on_message` so the transcript updates live and `Usage` is recorded for the dashboard; verified with `Hola`→`¡Hola! ¿Cómo estás?` streaming 5 chunks + final `Usage`.
- **`/setup` slash command inside TUI crashed/did nothing.** Root causes: `TUIAppShell._run_setup:810` used `await asyncio.to_thread(lambda: asyncio.run(run_setup_panel()))` inside `with self.suspend():` which fails headless (`App.suspend is not supported in this environment`) and races in a real terminal; and `invoke:735` checked only `asyncio.iscoroutine(result)` so `@work`-decorated `action_run_setup` returning `Worker` was ignored (direct `await app.action_run_setup()` raised `TypeError: object Worker can't be used in 'await' expression`). Fix: `_run_setup` now does `with self.suspend(): result = await run_setup_panel()` in the same loop with headless fallback (`run_setup_panel` without suspend), `invoke` now handles both `Worker` and coroutine via `isinstance(result, Worker)` vs `run_worker`, and `run_app_shell`/`_ensure_runner` immediately re-attach after a successful setup so ` /setup → Hola` works without restart. **How `/setup` now works:** `tui/commands.py:362 build_default_commands` with `Command("setup", slash="setup", handler=shell.action_run_setup, enabled=not_while_busy)` verified, `shell_app.py:634 _on_composer_changed` filters `commands.slash_matches` for `/` prefix, `706 _on_submit` + `735 invoke` + `commands.py:362` + `ErrorBoundary` correctly route `/setup` to `action_run_setup`; typing `/` shows filtered suggestions (limit 8, fuzzy prefix-first), Tab completes, Enter runs, Esc dismisses; `ErrorBoundary` and `safe_action` guarantee no crash; verified headless: `slash.is_open True matches ['setup']` and after Enter `is_running True` but `runner is None` fixed, `await run_setup_panel` mocked returns `QuickSetupResult` and triggers `_reload_settings`/`_ensure_runner`/`_resolve_health`.
- **Other slash fixes:** `slash_matches` fuzzy ranking and `SlashSuggestions` inline popup now correctly filter (`/set`→`setup`, `/mod`→`model`, `/unknown`→`unknown command /unknown — press / to see what exists`), `CommandRegistry` shows no broken handlers (`handler is None` never appears), `Textual` command palette (`ctrl+p` → `screens.CommandPalette` → `build_default_commands`) wiring verified, footer generated from bindings so it cannot advertise a missing key.

### Added
- Precise diagnostics for five distinct states (no default provider/model, auth failed / `invalid_credentials` / `credits_exhausted` / `offline`, backend attach failed, session init failed) via `_diagnose_attachment` with `hint`/`remedies` (`/setup`, `f3 provider`, `f2 model`, `ctrl+r retry`) and in-TUI retry (`action_attach_agent`/`action_retry_provider`) — the generic `no agent attached — UI-only session` is gone.
- Headless tests for `attached`/`unattached`/`no-provider`/`no-model`/`attach-failed` states covering startup, session creation, provider/model binding, agent attachment, prompt handling, greeting handling (`Hola` streams via provider-native SSE vs complete fallback), and `/setup` slash filtering/routing including `Textual` palette wiring.

### Changed
- `TUIAppShell.run_task` no longer drops to UI-only when a valid backend exists; it distinguishes configured→resolved→attached→session-ready and streams via the correct provider path.
- `TUIAppShell.invoke` now correctly handles `@work` `Worker` results instead of only coroutines.

## [1.1.2] - 2026-09-21

### Added
- **AGENTS.md — guía para agentes.** Regla de oro: nunca dejar cambios sin bump de `src/unknown_agent/version.py` + `CHANGELOG.md` + tag `vX.Y.Z` (es lo que hace que `ua upgrade --check` / `ua version` detecten el update). Incluye checklist de release, comandos copy/paste para `pip`/`pipx`/`uv`/`ua upgrade`, flujo seguro de PAT sin token en disco (`credential.helper` efímero), verificación `SHA256SUMS`, checklist UNLI/custom-provider, y política de placeholders `API_TOKEN:` / `KEY:`.

## [1.1.1] - 2026-09-21

### Fixed
- **UNLI (`https://api.unli.dev/v1`) was unusable: “no agents” and “provider is not compatible with OpenAI”.** Root causes were a parser that missed `capabilities.limits.supports` / `max_context_window_tokens`, a header mismatch (`Bearer` vs `x-api-key`), an empty-list blocking path, and re-inference of compatibility from `type` only in `provider_status`/`health`/`registry`/`shell`. Added `providers/compatibility.py` (`openai`/`anthropic`/`auto`), made `discovery.build_client` / `fetch_models` / `validate_credential` / `_probe_completion` compatibility-aware (auto retries `openai`→`anthropic` on 404/405), fixed UNLI payload parsing, added `resolve_entry_for` with `extra.compatibility`, and threaded compatibility through `health`, `provider_status`, and `shell_app._available_models` with a `live → catalog (auto/unli-auto) → manual (typed input)` fallback so the selector is never empty.
- Generic custom OpenAI-compatible gateways are now first-class: the setup panel always offers “➕ Add custom endpoint”, validates `https://` URLs, collects display name + explicit API compatibility, and fabricates a `ProviderEntry` with the correct `protocol`/`model_listing`; the typed model input accepts any id when live listing is unavailable.

### Added
- `providers/compatibility.py` — single source for OpenAI/Anthropic routing.
- **Improved TUI for custom providers:** `quicksetup.CustomEndpointScreen` + typed model id, `CompatibilityScreen`, `ModelScreen` manual source, `ModelSelector` manual caption, and tolerant `_safe_*` wrappers so mocked tests keep passing.

## [1.1.0] - 2026-09-20

### Fixed
- **The TUI can no longer be blocked by provider state.** Launching the shell used to
  validate the credential and list models *before* drawing anything, so an account with
  no credit (HTTP 402), an expired key, an unreachable endpoint or a slow network
  prevented the UI from opening at all. Startup now touches local configuration only;
  provider health is resolved on a background worker after the first frame and arrives
  as UI state. 402, 401, 429, DNS failures, timeouts and unclassified errors all render
  as an actionable banner with recovery actions, and the shell stays interactive.
- `ua tui` was defined but never registered as a command, so the shell was unreachable
  from the CLI. It is now a first-class command.

### Added
- **Per-provider authentication modes.** Providers declare how they can be reached
  instead of being assumed key-only: `api_key`, `subscription`, `routed`, `endpoint`
  and `none`. The setup panel shows the modes for the chosen provider with their
  billing implications and caveats, and routes to the right flow for each.
- **Codex supports both sign-in paths.** "Sign in with ChatGPT (subscription)" uses the
  session owned by the `codex` CLI and is billed through your plan; "OpenAI API key
  (direct billing)" is billed per token. The difference is stated in the panel, and the
  subscription flow launches and verifies the login rather than asking for a secret.
- **Auth-conflict detection.** An exported `OPENAI_API_KEY` can silently override a
  ChatGPT subscription and bill you per token; this is now detected and explained at
  configuration time, with the fix, and the *active* path is the one tested.
- **Real provider testing before anything is marked working.** `run_provider_check`
  reports independent stages — auth conflicts, credential or session validity, model
  listing, model availability, capability detection, and an optional inference probe —
  and returns a normalised status (`ready`, `connected`, `login_required`,
  `invalid_credentials`, `credits_exhausted`, `degraded`, `offline`, `auth_conflict`,
  `setup_required`, `rate_limited`, `recovering`) rather than raising.
- **A redesigned shell.** Conversation area, composer with slash commands, status bar,
  and a live sidebar: provider health with remedies, a context-window gauge, session
  metrics (input/output/total tokens, cost, calls, per-model breakdown) and agent state.
- **Command palette** (`ctrl+p`) with fuzzy search over every command, plus interactive
  switchers for model (`f2`), provider (`f3`) and reasoning mode (`f4`), and `ctrl+r` to
  re-check the provider. Every control stays available while the provider is failing.
- **Honest usage accounting.** `UsageTracker` distinguishes measured usage (from the
  provider's usage block) from estimates (character counts, shown with `~`), and prices
  from published rates; unpriced models read `unpriced`, never `$0.00`.

### Changed
- **Setup is now a panel, not a command sequence.** `ua setup` opens an interactive
  Textual UI: provider selector → masked API-key dialog → automatic key validation →
  automatic model fetching from the provider's models endpoint → model selector →
  review and save. Invalid keys are explained in the dialog with a retry instead of a
  raw error, and nothing is written until the save step.
- `ua models use` with no argument now opens the same model selector panel instead of
  requiring a model id; `/model` in the shell does too. Passing an id still works.
- The panel is the **default path**: `ua`, `ua run`, `ua build`, `ua fix`, `ua plan` and
  `ua ask` route into it automatically when no provider is configured, instead of
  printing a hint about commands to memorise.
- `textual` moved from an optional extra to a core dependency, because the setup panel
  ships with the product.

### Added
- `unknown_agent.tui.quicksetup` — the four-screen setup panel (`ProviderScreen`,
  `ApiKeyScreen`, `ModelScreen`, `ReviewScreen`) plus `save_provider_choice`.
- `unknown_agent.onboarding.panel.launch_setup` — one entry point that chooses the panel
  or the Rich prompt fallback (CI, `--json`, no TTY, `ua setup --classic`).
- `ua setup --classic` to opt out of the full-screen UI.
- `unknown_agent.tui.quicksetup.run_model_picker` — the model selector on its own, for
  changing models after setup.
- MCP subsystem: official SDK client (stdio/HTTP/SSE), trust ladder, tool
  bridge (`mcp.<server>.<tool>`), curated catalog, `ua mcp` management group.
- Vision system: capability resolution with sources, image ingestion with an
  exfiltration guard, fallback-profile routing, per-dialect provider encoding
  (OpenAI/Anthropic/Ollama), `analyze_image`/`compare_images`/
  `screenshot_analyze` tools, `ua vision` group.
- Computer-use abstraction: closed action vocabulary, driver seam with a
  Playwright-backed implementation, safety ladder (budgets, host allowlist,
  denied keys, fail-closed confirmation, JSONL journal), observe/act tools.
- Advanced tools: `architecture_map`, `dependency_audit`, `repo_health`,
  `app_scaffold`, `doc_query`, `plan_review`, `mcp_discover`, `skill_distill`,
  `ui_validate`.
- Install & delivery: product-grade `scripts/install.sh` (pipx > uv > managed
  venv, verify, PATH repair, idempotent, `--uninstall`), install-mode detection
  with receipts (`host/install.py`), origin-aware `ua upgrade`.
- `ua service` group with a user-level systemd maintenance timer
  (`host/systemd.py`): weekly prune/rotate/update-check; cron and Task
  Scheduler equivalents printed on non-systemd platforms.
- Doctor groups: `install`, `mcp`, `vision`.
- Onboarding step 8: vision capability fact + opt-in MCP starter servers.
- GitHub delivery: CI (test matrix + installer smoke + build check), release
  workflow with `SHA256SUMS` over all assets, `CONTRIBUTING.md`, `SECURITY.md`,
  secure PAT push guide (`docs/publishing.md`).

## [1.0.0] - 2026-09-20

### Added
- Initial production foundation: CLI-first hyperagent for building, fixing,
  verifying and shipping applications.
- Agent core: plan → act → observe → verify execution loop with self-healing,
  budgets, step journaling and resume.
- Tool ecosystem (28 tools): filesystem (read/write/patch/create/delete/list/
  search/glob/tree), repo intelligence (inspect/summarize/git state & diff),
  execution (shell/tests/lint/format/typecheck/build/logs/http), memory
  (search/remember/project-note), validation and browser.
- Layered memory: working, session, persistent project and procedural.
- Validation engine with auto-detected quality gates, evidence capture and a
  configurable definition of done.
- Playwright/Chromium browser subsystem: navigation, flows, forms, assertions,
  screenshots, console and network error capture, traces.
- Typer + Rich CLI: 20 command groups, JSON output mode, structured errors,
  stable exit codes, shell completion.
- Typed configuration with layered precedence, `--explain` provenance, secret
  references and a redaction pipeline.
- Plugin system: entry-point, user and project plugin discovery, tool/provider/
  gate registration.
- Packaging for Linux-first use: pipx/uv/pip install, cross-platform CI matrix,
  Homebrew and winget templates, PyInstaller standalone build recipe.
