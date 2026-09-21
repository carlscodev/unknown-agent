# Handoff — Unknown Agent

State of the project for the next agent picking this up. Everything below was true at
the `v1.6.0` release (2026-09-21). The web console ships built assets; rebuild steps
and the live-test recipe are included at the end.

---

## 0. What the user asked for on 2026-09-21 (and where each item lives now)

| Complaint | Status | Where |
| --- | --- | --- |
| “el agente dice que no tiene herramientas” | ✅ fixed | `src/unknown_agent/web/agent_loop.py` — every panel message runs a tool-calling loop on the real registry + policy + executor |
| “creó una landing pero no puedo verla en tiempo real” | ✅ fixed | `ArtifactCard.vue` + `/api/files` media types + live preview inside code cards |
| “la ventanita de streaming no stremea” | ✅ fixed | real `delta` stream over the WebSocket; the old text-echo window was deleted (`StreamMonitor.vue` removed) |
| “el razonamiento debe salir donde se genera el texto, con animación y segundos” | ✅ fixed | `ReasoningBlock.vue` inside the message, pulse while thinking, clock + `timing` on `done` |
| “quiero ver cuánto tarda pensando y cuánto generando” | ✅ fixed | `Timing` (think / gen / tool / total) per run, live in `RunStatus.vue` |
| “que use herramientas como OpenCode: leer, escribir, ejecutar” | ✅ fixed | 39 builtin tools (filesystem, search, repo, execution, validation, docs, scaffold, memory, network) |
| “cuando entrega código que sea una ventanita, no un muro” | ✅ fixed | `CodeCard.vue`: 9-line window, copy, download, expand, live preview |
| “documenta lo que falta en el HANDOFF” | ✅ this file | §4 |

---

## 1. What is done (do not redo)

- `v1.3.0` — premium web console (Vue 3 + Vite, FastAPI + WebSocket): token login,
  `approve device` pairing, composer with provider/model selects, stop, uploads,
  streaming chat, dashboard.
- `v1.3.1` — fresh-install crash fix (`python-multipart`, `pypdf` core deps; 501 guard).
- `v1.4.0` — the web agent became a real agent: `Runner` behind the bolt button,
  `LoopHooks` → `thought`/`tool`/`verify` events, `/api/files/{path}`, HTML artifact
  cards, collapsible code blocks, `StreamMonitor`.
- `v1.5.0` (parallel session) — per-message provider/model on runs, image/PDF/file
  artifact previews, `full agent` mode chip, WebSocket auto-reconnect, `SyntaxWarning`
  fix in `cli/commands/agent.py`, plus the `web/qa/qa9.py` + `stub_server.py` +
  `stub_loop.sh` provider-independent browser QA harness.
- `v1.6.0` (this session) — **the panel agent is a first-class tool-using agent**:
  - `src/unknown_agent/web/agent_loop.py` (`WebAgent`): builds
    `build_default_registry` + `PolicyEngine` + `ToolExecutor` + `ToolContext` exactly
    like the CLI, then drives a streaming tool-calling loop (max 12 steps) emitting
    `mode` / `tools` / `phase` / `reasoning` / `delta` / `tool` / `artifact` / `done`.
    The system prompt lists the real tool names + cwd and forbids the “I have no
    tools” excuse; when the provider cannot do tool calling it degrades honestly.
    Guards: empty-completion retries (`EMPTY_TURN_RETRIES`), a closing-summary nudge,
    a fallback summary, cooperative stop, per-message provider/model override.
  - **Reasoning channel end to end**: `StreamChunk.reasoning` and
    `ModelResponse.reasoning`; parsed from `reasoning_content` / `reasoning` /
    `thinking` (OpenAI-compatible, stream and non-stream) and `thinking_delta`
    (Anthropic).
  - **Gateway compatibility**: `OpenAICompatibleProvider.tool_results_as_user` — when a
    gateway answers 5xx on a turn containing `role: "tool"` (unli: `502
    upstream_error`), tool results are re-encoded as user turns once and the mode is
    remembered. Force it with `extra.tool_results_as_user = true`.
  - **Backend timers**: `Timing` (think/gen/tool/total ms) sent on `done`.
  - `/api/files/{path}`: correct media types (html, svg, images, pdf, md, text, code)
    inline, `?download=1` for attachments; traversal still 404s.
  - `/api/status`: `tools`, `tool_count`, `cwd`, `read_only`.
  - Frontend: `ChatView` builds a per-message **timeline** (reasoning → tool → text →
    artifact); new `ReasoningBlock` (inline thinking: pulse + clock), `ToolRow`
    (arguments, spinner, duration, expandable output), `CodeCard` (window + copy +
    download + live preview), `ArtifactCard` (html/pdf/image/md/text), `Blocks`
    (shared markdown renderer), `RunStatus` (one-line phase + timers + tool count),
    and the `mode` chip (`tools` / `full agent`). `StreamMonitor.vue` deleted.
  - Markdown renderer: headings, bullet/ordered lists, links, inline code, bold.
- Tests: `tests/test_web_panel.py` (27 tests) covers tool execution through the real
  executor, reasoning deltas, tool start/end events, artifact + media type + download,
  system-prompt assertions, cooperative stop, `/api/status` toolset, provider/model
  override, and the WS event flow. Full suite green (703 passed) except a pre-existing
  sandbox failure (`tests/test_cli.py::test_doctor_reports_checks_as_json` — needs a
  configured box; it fails on a clean checkout too).

