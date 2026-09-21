# Changelog

All notable changes to Unknown Agent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
