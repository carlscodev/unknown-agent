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

## [1.1.5] - 2026-09-21

### Added — Session history + resume (TUI + CLI) — full lifecycle
- **Real persistence, not a placeholder.** Each TUI launch is now a *session* with a stable 16-hex ID (`short_id` 8), human name/title, `created_at`/`updated_at`/`finished_at`, `status` (`active`/`completed`/`interrupted`), `cwd`/`provider`/`model`, `turns`/`message_count`, `last_task`/`summary`, `resume_count`, `duration_s` and a JSONL transcript (`1 line = 1 message {role, content, timestamp, provider, model}`) — all in a dedicated SQLite index `state_dir/tui_sessions.db` + `tui_transcripts/<id>.jsonl` (`TuiSessionStore` in `tui/history.py`, `SCHEMA_VERSION 1`, `prune`/`stats`/`latest`/`get` with short-id substring, `current_tui_session` marker). Deleting the DB is safe; listing is instant; transcripts survive restarts.
- **Polished recent-sessions list everywhere.** `TuiSessionRecord.to_dict()` exposes `name`, `ID/short_id`, `date`/`time`/`datetime` (from `time.localtime`) plus provider/model, turns/messages, cwd, resume_count, duration — exactly the advanced metadata the spec requires. Human tables via `ui/renderers.data_table` show `name | id | date | time | provider/model | activity (τ/✉) | status` with current highlighted `● current`/`CURRENT`; machine mode (`--json`) returns the full dict. No raw logs.
- **Recent-sessions + resume-by-ID matching other CLIs.** `ua tui --resume <id>` (`--resume` bare ⇒ `latest`, `--resume=<id>` and substring short-id all work), `ua --resume <id>` top-level shorthand, `ua sessions resume <id>` (with `--launch/--no-launch`) and `ua sessions resume latest`, `ua tui --list` / `ua sessions list --tui` (with `--all`/`--search`/`--status`/`--json`) and `ua sessions tui`. Every path validates the id, prints recent hints on typo, and resolves short-ids deterministically (`ORDER BY updated_at DESC`).
- **In-TUI command to view + resume.** New `tui/screens.SessionHistoryScreen` — a polished modal (96×88%, `fuzzy_score` filter, `ListView` with `is_current` marker `●`/`○`/`◐`, date/time, provider/model, turns/messages, hint `ua tui --resume <short>` + short cwd + last_task) and `SessionDetailScreen`. Replaces raw logs with cards; shows current clearly; supports `↑↓`/`enter` resume, `d` delete (with `ConfirmScreen`), `n` new session, `esc` close, live filter. Opened via three discoverable surfaces: slash `/sessions` `/history` `/resume` (also in `SlashSuggestions`), palette `Session history` / `History` / `Resume session`, hotkeys `f7` / `ctrl+o`. Help screen (`f1`) lists `f7`/`ctrl+o`. Footer no longer clips.
- **Show current session clearly + polished metadata.** Sidebar `SessionCard` now renders `session`/`id`/`created`/`using`/`history` plus a subtle `resume ua tui --resume <short>` hint, refreshed from `store.get(id)` on every turn. Header never clips; TUI `on_mount` replays resumed transcript (`_load_tui_transcript_into_ui`) and shows a banner `resumed session <name> · <short> · <turns> turns · <datetime> — resume again with: ua tui --resume <short>`. New sessions show `session <name> · <short> · ua tui --resume <short>` as chrome hint. Every session row is `name | short_id | date | time | provider/model | activity | status` — not logs.
- **Resume command on every exit, reliably.** `cli/commands/misc.tui_command` wraps `asyncio.run(run_app_shell(..., resume_id, tui_store))` with a `try/finally` that calls `_print_tui_resume` (human `output` + fallback `sys.stderr` plain) for **normal**, **Ctrl-C** (`KeyboardInterrupt → ExitCode.INTERRUPTED`), **alternate quit** (`ctrl+q` with confirm) and **crash** (`BaseException`) — all paths print `session "<name>" (<short>) saved. Resume with: ua tui --resume <short> | ua sessions resume <short>` with both forms. `TUIAppShell.on_unmount` finishes the session (`status completed`) and closes the store so even an unhandled exception marks it finished. `action_request_quit` also finishes before `exit()`. `run_task` and `_on_submit` persist `user`/`assistant` turns via `store.update_on_message(..., provider, model)` so resume restores up to 500 messages. Tested at 40×20/50×30/60×24/80×24/120×32/150×44.
- **Architecture.** `TuiSessionStore` (`state_dir/tui_sessions.db` + `tui_transcripts/`), `TuiSessionRecord`, `tui/history.py` as single source; `shell_app._tui_store`/`_tui_session`/`_tui_owns_store` + helpers `_tui_store_paths`/`_ensure_tui_session`/`_load_tui_transcript_into_ui`/`_persist_tui_message`/`_finish_tui_session`/`_tui_resume_command` + `on_mount` replay + `on_unmount` close + `run_task` persist + `_refresh_sidebar` enrichment. `sessions.py` adds `list --tui`/`tui`/`resume` with current-per-scope logic (`latest(cwd)` vs global marker) and `kv_table` detail view. `app.py` adds global `--resume` shorthand (`--resume` in `_VALUE_FLAGS`, `main_callback` synthetic `tui --resume`). Storage, listing, current display, resume flow and exit command are all exercised headless.

