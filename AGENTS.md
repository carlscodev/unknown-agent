# AGENTS.md — Instrucciones para agentes (y humanos) que trabajan en Unknown Agent

> **Regla de oro:** *Nunca dejes el programa sin versionar.* Cada cambio que toca `src/`, `docs/` o `pyproject.toml` **DEBE** bumppear `src/unknown_agent/version.py` y `CHANGELOG.md` y crear un tag `vX.Y.Z` antes de `git push`. Esto es lo que hace que `ua upgrade --check` y `ua version` detecten la actualización en las máquinas de los usuarios. Si olvidas el bump, el usuario no ve el update.

---

## 1. Versionado — fuente única

* **Archivo único:** `src/unknown_agent/version.py` → `__version__ = "1.1.x"`
  * `pyproject.toml` usa `dynamic = ["version"]` + `[tool.hatch.version] path = "src/unknown_agent/version.py"` — **no dupliques la versión en otro lado.**
* **Política SemVer:**
  * `MAJOR` — ruptura de CLI, API de plugins o formato en disco.
  * `MINOR` — nuevos comandos, providers, gates, memoria, skills.
  * `PATCH` — bugfix, pulido TUI, parsing, perf. (ej. fix UNLI 1.1.0 → 1.1.1)
* **Detección por el programa:** `ua version` lee `__version__`; `ua upgrade --check` compara contra PyPI (`https://pypi.org/pypi/unknown-agent/json`) y, si la instalación vino de `git+https`, `host/install.py:upgrade_command_for` genera `pip install --upgrade git+https://github.com/klowerg/Unknown-Agent.git@vX.Y.Z`.

**Checklist obligatorio en cada PR/commit a `main`:**

```bash
# 1. bump
# edita src/unknown_agent/version.py: 1.1.1 -> 1.1.5 (o 1.2.0 si es feature)
# 2. changelog (arriba de [Unreleased])
#   ## [1.1.5] - 2026-09-22
#   ### Fixed/Added/Changed ...
# 3. commit + tag + push
git add src/unknown_agent/version.py CHANGELOG.md
git commit -m "release: 1.1.5 - <qué hace>"
git tag v1.1.5
git push origin main
git push origin v1.1.5
```

Si el paso 1-3 no se hace, el fix existe en `main` pero **el usuario con `1.1.1` nunca lo recibe**.

---

## 2. Publicar — GitHub (siempre) + PyPI (cuando toque)

### GitHub — seguro, sin token en disco

Ver `docs/publishing.md` completo, pero el atajo para agentes:

```bash
# NUNCA: git remote set-url origin https://TOKEN@github.com/...
# SÍ: helper efímero con variable (no queda en .git/config ni en history)
read -rs GITHUB_TOKEN && export GITHUB_TOKEN   # pega el PAT fine-grained (contents:write, solo este repo)
git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' \
    push origin main
git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' \
    push origin v1.1.5
unset GITHUB_TOKEN
# Verifica que el remote quedó limpio:
git remote -v  # debe mostrar https://github.com/klowerg/Unknown-Agent.git SIN token
```

* Usa PAT **fine-grained**, `contents: write` solo a `klowerg/Unknown-Agent`, expiración corta. Si se pegó en chat/log, **revócalo** en https://github.com/settings/tokens.
* El workflow `release.yml` verifica `tag == __version__`, corre tests, construye `sdist+wheel`, genera `SHA256SUMS` y crea el GitHub Release. El usuario puede verificar:
  ```bash
  curl -fsSLO https://github.com/klowerg/Unknown-Agent/releases/latest/download/install.sh
  curl -fsSLO https://github.com/klowerg/Unknown-Agent/releases/latest/download/SHA256SUMS
  sha256sum -c SHA256SUMS --ignore-missing && sh install.sh
  ```

### PyPI — cuando el nombre esté registrado

`docs/publishing.md` §4 explica *trusted publishing* (OIDC, sin token largo en secrets). Hasta entonces, **toda instalación es `git+https`**. Cuando se active PyPI, añadir el job `pypi` a `release.yml` y los usuarios podrán:

```bash
pipx upgrade unknown-agent
# o
ua upgrade --yes   # detecta modo (pipx/uv-tool/bootstrap/venv) y corre el comando correcto
```

---

## 3. Comandos que el usuario debe tener aquí mismo (copiar/pegar)

**Usuario en `1.1.0` o `1.1.1` → actualizar a `1.1.5` (o latest):**

```bash
# pip / venv
pip install --upgrade git+https://github.com/klowerg/Unknown-Agent.git@v1.1.5
# o latest sin tag
pip install --upgrade git+https://github.com/klowerg/Unknown-Agent.git

# pipx
pipx upgrade unknown-agent
# si falla porque vino de git:
pipx install --force git+https://github.com/klowerg/Unknown-Agent.git@v1.1.5

# uv
uv tool upgrade unknown-agent
# o
uv tool install --force git+https://github.com/klowerg/Unknown-Agent.git@v1.1.5

# nativo (auto-detecta tu modo)
ua upgrade --check   # muestra 1.1.1 -> 1.1.5
ua upgrade --yes

# verificar
ua version
ua version -v
```

---

## 4. Checklist para el próximo agente que toque UNLI u otro provider

- [ ] ¿Tu endpoint es `openai` o `anthropic`? Define `extra.compatibility` en `save_provider_choice` y `resolve_entry_for`.
- [ ] ¿El payload trae `capabilities.limits.*`? Extiende `_parse_openai_style` + `_tools_supported` / `_vision_supported` en `discovery.py`.
- [ ] ¿El live puede venir vacío? Implementa fallback `live → catalog (known_model_ids) → manual (typed input)` en `quicksetup.ModelScreen` y `shell_app._available_models`.
- [ ] ¿Header correcto? `build_client` debe elegir `Bearer` vs `x-api-key + anthropic-version`.
- [ ] ¿`provider_status` y `health` propagan `compatibility`? Sino, el banner dirá “not compatible”.
- [ ] ¿TUI `no agent attached` está arreglado? Verifica `TUIAppShell._ensure_runner`/`_diagnose_attachment`/`_build_chat_runner` y que `Hola` streamea vía `provider.stream` (SSE) con fallback a `complete`.
- [ ] ¿`/setup` funciona dentro del TUI? Verifica `slash_matches`/`_on_composer_changed`/`_on_submit`/`invoke` (Worker vs coroutine) y `suspend` → `await run_setup_panel()` sin `to_thread`.
- [ ] ¿Bumppeaste `version.py` + `CHANGELOG.md` + tag? Sino, el usuario no se actualiza.

---

## 5. Placeholders — nunca hardcodear secretos

Usa **siempre** `API_TOKEN:` y `KEY:` en docs, logs, ejemplos y tests. El secreto real solo vive en `CredentialStore` (`0600`, `store:<name>`) o en `env:VAR`. Verificación:

```bash
git grep -iE "(api[_-]?key|token|secret)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}" -- ':!docs/publishing.md' -- ':!AGENTS.md' || echo clean
```

---

*Este archivo es para que **cualquier agente** (Codex, Claude, Grok, etc.) sepa publicar sin olvidar el bump. Si cambias el flujo de release, actualiza este `AGENTS.md` y `docs/publishing.md` juntos.*