## 2. Architecture map (where things live)

- `src/unknown_agent/web/agent_loop.py` — `WebAgent`, the default chat path.
- `src/unknown_agent/web/server.py` — routes; `_run_chat` (slash commands → bolt
  `Runner` → `_run_tool_chat`), cached `_web_agent`, `stop_flags`, `/api/files`.
- `src/unknown_agent/core/loop.py` + `core/runner.py` — full agent behind the bolt.
- `src/unknown_agent/providers/base.py` — `StreamChunk.reasoning`,
  `ModelResponse.reasoning`.
- `src/unknown_agent/providers/openai_compatible.py` — streaming, reasoning parsing,
  tool-result re-encoding fallback.
- `web/frontend/src/components/*.vue` — the panel UI.
- `web/qa/qa.py` + `qa8.py` — Playwright panel/agent QA (server on :8787).
- `web/qa/qa9.py` + `stub_server.py` + `stub_loop.sh` — provider-independent QA.

Rebuild the console after touching `web/frontend/src`:

```bash
cd web/frontend && npm install && npm run build
rm -rf ../../src/unknown_agent/web/static/assets
cp -r dist/* ../../src/unknown_agent/web/static/
```

## 3. Live-test recipe (sandbox, this session)

```bash
# 1) deterministic mock provider — port 8899 (/home/user/mock_provider.py)
python -m uvicorn mock_provider:app --host 127.0.0.1 --port 8899

# 2) panel — port 8787, workspace /home/user/demo-workspace
XDG_CONFIG_HOME=/home/user/uaweb/config XDG_DATA_HOME=/home/user/uaweb/data \
XDG_STATE_HOME=/home/user/uaweb/state XDG_CACHE_HOME=/home/user/uaweb/cache \
UNLI_API_KEY=KEY: ua web --host 0.0.0.0 --port 8787 --show-token
```

Config root: `/home/user/uaweb/config/unknown-agent/config.toml`, two profiles —
`unli` (`https://api.unli.dev/v1`, model `unli-auto`, key via `env:UNLI_API_KEY`) and
`mock-demo` (`http://127.0.0.1:8899/v1`). Switch from the composer or with
`POST /api/provider`.

End-to-end probe without a browser (prints every event with timings):

```bash
python /tmp/ws_test.py "crea una landing page para una cafetería"
```

Verified end-to-end with the real gateway: `list_files` → `shell_run` → reasoning
stream → artifacts; and with the mock: `write_file` → artifact card → token-by-token
answer (think 1.09 s, gen 1.32 s, tools 4 ms).

### Known external problem: the unli gateway

- `role: "tool"` turns are rejected with `502 upstream_error` → handled by the
  re-encoding fallback (verified: multi-step runs then complete).
- **Intermittent degraded windows**: it returns `reasoning_content` and an *empty*
  completion on both stream and non-stream paths. Reproduced with raw `curl` at the
  same minute, so it is **not** a client bug. The loop nudges 3× and then reports
  “respuesta vacía”. During those windows use `mock-demo` or another provider.
- Free tier: 100 requests/month per key; an exhausted key answers
  `403 Quota Exceeded … resets on <date>`.

## 4. Unfinished / next steps (ordered by value)

1. **Empty-completion resilience is only nudging.** When unli returns reasoning-only
   three times in a row the run still ends with a notice. Next: fall back to another
   configured profile automatically (round-robin), or re-send the last turn with a
   shorter system prompt and a reduced toolset (6 core tools) before giving up.
2. **Plan approval is auto-yes in the web** (`request_plan_approval=lambda _plan: True`)
   for the bolt path. A real approve/reject card would match the terminal UX.
3. **Attachments in agent mode are text-only.** Images do not reach a vision model
   inside `WebAgent` (they are appended as context blocks). `Message.images` is
   supported by the loop; the composer flow for build tasks is not wired.
4. **Chat history is in-memory** (`history` dict in `server.py`). Persist per-session
   transcripts so a refresh keeps context.
5. **No run visibility on the dashboard.** Active runs, elapsed time and cancel live
   only inside the chat socket.
6. **Re-run in the other mode.** The chip shows *why* a message ran as `tools` or
   `full agent`, but there is no way to replay it in the other mode.
7. **Cosmetic/lint:** `ProviderError` is imported but unused in
   `providers/openai_compatible.py`; `ruff check` on the whole repo reports ~3.4k
   pre-existing findings (mostly E501 with ruff ≥ 0.16) — keep *new* files clean:
   `ruff check --select F,E9,B,SIM src/unknown_agent/web/`.
8. **Security hygiene:** the API keys and the GitHub PAT shared in this chat are
   compromised — rotate all of them (`https://github.com/settings/tokens`, unli
   dashboard). Never commit `config.toml` with a live key; use `env:` references.

## 5. Operational notes

- `ua upgrade --check` reads `__version__` from `src/unknown_agent/version.py`; every
  change to `src/`, `docs/` or `pyproject.toml` **must** bump it, add a `CHANGELOG.md`
  entry and push a matching `vX.Y.Z` tag, or installed users never see the fix.
- Push without leaving a token on disk:
  ```bash
  read -rs GITHUB_TOKEN && export GITHUB_TOKEN
  git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' push origin main
  git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' push origin v1.6.0
  unset GITHUB_TOKEN && git remote -v   # must show no token
  ```
