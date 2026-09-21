# Handoff — Unknown Agent

State of the project for the next agent picking this up. Everything below was true at
the `v1.4.0` release (2026-09-21). The web console ships built assets; rebuild steps
and the live-test recipe are included.

## What is done (do not redo)

- `v1.3.0` — premium web console (Vue 3 + Vite, FastAPI + WebSocket): token login from
  the terminal, `approve device` pairing, composer with provider/model selects, stop,
  uploads (image → vision, text/pdf/doc → context), streaming chat, dashboard.
- `v1.3.1` — fresh-install crash fix (`python-multipart`, `pypdf` as core deps;
  registration-time 501 guard) + PEP 668 one-liner in README.
- `v1.4.0` — the web agent became a real agent:
  - `web/server.py::_run_agent` runs the full `Runner` (tools, planning, verification)
    for build/file messages; `_needs_agent` is the imperative-verb router; the bolt
    button in the composer forces agent mode (`agent: true` on the chat payload).
  - `LoopHooks` (thought / tool_start / tool_end / verification) are forwarded to the
    socket as `thought` / `tool` / `verify` events; `phase` marks run start.
  - `/api/files/{path}` serves agent-written files (session authed, rooted at
    `paths.cwd`, traversal-guarded, `Cache-Control: no-store`); `.html`/`.htm` writes
    emit `artifact` events that render live iframe preview cards.
  - Frontend: `StreamMonitor` shows internal activity + real thinking/generating
    timers; code blocks > 8 lines collapse by default; `Message` renders artifact
    cards (reload / open-in-tab).
  - Final run message prefers `AgentDecision.answer` over the runner boilerplate.
- Tests: `tests/test_web_panel.py` covers agent mode (stubbed Runner), artifacts,
  files endpoint auth/traversal, and the question-vs-agent routing. Full suite green
  at release time; `ruff check` + `ruff format --check` clean.

## Architecture map (where things live)

- `src/unknown_agent/web/server.py` — all web routes + `_run_chat` routing,
  `_run_agent`, `_spawn`, `_needs_agent`, `/api/files`.
- `src/unknown_agent/core/runner.py` — `RunRequest`, `Runner`, `RunResult`
  (`summary`, `files_changed`, `ok`, `blockers`, `outcome.steps[].decision.answer`).
- `src/unknown_agent/core/loop.py` — `LoopHooks` (hook call sites ~237/402/407/464).
- `src/unknown_agent/tui/app.py` — `ShellBridge` is the reference pattern for
  hooks → UI forwarding.
- `web/frontend/src/` — Vue 3 app; build with `npm run build` in `web/frontend`,
  then copy `dist/*` to `src/unknown_agent/web/static/` (assets are committed).
- `web/qa/qa.py` (panel QA) and `web/qa/qa8.py` (agent-run QA) — Playwright, expect a
  server on :8787 with XDG root `/tmp/uaweb` and the unli provider configured.

## Unfinished / known gaps (ordered by value)

1. **Per-message provider/model in agent mode.** The composer selects the *global*
   active profile (`POST /api/providers/...`); `RunRequest` accepts `provider`/`model`
   but `_run_agent` does not pass them. Wire the current selects through the chat
   payload so a run uses the chosen pair without changing global state.
2. **No WebSocket auto-reconnect.** `ChatView` sets `live=false` on close and never
   retries; a dropped socket requires a manual refresh. Add exponential-backoff
   reconnect + a reconnecting state in the header.
3. **Plan approval is auto-yes in web.** `request_plan_approval=lambda _plan: True`.
   A real approval card (show plan, approve/reject) would match the terminal's
   plan-mode UX; today the user cannot inspect or reject a plan from the browser.
4. **Non-HTML artifacts have no preview.** `/api/files` already serves any file;
   extend `artifact` emission (and the card) to images (inline `<img>`), PDFs and
   markdown, not just `.html`/`.htm`.
5. **Attachments in agent mode are text-only.** Files become `extra_instructions`;
   images do not reach a vision model inside the Runner. If the loop grows vision
   support, plumb `images` through `RunRequest`.
6. **Chat history is in-memory.** The `history` dict in `server.py` dies with the
   process; persist per-session transcripts (there is already a session/memory
   subsystem in `core/`) so refresh keeps context.
7. **No run visibility on the dashboard.** Active runs, elapsed time and cancel live
   only inside the chat socket. A dashboard panel for running/last runs would help
   on headless servers.
8. **Mode indicator.** Nothing shows *why* a message went to the full agent vs chat;
   a small chip on the message ("full agent" / "chat") plus a way to re-run a message
   in the other mode would make the router legible.
9. **Cosmetic:** `cli/commands/agent.py` help strings raise `SyntaxWarning`
   (invalid escape `\[`); use raw strings. The sandbox config warning about a live
   `api_key` in `config.toml` is deployment hygiene: move to `env:` references.

## Operational notes

- The unli gateway 429s under load; the provider layer backs off and cools down
  (correct behaviour — never retry immediately). Agent runs take ~1.5–3 min when the
  gateway is congested; QA scripts must allow ≥ 420 s per agent step.
- Credentials in earlier chat messages (unli key, GitHub PATs) are compromised;
  rotate before any further release work. Installs use the PAT-less public flow only
  if the repo is made public; otherwise pass `x-access-token` inline and unset it.
- Live-test recipe (sandbox): `XDG_*_HOME=/tmp/uaweb/…`, run
  `ua web --port 8787 --host 0.0.0.0` from a scratch cwd (the server cwd is both the
  `/api/files` root and the agent's working directory).
