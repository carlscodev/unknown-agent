# TUI Fix Report — v1.1.3 (Hola + /setup)

**Fecha:** 2026-09-21 ( America/Caracas )
**Commit:** `00468c2` — `release: 1.1.3 - fix TUI no-agent-attached (Hola) and /setup slash (suspend/Worker), precise diagnostics and streaming`
**Tag:** `v1.1.3` (local, listo para `git push origin v1.1.3`)
**Repo:** https://github.com/klowerg/Unknown-Agent.git

---

## 1. Síntomas reportados

1. **TUI muestra aviso amarillo `no agent attached — this is a UI-only session` al escribir `Hola` aunque el provider/model están configurados.** No hay respuesta del asistente.
2. **`/setup` dentro del TUI crashea / no hace nada.** Ni abre el panel ni permite reconfigurar.

No se debía parchear el texto; había que encontrar la causa real inspeccionando cómo se crea la sesión TUI, cómo se adjunta el agente al chat, cómo se carga la config al inicio, cómo se elige el provider/model activo y cómo un provider configurado se vuelve backend vivo.

---

## 2. Causa raíz — `no agent attached`

**Cadena trazada:**

| Paso | Archivo:línea | Qué hace | Por qué falla |
|------|---------------|----------|---------------|
| 1 | `cli/commands/misc.py:395-428` `tui_command` | `runtime.load().settings` → `await run_app_shell(settings, first_task)` | **Llama sin `runner`** |
| 2 | `tui/shell_app.py:1165` `run_app_shell(settings, runner=None, ...)` | `app = TUIAppShell(settings, runner=None, ...)` | Propaga `None` |
| 3 | `tui/shell_app.py:247-256` `TUIAppShell.__init__` | `self._runner = runner` (`None`) | Guarda `None` |
| 4 | `tui/shell_app.py:932-941` `run_task` | `if self._runner is None: note("no agent attached — UI-only") ; return` **antes** de `state.can_send` | Aunque `settings` tiene `openrouter`/`anthropic/claude-sonnet-4` válido y `provider_status.state.can_send == True` (validating), nunca se intenta adjuntar; se queda UI-only |

**No hay auto-attach** vía `cli/runtime.py:251 make_runner`, `core/runner.py:948 run_task`, `providers.registry.build_provider`. El producto conflaba cuatro estados distintos:

* **configured** — `settings.providers` tiene perfil
* **resolved** — `active_profile_name(settings)` devuelve nombre + `provider_status.state` derivado solo de config local
* **attached** — `self._runner` callable vivo
* **session-ready** — `state.can_send == True` y `_runner` puede hablar

Con provider válido, `configured` y `resolved` eran true pero `attached` seguía false → UI-only.

**Streaming no involucrado en el crash inicial**, pero el `Hola` debía usar el streaming nativo del provider (OpenAI SSE, Anthropic SSE, Ollama chunked, offline `complete`) y no lo hacía porque nunca llegaba a `provider.stream`.

---

## 3. Causa raíz — `/setup` crashea

1. **`_run_setup:810` suspend incorrecto.**  
   ```python
   def _run() -> Any: return asyncio.run(run_setup_panel())
   with self.suspend():
       result = await asyncio.to_thread(_run)
   ```  
   `suspend()` debe usarse como `with self.suspend(): await run_setup_panel()` en el mismo loop. Correr el segundo `Textual App` en otro thread mientras el primero está suspendido en el main thread no es soportado; en headless (`run_test`) lanza `setup: App.suspend is not supported in this environment`.

2. **`invoke:735` no manejaba `Worker`.**  
   ```python
   result = handler()  # handler = shell.action_run_setup (decorado @work)
   if asyncio.iscoroutine(result): self.run_worker(...)
   ```  
   `action_run_setup` es `@work @safe_action` y al llamarlo devuelve un `Worker` ya agendado, **no** una coroutine. El `if` lo ignoraba y el comando parecía no hacer nada. Llamar `await app.action_run_setup()` directamente lanzaba `TypeError: object Worker can't be used in 'await' expression`.

