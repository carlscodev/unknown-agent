<div align="center">

# Unknown Agent

**A Python-first hyperagent that builds, fixes, verifies and ships real software.**

CLI-first · Linux-first · Windows-capable · tool-driven · verification-gated · extensible

```console
$ ua build "add Stripe checkout with webhooks, tests and a browser-verified happy path"
```

</div>

---

Unknown Agent is not a chat wrapper around a model. It is an execution system: it
maps a repository, plans the work, edits real files through a policy-checked tool
layer, runs the project's own build/test/lint commands, drives a real Chromium
browser against the running app, and **refuses to declare success until the
quality gates pass**. Everything is a Python package with proper entry points,
typed configuration, structured logging, a plugin API and a test suite.

```
plan ──▶ act ──▶ observe ──▶ verify ──▶ (loop until the task actually passes)
                    │                        │
                    └──── self-heal ◀────────┘
```

## Why it is different

| Ordinary coding agent | Unknown Agent |
| --- | --- |
| Answers, then stops | Runs a plan → act → verify → self-heal loop until the definition of done passes |
| Reads whatever files it happens to open | Builds a repository map, detects stack/conventions, recalls project memory |
| Trusts its own diff | Proves work with real builds, tests, linters and a headless Chromium pass |
| Forgets between runs | Three-layer memory: working, session, and persistent project knowledge |
| "It works on my machine" shells | No-shell exec by default, process-group timeouts, deny-by-default path policy |
| Prompt-hardcoding a vendor | Provider abstraction (OpenAI-compatible, Anthropic, Ollama, offline) |

## Install

Linux is the primary platform; macOS and Windows are supported.

### One command (recommended, isolated)

```bash
pipx install "git+https://github.com/klowerg/Unknown-Agent"        # or: uv tool install "git+https://github.com/klowerg/Unknown-Agent"
ua setup                                                 # interactive setup panel (provider → key → model)
```

`pipx` gives the CLI its own virtualenv, a `ua` command on PATH, and one-command
upgrades — the cleanest way to run a Python CLI tool without touching your
system Python. `uv tool` is equivalent and faster.

### Shell installer (auto-detects the best strategy)

Download it, **read it**, then run it — never pipe remote code straight into a shell:

```bash
curl -fsSLO https://raw.githubusercontent.com/klowerg/Unknown-Agent/main/scripts/install.sh
less install.sh          # inspect what you are about to execute
sh install.sh            # picks pipx > uv > managed venv, verifies, offers setup
```

The installer never uses sudo, never writes outside `$HOME`, prints every
action, is idempotent, and supports `--yes`, `--with-service`, `--with-setup`,
`--from-pypi`, and `--uninstall`. On releases, a `SHA256SUMS` file lets you
verify the script before running:

```bash
sha256sum -c SHA256SUMS --ignore-missing
```

### Other paths

```bash
pip install --user "git+https://github.com/klowerg/Unknown-Agent"                  # plain pip
python3 -m venv .venv && .venv/bin/pip install "unknown-agent[all]"     # your own venv
py -m pip install --user pipx && pipx install "git+https://github.com/klowerg/Unknown-Agent"   # Windows (PowerShell)
```

### After install

```bash
ua                              # first run drops you into guided setup automatically
ua doctor                       # verify environment, install, credentials, toolchain
ua doctor --install-browsers    # optional: Chromium for browser validation
ua service install maintenance  # optional (Linux): weekly housekeeping via a systemd *user* timer
```

### Upgrade, repair, removal — first-class

```bash
ua upgrade --check          # is a newer release available?
ua upgrade                  # upgrades through the same installer that installed you
ua doctor                   # detects broken PATH, wrong install mode, stale services
sh install.sh --uninstall   # clean removal (keeps your config/state, says where they live)
pipx uninstall unknown-agent   # equivalent for pipx installs
```

## Quickstart

```bash
ua                                            # first run: pick a provider, paste a key, go
```

Setup is a **panel, not a command sequence**. `ua setup` (or just running `ua` before
anything is configured) opens a full-screen terminal UI:

1. a **provider selector** — 21 providers, every one with a working adapter, filterable;
2. an **API-key dialog** — masked input; press **Enter** and the key is validated
   immediately;
3. **automatic model fetching** — a valid key goes straight to the provider's models
   endpoint (OpenRouter `/models`, OpenAI `/models`, Anthropic `/models`, Ollama
   `/api/tags`), with the curated catalog as the fallback;
4. a **model selector** with context window, tools, vision and price;
5. a **review + save** step, then a clean success panel.

A rejected key stays in the dialog with a plain-language explanation and a retry — never
a raw traceback — and nothing is written until you save.

```bash
ua setup                                      # the interactive panel (recommended)
ua setup --classic                            # plain prompts, no full-screen UI
ua setup --provider groq --api-key-env GROQ_API_KEY       # scripted, no prompts
ua providers list                             # configured profiles, credential state, active one
ua models list -s llama                       # what this provider actually offers
ua doctor --network                           # proves key + endpoint + model + tool calling work
```

Prefer the environment? `export OPENAI_API_KEY=sk-...` and
`ua setup --provider openai --api-key-env OPENAI_API_KEY`. Secrets are stored in
`~/.config/unknown-agent/credentials.toml` (`0600`) and referenced from the config as
`store:openai` — never as a literal. See [`docs/onboarding.md`](docs/onboarding.md).

```bash
ua config init                                # commented, documented config file

ua inspect                                   # repo map: stack, entry points, commands, risks
ua plan "add dark mode with persisted preference"     # review the plan before spending tokens
ua run "add dark mode with persisted preference"      # execute, verify, self-heal, report

ua fix "checkout page 500s on empty cart"     # diagnose → patch → verify
ua verify                                     # run every quality gate on the current tree
ua browser open http://localhost:3000         # real Chromium session, screenshots
ua runs show --last --events                  # full audit trail of the last run
```

## Command surface

```
ua init                     Guided project setup (.unknown/)
ua plan "<task>"            Produce a reviewable execution plan (no writes)
ua run  "<task>"            Execute a task through the full agent loop
ua build "<task>"           Alias for run tuned for greenfield feature work
ua fix  "<issue>"           Diagnose → patch → verify with a regression guard
ua ask  "<question>"        Read-only Q&A about the codebase
ua inspect                  Repository intelligence: map, stack, commands, risks
ua verify                   Run validation gates (+ optional browser flows)
ua doctor                   Environment, credentials, toolchain, browsers
ua config …                 show | get | set | init | validate | path | edit
ua memory …                 search | remember | recall | list | forget | stats | export
ua runs …                   list | show | events | resume | prune
ua sessions …               list | show | rm
ua browser …                open | flow | screenshot | console
ua tools …                  list | describe | call        (direct tool invocation)
ua plugins …               list | new | enable | disable | info
ua context …               build | show | tokens
ua log …                   tail | path | level
ua upgrade                  Check/perform upgrade of the installed CLI
ua completion               Emit shell completion (bash/zsh/fish/powershell)
ua version                  Version, build metadata, environment fingerprint
```

Every command supports `--json` (machine-readable), `--verbose`/`--debug`,
`--quiet`, and honours `NO_COLOR`, `CLICOLOR_FORCE`, `TERM=dumb`, non-TTY pipes
and `--pager` where output is long.

## Repository intelligence

`ua inspect` produces a deterministic picture of the project without any model
call: stack detection across Python/Node/Go/Rust/PHP/Ruby/JVM, package manager
resolution (uv, poetry, pnpm, yarn, bun, cargo, go modules, composer, bundler),
framework detection (Django, FastAPI, Flask, Next.js, React, Vue, Svelte,
Express, Nest, Laravel, Rails, Spring), test/lint/build command discovery,
monorepo layout, entry points, API routes, and risky areas.

## Verification is the product

Validation is not a suggestion appended to a prompt — it is an engine:

1. **Gates** are discovered per project (`syntax → lint → format → typecheck →
   tests → build → browser → diff → custom`) and can be required or advisory.
2. **Evidence** is captured for every gate: exit code, duration, trimmed output,
   screenshots, console errors, failing requests, diffs.