### Changed — CLI + TUI wiring
- `cli/app.py` — global `--resume` (shorthand for `tui --resume`), added to `_VALUE_FLAGS` and handled before banner; `cli/commands/misc.tui_command` now parses `--resume`/`--resume-id`/`--resume=<id>`/`--list`/`--ls` from `ctx.args`, validates id upfront with recent hints, opens `TuiSessionStore`, handles `--list` polished table without launching TUI, and guarantees resume print via `_did_print` guard. `run_app_shell` now accepts `resume_id`/`tui_store` and forwards to `TUIAppShell`.
- `tui/commands.build_default_commands` — adds `sessions` (`f7`/`/sessions`), `history` (`/history`) and `resume` (`/resume`) all wired to `action_show_sessions` (with `not_while_busy` where appropriate). `tui/commands.fuzzy_score` reused for session filter.
- `tui/screens` — `__all__` gains `SessionHistoryScreen`/`SessionDetailScreen`; `HelpScreen` rows gain `f7`/`ctrl+o`.
- `tui/shell_app` — `BINDINGS` gains `f7`/`ctrl+o` `show_sessions`; `__init__` gains `resume_id`/`tui_store` + `_tui_session` bootstrap; `on_mount` ensures session + replays; `on_unmount` finishes+closes; `write` signature gains `persist` (default true) but transcript replay uses `persist=False`; `_on_submit` persists user; `run_task` persists assistant + enriches `_session_info`; `_refresh_sidebar` injects `session_id`/`session_name`/`session_turns`/`session_date`/`session_time`; `action_clear` truncates transcript file; `action_show_sessions` implements picker→resume/new/delete with `ConfirmScreen`; `run_app_shell` signature updated; CSS unchanged but session bar already responsive. `dashboard.SessionCard` now renders session identity (`session`/`id`/`created`/`using`/`history` + resume hint).
- `cli/commands/sessions` — `list` gains `--tui` flag (polished history with per-scope current), new `tui` alias command and `resume` command (with `kv_table` detail, `--launch/--no-launch` and direct `run_app_shell` launch). `__all__` unchanged.

### Fixed
- `sessions list --tui` without `--all` now shows the most recent session *for that cwd* as `● current` instead of only the global marker from a different project.
- Headless `run_test` sessions no longer leak: `on_unmount` closes the owned store and marks `completed`, so subsequent `sessions list --tui` is not polluted.

### Files / Modules changed
- `src/unknown_agent/tui/history.py` **new** — `TuiSessionStore` + `TuiSessionRecord`, JSONL transcript, `state_dir/tui_sessions.db`.
- `src/unknown_agent/tui/shell_app.py` — session bootstrap, replay, persist, finish, picker, current chrome, responsive still intact.
- `src/unknown_agent/tui/screens.py` — `SessionHistoryScreen` + `SessionDetailScreen` + help rows.
- `src/unknown_agent/tui/commands.py` — three session commands.
- `src/unknown_agent/tui/dashboard.py` — `SessionCard` session identity.
- `src/unknown_agent/cli/commands/misc.py` — `tui --resume`/`--list` parsing + exit resume print + `run_app_shell` forwarding.
- `src/unknown_agent/cli/commands/sessions.py` — `list --tui` + `tui` + `resume`.
- `src/unknown_agent/cli/app.py` — global `--resume` shorthand.
- `src/unknown_agent/version.py` `1.1.4 → 1.1.5`.

### Testing
- `TuiSessionStore` unit: create/list/search/short-id/transcript/resume/marker/prune/finish.
- Headless `run_test` at 40/50/60/80/120/150 — breakpoint + `sidebar hidden` + `/sessions` slash + `ctrl+p` palette + `f7` picker + replay still `PASS`; `pytest tests/test_tui_shell.py` (30) and `tests/test_tui_interaction.py` (19) `PASS`.
- CLI: `sessions list --tui`/`--json`/`--all`, `sessions tui`, `sessions resume --no-launch`, `tui --list` all polished.

## [1.1.4] - 2026-09-21

### Fixed — Responsive / Mobile (Termius phone)
- **TUI was not responsive on narrow screens.** On Termius phone (40-60 cols) the `Horizontal #body` with fixed `width:32` sidebar left only ~8-18 cols for `#main`, causing cramped side-by-side, clipped header (`provider · model · cost` overflow), broken multi-column, panels fighting for width, and ugly density. Root cause: layout assumed desktop width, no breakpoints, no stacked fallback, sidebar always `display:block` even when it could not fit.
- **No text clipping, no cramped sidebars, no fighting panels now.** Verified at 40×20, 50×30, 60×24, 80×24, 120×32, 150×44 headless.