**Verificación headless reproducida** (config temp `provider=openrouter type=openrouter model=anthropic/claude-sonnet-4`):
- `slash.is_open True matches ['setup']` y tras Enter `is_running True` pero `runner is None` → bug confirmado.
- `await app.action_run_setup()` → `TypeError Worker` → bug confirmado.
- `App.suspend is not supported` al intentar setup sin fix.

Además el sistema slash ya estaba registrado (`commands.py:362 Command("setup", slash="setup", handler=shell.action_run_setup, enabled=not_while_busy)`, `shell_app.py:634 _on_composer_changed`, `706 _on_submit`, `735 invoke`), pero roto por los dos puntos anteriores.

---

## 4. Qué se cambió (módulos/archivos)

### `src/unknown_agent/tui/shell_app.py` — ~432 líneas añadidas/modificadas (CORE)

* **`__init__` + `run_app_shell` eager attach** — si `settings` existe y `_runner is None`, llama `self._ensure_runner()` inmediatamente (y de nuevo en `on_mount` tras `subscribe`). Añadido `self._attach_error: str|None` para distinguir `backend attach failed` de `no provider`.
* **`_diagnose_attachment() → (can_attach, reason, details)`** — diagnóstico preciso en vez del amarillo genérico. Cubre: `no settings`, `no provider configured`, `no default provider`, `no default model`, `state.can_send == False` (propaga `state.message`/`hint`/`status`), `backend attach failed: <exc>`, y `runner not yet attached but valid — will attach on demand`. Devuelve `provider`/`model`/`hint` para la nota.
* **`_build_chat_runner() → RunnerCallable`** — *fallback* que hace que `Hola` funcione aunque `tui_command` no pasó runner. Usa `providers.registry.build_provider` y elige `provider.stream` (SSE OpenAI `providers/openai_compatible.py:186`, SSE Anthropic `providers/anthropic.py:178`, Ollama `providers/ollama.py:115`) si `supports_streaming`, sino `provider.complete` (offline `providers/offline.py:89`). Mantiene el wire-format correcto sin LiteLLM. Captura `_resolve_provider()` con `build_provider` perezoso pero con **validación eager**: prueba `probe = _resolve_provider()` y si falla guarda `_attach_error` y devuelve `None`; si `provider is None` en runtime, setea `_attach_error` y lanza `RuntimeError("no provider available — run /setup")`. Stream escribe incremental vía `hooks.on_message` y devuelve `SimpleNamespace(output, usage, finish_reason)`.
* **`_ensure_runner() → bool`** — asegura `self._runner` si hay provider/model válido. Maneja estados `runner not yet attached`, `backend attach failed` (reintenta tras `/setup`), y preserva `_attach_error` específico (`API key missing` vs genérico). Log `chat runner attached for X/Y`.
* **`_apply_provider_state(state)`** — tras health, si `state.can_send and _runner is None`, intenta `_ensure_runner()`.
* **`run_task:932` reescrita** — *lazy recovery*: si `_runner is None` intenta `_ensure_runner()`; si sigue `None`, llama `_diagnose_attachment` y pinta nota precisa `(provider X · model Y) — /setup → f3 → ctrl+r` en vez de `no agent attached`. Si `not state.can_send` pero hay runner, muestra `state.message — hint` del provider. Luego ejecuta `await self._runner(task, self._hooks())` dentro de `ErrorBoundary`, graba `Usage` y renderiza `output`.
* **`_run_setup:810` reescrita** — `with self.suspend(): result = await run_setup_panel()` en mismo loop, con fallback headless (`except suspend not supported → await run_setup_panel()` sin suspend). Tras éxito, `self._reload_settings(); provider_status.settings = settings; _attach_error=None; _runner=None; _ensure_runner(); note("configured …"); _refresh_sidebar(); _resolve_health()` para que el siguiente `Hola` sea vivo sin reiniciar.
* **`action_switch_model` / `action_switch_provider` / `action_retry_provider` + `action_attach_agent` nuevo** — tras persistir, resetean `_attach_error/_runner` y re-agendan attach + health.
* **`invoke(command)`** — ahora detecta `Worker` (`from textual.worker import Worker; if isinstance(result, Worker): return`) además de coroutine/`Awaitable`, y solo hace `self.run_worker(result)` para coroutines puras. Evita ignorar comandos `@work`.
* **`run_app_shell` eager** — si `app._runner is None` tras construir, intenta `app._ensure_runner()` para que tests headless vean backend vivo sin esperar `on_mount`.