3. **Definition of done** decides what "finished" means: in `strict` mode every
   required gate must pass; in `balanced` mode the agent may report an issue only
   with explicit evidence and a documented next step.
4. **Self-heal** turns a failing gate into the next iteration: the agent reads the
   failure, forms a hypothesis, patches, re-runs the exact gate, and never
   declares "probably fixed".

## Browser-aware by design

```bash
ua browser flow --url http://localhost:3000 --steps-file flow.yaml --screenshot out.png
```

The browser subsystem drives Playwright/Chromium: navigating, filling forms,
clicking flows, asserting visible text, capturing screenshots, collecting console
errors and failed network requests, and (optionally) traces and video. Browser
findings are first-class gate evidence and feed straight back into the agent loop.

## Memory model

| Layer | Lifetime | Storage | Purpose |
| --- | --- | --- | --- |
| Working | single step | in-process | current plan, observations, scratchpad |
| Session | one run | `.unknown/runs/<id>/` | step journal, decisions, evidence, resume state |
| Project | forever | `.unknown/memory/` + SQLite | conventions, architecture notes, known issues, decisions |
| Procedural | forever | `procedures/*.md` | reusable workflows and behavioural rules injected into prompts |

Memory is inspectable and editable: `ua memory search "auth"`,
`ua memory remember "API keys are loaded in app/config.py"`, `ua memory forget <id>`.
Nothing is stored without provenance (source, run, timestamp, confidence).

## Configuration and secrets

Precedence: **defaults < user config < project config < `UNKNOWN_AGENT_*` env < CLI flags**,
and `ua config show --explain` prints which source produced every value.

```toml
[providers.openrouter]
type = "openrouter"                   # vendor key; the registry picks the adapter dialect
model = "openai/gpt-4o-mini"
base_url = "https://openrouter.ai/api/v1"
api_key = "store:openrouter"          # credentials.toml (0600)
# api_key = "env:OPENROUTER_API_KEY"  # environment variable
# api_key = "keyring:unknown-agent/openrouter"
```

Raw credentials in files are never required, flagged when detected, and redacted
from logs, events and run journals by a process-wide redactor.

## Extensibility

* **Tools**: subclass `Tool`, declare a JSON-schema `ParameterSpec` and a
  `RiskLevel`, register it — it appears in `ua tools list` and works from the
  agent loop, the CLI and the plugin system.
* **Providers**: implement the `ModelProvider` protocol; ship it as a plugin.
* **Gates**: add `validation.custom_gate_commands` entries or a `Gate` subclass.
* **Plugins**: packaged entry points under `unknown_agent.plugins`, discovered
  from installed distributions plus `~/.config/unknown-agent/plugins` and
  `<repo>/.unknown/plugins`.

## Documentation

| Document | Contents |
| --- | --- |
| `docs/onboarding.md` | First-run onboarding, provider abstraction, model discovery, secrets, startup flow |
| `docs/onboarding-preview.txt` | Captured transcript of the real onboarding screens |
| `docs/architecture.md` | Layers, module boundaries, data flow, stability tiers |
| `docs/blueprint.md` | The full product blueprint and v1 scope |
| `docs/cli.md` | Command reference, exit codes, scripting contract |
| `docs/configuration.md` | Every setting, precedence, secrets, examples |
| `docs/tools.md` | Tool contract, risk levels, authoring a tool |
| `docs/memory.md` | Memory layers, schema, lifecycle, privacy |
| `docs/validation.md` | Gates, definition of done, self-heal policy |
| `docs/browser.md` | Playwright subsystem, flows, evidence |
| `docs/packaging.md` | pipx/uv/pip/standalone, Windows notes, CI matrices |
| `docs/plugins.md` | Plugin API and lifecycle |
| `docs/roadmap.md` | v1 scope, phase two (TUI, dashboard, teams, cloud) |
| `docs/development.md` | Local setup, tests, quality gates, release checklist |

## Development

```bash
git clone https://github.com/unknown-agent/unknown-agent
cd unknown-agent
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check        # lint + types + tests + build
make test         # pytest (unit + integration)
```

## License

Apache-2.0. See `LICENSE`.