### Added — Responsive architecture (proper, not tweaks)
- **Width breakpoints + layout switching** (`phone <60`, `narrow 60-80`, `compact 80-110`, `wide ≥110`) via `TUIAppShell._breakpoint_for_width` / `_update_responsive` / `_apply_breakpoint`. App adds `is-phone|is-narrow|is-compact|is-wide` classes which drive CSS: `TUIAppShell.is-narrow #body { layout: vertical; }` vs `horizontal` on desktop, flexible containers, docking (`#composer-shell dock:bottom`, `layers: base overlay`). Inspired by Textual's breakpoint-driven and dock/flex strategies.
- **Stacked layout on small widths** — `is-phone`/`is-narrow` switch `#body` to `vertical`: `#main` full width on top, `#sidebar` stacked below as `width:1fr; height:auto; max-height:12-18; border-top`, not side-by-side.
- **Collapsible / switchable panels** — sidebar hidden by default on `phone`/`narrow` (conversation gets 100% width). Toggle with `f5` / `ctrl+b` (`action_toggle_sidebar`) flips `is-phone #sidebar.hidden { display:none }` / `sidebar-collapsed` on desktop. Only most important info visible by default.
- **Compact status/header patterns** — `is-phone` shows `ua` instead of `unknown agent`, truncates provider/model tighter (10/12 vs 18/26), hides `cost` on mobile and shows `f5 info` hint; status label shortened. `_render_header` re-truncates if `right.cell_len > max_right` to never overflow.
- **Cleaner command/input area** — composer placeholder adapts: `phone → "Ask or / for commands"`, `narrow → "Ask, or press /"`, else full. `#composer` margins/paddings shrink via `is-phone` / `is-narrow` transitions. `SlashSuggestions` margin `0 1` and `max-height 6-7` on mobile to not cover conversation.
- **Adaptive footer** — `is-phone` shows `ctrl+p cmds · / slash · f1 help` (+ `f5 info` when hidden), `narrow` adds `f5 info`, `compact` adds `f2/f3`, `wide` full 6 keys. Prunes trailing entries while `est_len > width` so it never clips, adds `…` hint.
- **Thin context bar for mobile** — new `#context-bar` (hidden on wide, `is-narrow|is-phone .has-content { display:block }`) with `height:1`, `border-bottom`, shows `ctx ▰▰▱ 9% 12k/128k` when `context_window>0`; built in `_render_context_bar` from `UsageTracker` and refreshed on resize.

### Changed — Visual design, animation, polish
- **Refined spacing & hierarchy** — header `18%` hairline, sidebar `22%` scrollbar, conversation `1 1` on phone vs `1 3` on wide, cards `opacity 1` with `transition: opacity 180ms`, notice `opacity 0→1 + offset-y -1→0 200ms in_out_cubic`, composer `border/background/margin 150-180ms`, body/sidebar `width 220ms in_out_cubic`.
- **Tasteful animation** — sidebar show/hide via `width/opacity` transition, notice slide+fade, slash popup `opacity/offset 150ms`, busy state `is-busy` tints header/composer/footer (`accent 45%`) without noise. No overdone motion; all `in_out_cubic`, 150-220ms, useful not decorative.
- **Premium production-ready** — single slate-blue accent, neutrals for structure, bold/dim hierarchy, rounded ` $primary 22%→70%` focus, `layers: base overlay` for modals, `hatch: right $surface` on sidebar, `min-width` guards, `truncate` everywhere.

### Optimized
- TUI architecture refactored from rigid `Horizontal` to responsive `layout: horizontal→vertical` with breakpoint classes, docking, flexible `1fr` containers, and adaptive panel behavior. No longer assumes wide terminal; explicitly switches at breakpoints.
- Header/footer rendering now width-aware (gap calc + `max_right` re-truncate, entry pruning) so no awkward clipping.
- Sidebar `max-height` prevents it from stealing conversation on short phone heights.

### Files / Modules changed
- `src/unknown_agent/tui/shell_app.py` — ~+400 lines: new `CSS` (responsive, layers, transitions, `is-*` + `hidden`/`sidebar-collapsed`), `BINDINGS` (`f5`/`ctrl+b` toggle), `__init__` (`_breakpoint`, `_sidebar_visible`), `compose` (`#context-bar`), `_breakpoint_for_width`, `_update_responsive`, `_apply_breakpoint`, `action_toggle_sidebar`, `on_resize`, `_render_header` (responsive), `_render_footer` (responsive), `_refresh_sidebar` (hide secondary cards), `_render_context_bar`, `run_task` busy `is-busy` class + transitions, slash responsive.
- `src/unknown_agent/version.py` `1.1.3 → 1.1.4`
- `CHANGELOG.md` + `AGENTS.md` updated for `v1.1.4` publish flow.

### Responsive testing performed
- Headless `run_test` at 40×20, 50×30, 60×24, 80×24, 120×32, 150×44 — breakpoint mapping, `sidebar hidden` default, `header`/`footer`/`composer` placeholder, `context-bar has-content`, `Hola` streaming (`FakeProvider` SSE) still `PASS` at every width with no `no agent attached`, resize `wide→phone→wide` and toggle `f5` verified, `pytest 30` TUI tests `PASS`.

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