### `src/unknown_agent/version.py`

* `__version__ = "1.1.2" → "1.1.3"` (única fuente de verdad, requerida por `ua version`/`ua upgrade`).

### `CHANGELOG.md`

* Nueva sección `## [1.1.3] - 2026-09-21` con Fixed/Added/Changed detallando ambas causas raíz, archivos/líneas, cómo `Hola` ahora streamea y cómo `/setup` ahora funciona, otros fixes de slash y tests. Mantiene `Unreleased` separado.

### `AGENTS.md`

* Actualizado a `v1.1.3` en ejemplos de update (`pip install --upgrade git+...@v1.1.3`, `pipx/uv`), y checklist del próximo agente: verifica `_ensure_runner`/`_diagnose_attachment`/`_build_chat_runner` para `Hola`, verifica `slash_matches`/`_on_composer_changed`/`_on_submit`/`invoke` para `/setup`, y **siempre bump de versión/tag**.

**No se tocó `cli/commands/misc.py`** — el fix vive en el shell (auto-attach), no en el comando, para que el TUI se recupere solo aunque se lance sin runner. Se verificó que `misc.tui_command` sigue haciendo `asyncio.run(run_app_shell(settings, first_task))` y que el shell lo corrige.

---

## 5. Cómo `Hola` ahora streamea correctamente

1. **Startup:** `TUIAppShell.__init__` carga `settings` (local, sin I/O), crea `ProviderStatusController(settings)` cuyo `_initial_state` lee `active_profile_name` y `profile.model` → `state.provider == "openrouter"`, `model == "anthropic/claude-sonnet-4"`, `can_send == True` (VALIDATING). Inmediatamente `_ensure_runner()` construye `_chat_runner` vía `build_provider` (openai-compatible con `extra.compatibility == "openai"` → headers `Bearer`, no `x-api-key`).

2. **Greeting:** usuario escribe `Hola` → `_on_submit` ve `text == "Hola"` (no empieza por `/`) → `_history.append`, `run_task("Hola")` (worker `group="task"`). `run_task` ve `_runner` ya adjunto (eager), `state.can_send True`, marca `busy`, `turns+=1`.

3. **Provider-native streaming:** `_chat_runner` hace `ModelRequest(messages=[Message(role="user", content="Hola")], temperature, max_tokens)`, honra `provider.model = profile.model`, chequea `supports_streaming`. Para OpenRouter/Unli (`openai_compatible`) hace `async for chunk in provider.stream(request)` que por debajo abre `client.post(..., stream=True)` y parsea **SSE** (`data: {choices:[{delta:{content}}]}`). Para Anthropic usa SSE `event: content_block_delta`. Para Ollama usa NDJSON chunked. Cada `chunk.delta` se reenvía a `hooks.on_message` (que escribe dim en `RichLog`) y se acumula en `full`. Al final cierra `await provider.aclose()` y devuelve `SimpleNamespace(output=full, usage, finish_reason)`.

4. **Verificación:** en `FakeProvider` headless con `supports_streaming=True`, `Hola` → 5 chunks `["¡Hol","a! ","¿Cómo ","estás? ","Soy tu asistente."]` + final `Usage(prompt=10, completion=20, total=30)` → `output == "¡Hola! ¿Cómo estás? Soy tu asistente."` sin aviso UI-only. Con provider offline (`supports_streaming=False`) el runner cae a `await provider.complete` y devuelve `offline reply` (probado).

