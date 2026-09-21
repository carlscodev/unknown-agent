# Changelog

All notable changes to Unknown Agent are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