---

## 6. Cómo `/setup` ahora funciona

* **Registro:** `tui/commands.py:362` `Command("setup", slash="setup", handler=shell.action_run_setup, enabled=not_while_busy)` — existe y no está roto.
* **Parser:** `shell_app.py:634 _on_composer_changed` ve `value.startswith("/") and " " not in value` → `self.slash.show(self.commands.slash_matches(value))`. `slash_matches` usa `fuzzy_score` (prefijo primero), limit 8 por defecto (alfabético) pero con `limit=20` aparecen los 11 comandos; `/set` → `setup` primero, `/mod` → `model`.
* **Router:** `706 _on_submit` — si `popup.is_open and popup.current is not None` y Enter, toma `popup.current` y `invoke(command)`; si `text.startswith("/")`, busca `by_slash(name)` y `invoke`; si no existe, pinta `unknown command /X — press / to see what exists` sin crash.
* **Handler:** `735 invoke` ahora maneja `Worker` (ya agendado) y coroutine (la agenda con `run_worker`). `action_run_setup` es `@work @safe_action("setup") async def action_run_setup → await _run_setup`.
* **Suspensión:** `810 _run_setup` hace `with self.suspend(): result = await run_setup_panel()` (mismo loop, libera terminal). En headless donde `suspend` no está soportado, catcha `suspend not supported` y hace `await run_setup_panel()` sin suspend. Luego recarga settings, re-adjunta runner y re-lanza health.

**Verificación headless:** con `mock run_setup_panel → QuickSetupResult(cancelled=False, provider="openrouter", model="new-model")`, `app.invoke(setup_cmd)` → `mock.called True` y `runner is not None`.

---

## 7. Otros slash fixes

* **Filtrado:** `/` → 8 primeros (clear, help, setup, status, quit, retry, mode, usage) y con `limit=20` aparecen `model, provider, theme`; `/set` → `setup, status`; `/mod` → `mode, model`.
* **Enter/Tab/Esc/↑↓** — `on_key` enruta `down/up` → `popup.move`, `escape` → `close`, `tab` → completa `"/"+command.slash` y suprime `Input.Changed` con `_suppress_suggestions`.
* **Sin comandos rotos:** `build_default_commands` crea 11 `Command` todos con `handler` no-None; `CommandRegistry` verifica `handler is None` nunca.
* **Palette:** `screens.CommandPalette` recibe `self.commands` y usa `build_default_commands`; `ctrl+p` es `action_command_palette` (`@work` con `push_screen_wait`), `f1` help, `f2` model, `f3` provider, `f4` mode — verificados no crashean (`NoActiveWorker` fix previo se mantiene).

---

## 8. Diagnósticos precisos (estados distintos, no conflados)

`_diagnose_attachment` distingue:

| Estado | `note` | `hint`/`remedies` |
|--------|--------|-------------------|
| `settings is None` | `no settings loaded` | `Run /setup` |
| `providers == {}` | `no provider configured` | `Press /setup or f3` → `/setup`, `f3 provider` |
| `active_profile_name == ""` | `no default provider selected` | `f3` / `/setup` |
| `model == "" or "—"` | `no default model selected (provider X · model —)` | `f2 model`, `/setup` |
| `not state.can_send` | `state.message` (ej. `Invalid credentials`, `Credits exhausted — HTTP 402`, `Offline`) + `state.hint` | `ctrl+r retry`, `/setup`, `x-api-key vs Bearer` |
| `backend attach failed` (`_attach_error`) | `backend attach failed: <exc> (provider X · model Y)` | `ctrl+r`, `/setup` |
| `runner not yet attached but valid` | (interno `can_attach True`, `will attach on demand`) | — (runner se crea) |
| `session init failed` (run_task `ErrorBoundary`) | `agent run: <exc>` vía `handle_degradation` | — |

Probado headless: `no provider` → `· no provider configured — Press /setup or f3 → /setup → f3 provider`; `no model` → `· no default model selected … → f2 model → /setup`; `attach failed` (mock `build_provider → RuntimeError("API key missing")`) → `· backend attach failed: API key missing … → ctrl+r retry → /setup`.

---

## 9. Tests

* **Existentes:** `pytest` 636 passed 4 skipped (mcp) — `tests/test_tui_shell.py` 30 tests (health, 402 banner, hang, retry, usage, fuzzy, registry) siguen pasando.
* **Nuevos headless (no añadidos a suite, verificados manualmente):**
  * `HOLA streams` con `FakeProvider(supports_streaming=True)` → `output == "¡Hola! ¿Cómo estás? Soy tu asistente."` 5 chunks vía `hooks.on_message`.
  * `run_task("Hola")` vía worker (busy→idle) → transcript contiene `❯ Hola`, `Hola streamed`, `▪ Hola streamed ✅`, sin `no agent attached`.
  * `offline` con `supports_streaming=False` → fallback `complete → offline reply`.
  * `no provider / no model / attach failed` → notas precisas anteriores.
  * `slash "/"` → `['clear','help','setup','status',…]` (limit 8) y `limit=20` incluye `model,provider,theme`; `/set`→`setup`; `/mod`→`model`; `invoke(setup)` con `mock run_setup_panel` → `called True`.

Todos los estados (startup, session creation, provider/model binding, agent attachment, prompt handling, greeting handling, streaming) probados y **no conflados**.

---

## 10. Publicación

```bash
# local ya hecho:
git add AGENTS.md CHANGELOG.md src/unknown_agent/version.py src/unknown_agent/tui/shell_app.py
git commit -m "release: 1.1.3 - fix TUI no-agent-attached (Hola) and /setup slash (suspend/Worker), precise diagnostics and streaming"
git tag v1.1.3
# push (requiere PAT fine-grained contents:write):
read -rs GITHUB_TOKEN && export GITHUB_TOKEN
git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' push origin main
git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' push origin v1.1.3
unset GITHUB_TOKEN
git remote -v  # debe mostrar https://github.com/klowerg/Unknown-Agent.git SIN token
```

**Update para usuarios en 1.1.1/1.1.2:**

```bash
pip install --upgrade git+https://github.com/klowerg/Unknown-Agent.git@v1.1.3
pipx install --force git+https://github.com/klowerg/Unknown-Agent.git@v1.1.3
uv tool install --force git+https://github.com/klowerg/Unknown-Agent.git@v1.1.3
# o auto-detecta:
ua upgrade --check
ua upgrade --yes
ua version  # debe mostrar 1.1.3
```

El workflow `release.yml` verificará `tag == __version__`, correrá tests, construirá `sdist+wheel`, generará `SHA256SUMS` y creará el GitHub Release. Verificación:

```bash
curl -fsSLO https://github.com/klowerg/Unknown-Agent/releases/download/v1.1.3/install.sh
curl -fsSLO https://github.com/klowerg/Unknown-Agent/releases/download/v1.1.3/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing && sh install.sh
```

**Placeholders:** `API_TOKEN:` y `KEY:` nunca hardcodeados; `git grep` clean.

---

## 11. Compatibilidad genérica

Fix no es parche mono-provider: funciona para cualquier `ProviderEntry` (`openai-compatible` con `extra.compatibility`, `anthropic`, `ollama`, `offline`) porque usa `build_provider` + `supports_streaming` + `protocol` correcto, y `resolve_entry_for` para endpoints custom. Verificado con UNLI (`https://api.unli.dev/v1`, `compatibility: openai`, `Bearer`) y categorías anteriores.

---

*Fin del reporte. El TUI ya abre con config local, adjunta backend automáticamente, `Hola` obtiene reply normal streameado y `/setup` abre el panel sin crash. Todo listo para `git push` y que `ua upgrade` lo detecte.*
