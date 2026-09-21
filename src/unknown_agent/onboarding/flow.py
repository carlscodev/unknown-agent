"""The onboarding state machine: choose -> paste -> validate -> models -> save.

Flow contract (identical in interactive and non-interactive mode):

1. **choose a provider**      - catalog list, searchable by name or alias;
2. **paste the API key**      - masked input, or ``--api-key``/``--api-key-env`` for CI;
3. **validate the credential**- live call; failures explain themselves and offer retry;
4. **fetch models**           - live listing where supported, curated list otherwise;
5. **choose a default model** - tool-capable models ranked first;
6. **save**                   - profile in config, secret in the credential store (0600);
7. **prove tool calling**     - adapter round-trip check before the user relies on it.

Everything is resumable: an interrupted setup leaves either the old configuration
untouched or a complete new one, never a half-written profile.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from unknown_agent.config.credentials import CredentialStore, default_credentials_path
from unknown_agent.config.secrets import REDACTOR, looks_like_api_key
from unknown_agent.errors import ConfigError, UnknownAgentError
from unknown_agent.observability.logging import get_logger
from unknown_agent.onboarding import screens
from unknown_agent.providers.catalog import ProviderEntry, catalog_payload, known_model_ids, profile_name_for, resolve_entry
from unknown_agent.providers.discovery import ModelInfo, ProviderProbeError, fetch_models, validate_credential
from unknown_agent.ui.console import output
from unknown_agent.ui.prompts import Prompter

log = get_logger("onboarding.flow")


@dataclass(slots=True)
class OnboardingResult:
    provider: str          # profile name written to config
    provider_type: str     # catalog key
    provider_label: str
    model: str
    credential_ref: str    # "store:x" / "env:X" / "" for keyless providers
    credential_stored: bool
    models: list[str] = field(default_factory=list)
    config_path: str = ""
    credential_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_type": self.provider_type,
            "provider_label": self.provider_label,
            "model": self.model,
            "credential_ref": self.credential_ref,
            "credential_stored": self.credential_stored,
            "models": self.models,
            "config_path": self.config_path,
            "credential_path": self.credential_path,
        }


@dataclass(slots=True)
class OnboardingOptions:
    """Non-interactive inputs (flags), so setup works in CI and in scripts."""

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    profile_name: str | None = None
    store: CredentialStore | None = None
    skip_live_check: bool = False
    allow_keyless: bool = True


async def run_onboarding(
    runtime: Any,
    *,
    options: OnboardingOptions | None = None,
    prompter: Prompter | None = None,
) -> OnboardingResult | None:
    """Run the flow.  Returns ``None`` when the user aborts at any step."""

    options = options or OnboardingOptions()
    prompter = prompter or getattr(runtime, "prompter", None) or Prompter()
    store = options.store or CredentialStore()
    loaded = runtime.load(force=True)
    settings = loaded.settings

    interactive = _is_interactive(prompter)
    if interactive:
        output.print(screens.banner())

    def cancelled(step: str) -> None:
        output.print(
            screens.info_panel(
                "setup cancelled",
                f"stopped at: {step}. Nothing was written - your configuration is unchanged.",
                hint="Run `ua setup` again whenever you want, or use flags for scripted setups "
                "(`ua setup --provider groq --api-key-env GROQ_API_KEY`).",
            )
        )

    # 1 - provider ---------------------------------------------------------
    entry = await _choose_provider(settings, options, prompter, interactive=interactive)
    if entry is None:
        cancelled("provider selection")
        return None

    profile_name = options.profile_name or profile_name_for(entry, settings.providers)

    # 1b - endpoint, for providers that cannot ship a default -----------------
    if entry.requires_base_url and not options.base_url:
        if interactive:
            screens.step_header(2, "Endpoint", f"{entry.label} needs your own base URL (it is resource-specific)")
            options.base_url = prompter.text(f"{entry.label} base URL", default="", allow_empty=False).strip()
        else:
            raise ConfigError(
                f"{entry.label} needs --base-url.",
                hint=f"ua setup --provider {entry.key} --base-url https://<your-resource>/v1 --api-key-env {entry.env_var}",
            )

    # 2 - credential -------------------------------------------------------
    credential_ref, stored = await _collect_credential(entry, options, prompter, store, interactive=interactive)
    if credential_ref is None:
        cancelled("credential entry")
        return None

    # 3/4 - validate and list models ---------------------------------------
    models = await _validate_and_list(entry, options, credential_ref, prompter, interactive=interactive)
    if models is None:
        cancelled("provider validation")
        return None

    # 5 - default model ----------------------------------------------------
    model = await _choose_model(entry, models, options, prompter, interactive=interactive)
    if model is None:
        cancelled("model selection")
        return None

    # 6 - persist ----------------------------------------------------------
    result = _persist(runtime, settings, entry, profile_name, model, credential_ref, stored, models, store, options)

    # 7 - prove tool calling ------------------------------------------------
    _verify_tools(settings, profile_name, entry, interactive=interactive)

    # 8 - capabilities: vision + MCP (optional, interactive only) -----------
    if interactive:
        _capabilities_step(runtime, settings, profile_name, model, prompter)

    if interactive:
        output.print(
            screens.success_panel(
                entry.label,
                model,
                models=len(models),
                config_path=result.config_path,
                next_steps=(
                    "ua run \"build a landing page for my API\"",
                    "ua doctor          # verify everything end to end",
                    "ua providers list  # add or switch providers later",
                ),
            )
        )
    else:
        output.diag_print(f"configured {entry.label} profile {profile_name!r} with model {model!r}")
    return result


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
async def _choose_provider(
    settings: Any,
    options: OnboardingOptions,
    prompter: Prompter,
    *,
    interactive: bool,
) -> ProviderEntry | None:
    from unknown_agent.providers.catalog import CATALOG

    if options.provider:
        entry = resolve_entry(options.provider)
        if entry is None:
            raise ConfigError(
                f"Unknown provider {options.provider!r}.",
                hint="Run `ua providers catalog` to see supported providers.",
            )
        if entry.api_key_required and not (options.api_key or options.api_key_env or _env_key(entry)):
            if not interactive:
                # Scripted run without a key: fail now with the exact flag to add.
                raise ConfigError(
                    f"{entry.label} needs an API key.",
                    hint=f"Pass --api-key-env {entry.env_var} (recommended) or run `ua setup` interactively.",
                )
            # Interactive: the very next step is the masked key prompt, which is the
            # whole point of the flow - no need to make the user pass a flag too.
            output.diag_print(f"{entry.label} requires an API key - paste it in the next step.")
        return entry

    entries = list(CATALOG)
    while True:
        if interactive:
            screens.step_header(1, "Choose a provider", "every provider below has a working adapter; Enter selects the recommended one")
        output.print(screens.provider_table(entries, highlight=tuple(getattr(settings, "providers", {}) or ())))
        answer = prompter.text("provider (name, number, or Enter for OpenRouter)", default="openrouter", allow_empty=True)
        if not answer:
            return entries[0]
        if answer.isdigit() and 1 <= int(answer) <= len(entries):
            return entries[int(answer) - 1]
        entry = resolve_entry(answer)
        if entry:
            if entry.api_key_required and not (options.api_key or options.api_key_env or _env_key(entry)):
                output.diag_print(f"{entry.label} requires an API key - you'll paste it in the next step.")
            return entry
        output.print(screens.failure_panel("unknown provider", f"{answer!r} is not in the catalog.", hint="Try 'openrouter', 'openai', 'anthropic', 'groq'..."))
        if not interactive:
            return None


async def _collect_credential(
    entry: ProviderEntry,
    options: OnboardingOptions,
    prompter: Prompter,
    store: CredentialStore,
    *,
    interactive: bool,
) -> tuple[str | None, bool]:
    """Returns ``(reference, stored_in_credential_file)``."""

    if not entry.api_key_required:
        return "", False

    if options.api_key:
        # CI path: the value stays in memory and is written to the credential store.
        if not looks_like_api_key(options.api_key):
            raise ConfigError("--api-key does not look like an API key.", hint="Check for quotes or shell history substitution.")
        REDACTOR.register(options.api_key)
        store.set(entry.key, options.api_key)
        return f"store:{entry.key}", True

    env_name = options.api_key_env or _env_var_present(entry)
    if options.api_key_env or (env_name and not interactive):
        name = options.api_key_env or entry.env_var
        value = os.environ.get(name, "")
        if not value:
            raise ConfigError(f"Environment variable {name} is empty.", hint=f"export {name}=... and retry.")
        REDACTOR.register(value)
        return f"env:{name}", False

    for attempt in range(3):
        if interactive:
            screens.step_header(
                2,
                "Paste your API key",
                f"{entry.label} · the key is masked, validated immediately, and stored with 0600 permissions",
            )
            output.print(screens.key_prompt_panel(entry, existing_hint=_env_var_present(entry) and f"{entry.env_var} is set in your environment" or ""))
        key = prompter.text(f"{entry.label} API key (input hidden)", secret=True, allow_empty=True)
        if not key:
            if _env_var_present(entry) and _confirm(prompter, "Use the key already in your environment instead?", default=True):
                name = options.api_key_env or entry.env_var
                REDACTOR.register(os.environ[name])
                return f"env:{name}", False
            output.diag_print("no key entered")
            return None, False
        key = key.strip().strip("'\"")
        if not looks_like_api_key(key):
            output.print(
                screens.failure_panel(
                    "that does not look like an API key",
                    f"{_fingerprint(key)} is too short or contains spaces.",
                    hint="Copy the key again from your provider dashboard (they usually look like sk-..., sk-or-v1-...).",
                    options=("retry", "cancel"),
                )
            )
            continue

        REDACTOR.register(key)
        if options.skip_live_check:
            store.set(entry.key, key)
            return f"store:{entry.key}", True

        result = await _with_status("validating the key", validate_credential(entry, api_key=key, base_url=options.base_url))
        if result.ok:
            output.diag_print(f"✔ {result.message}")
            store.set(entry.key, key)
            return f"store:{entry.key}", True
        output.print(
            screens.failure_panel(
                "the provider rejected that key",
                result.message,
                hint=result.hint,
                options=("paste it again", "switch provider", "cancel (Esc/Ctrl-C)"),
            )
        )
        if attempt == 2:
            return None, False
    return None, False


async def _validate_and_list(
    entry: ProviderEntry,
    options: OnboardingOptions,
    credential_ref: str,
    prompter: Prompter,
    *,
    interactive: bool,
) -> list[ModelInfo] | None:
    api_key = _resolve_ref(credential_ref)
    if interactive:
        screens.step_header(3, "Validating and fetching models", f"talking to {entry.label} ...")

    if not options.skip_live_check:
        result = await _with_status("validating the key", validate_credential(entry, api_key=api_key, base_url=options.base_url))
        if not result.ok:
            if interactive:
                output.print(
                    screens.failure_panel(
                        "could not verify this provider",
                        result.message,
                        hint=result.hint,
                        options=("retry", "continue with the curated model list", "cancel"),
                    )
                )
                choice = prompter.text("retry? [R]etry / [c]ontinue / [a]bort", default="r", allow_empty=True).lower()
                if choice.startswith("r"):
                    return await _validate_and_list(entry, options, credential_ref, prompter, interactive=interactive)
                if choice.startswith("c"):
                    return _fallback_models(entry)
                return None
            raise ConfigError(result.message, hint=result.hint)
    if options.skip_live_check and entry.model_listing not in {"none", ""}:
        # `--skip-check` means "do not probe my credential" (offline setups, air-gapped
        # CI) - it does not mean "do not discover models".  Listing is attempted, and a
        # failure degrades to the curated catalog instead of aborting setup.
        try:
            listed = await _with_status("fetching models", fetch_models(entry, api_key=api_key, base_url=options.base_url))
        except (ProviderProbeError, ConfigError) as exc:
            output.diag_print(f"model listing unavailable ({exc}); using the curated list")
            listed = []
        if listed:
            output.diag_print(f"{len(listed)} model(s) available from {entry.label}")
            return listed
        return _fallback_models(entry)

    try:
        models = await _with_status("fetching models", fetch_models(entry, api_key=api_key, base_url=options.base_url))
        output.diag_print(f"✔ {len(models)} model(s) available from {entry.label}")
        return models
    except ProviderProbeError as exc:
        if entry.model_listing == "none":
            return _fallback_models(entry)
        if interactive:
            output.print(
                screens.failure_panel(
                    "model listing failed",
                    exc.message,
                    hint=exc.hint,
                    options=("retry", "use the curated list from the catalog", "cancel"),
                )
            )
            choice = prompter.text("retry? [R]etry / [c]ontinue / [a]bort", default="r", allow_empty=True).lower()
            if choice.startswith("r"):
                return await _validate_and_list(entry, options, credential_ref, prompter, interactive=interactive)
            if choice.startswith("c"):
                return _fallback_models(entry)
            return None
        return _fallback_models(entry)


def _fallback_models(entry: ProviderEntry) -> list[ModelInfo]:
    """Providers that cannot list models still get a usable chooser."""

    output.diag_print(f"{entry.label} does not expose a model list - using the catalog's curated set.")
    return [ModelInfo(id=model_id, name=model_id, provider=entry.key) for model_id in known_model_ids(entry)]


async def _choose_model(
    entry: ProviderEntry,
    models: list[ModelInfo],
    options: OnboardingOptions,
    prompter: Prompter,
    *,
    interactive: bool,
) -> str | None:
    wanted = options.model
    if not models and wanted:
        # Vendors whose model names are user-owned (Azure deployments): whatever the user
        # typed *is* the model - there is no catalogue to check it against.
        return wanted
    if wanted:
        if not any(model.id == wanted for model in models):
            closest = _closest(wanted, [model.id for model in models])
            if interactive and _confirm(prompter, f"{entry.label} has no model {wanted!r}. Use {closest!r} instead?", default=True):
                return closest
            if not interactive:
                raise ConfigError(
                    f"{entry.label} has no model {wanted!r}.",
                    hint=f"Closest match: {closest!r}. Run `ua models list --provider {entry.key}`.",
                )
        return wanted

    if not models:
        # Azure-style vendors: the "model" is a deployment the user named.  Ask for it
        # rather than ending onboarding with nothing selected.
        if interactive:
            screens.step_header(4, "Name your model", f"{entry.label} cannot list models - type the deployment or model name")
            answer = prompter.text(f"{entry.label} deployment/model name", default=entry.default_model or "", allow_empty=False).strip()
            return answer or None
        hint = (
            f"Start the local server, then re-run - or name a model directly: ua setup --provider {entry.key} --model <model-id>"
            if entry.local
            else f"ua setup --provider {entry.key} --model <deployment-or-model-name> ..."
        )
        raise ConfigError(f"{entry.label} cannot list models; pass --model explicitly.", hint=hint)

    ranked = _rank_models(models, preferred=entry.default_model)
    if interactive:
        hint = f"recommended: {entry.default_model}" if entry.default_model and ranked and ranked[0].id == entry.default_model else "tool-capable and cheaper models are listed first"
        screens.step_header(4, "Choose your default model", hint)
        output.print(screens.model_table(ranked))
        answer = prompter.text("model (number, name fragment, or Enter for the first)", default="1", allow_empty=True)
    else:
        return ranked[0].id if ranked else None

    if not answer:
        answer = "1"
    if answer.isdigit():
        index = int(answer) - 1
        if 0 <= index < len(ranked):
            return ranked[index].id
        output.diag_print(f"no model at position {answer}; picking the first")
        return ranked[0].id if ranked else None
    if answer.lower() == "all":
        output.print(screens.model_table(ranked, limit=len(ranked)))
        return await _choose_model(entry, models, options, prompter, interactive=interactive)
    matches = [model for model in ranked if answer.lower() in model.id.lower() or answer.lower() in model.name.lower()]
    if not matches:
        output.print(screens.failure_panel("no match", f"nothing in the list contains {answer!r}.", hint="Try 'all' to see everything."))
        return await _choose_model(entry, models, options, prompter, interactive=interactive)
    if len(matches) == 1:
        return matches[0].id
    output.print(screens.model_table(matches, limit=len(matches)))
    picked = prompter.text("which one? (number)", default="1", allow_empty=True)
    index = int(picked) - 1 if picked.isdigit() else 0
    return matches[index].id if 0 <= index < len(matches) else matches[0].id


def _persist(
    runtime: Any,
    settings: Any,
    entry: ProviderEntry,
    profile_name: str,
    model: str,
    credential_ref: str,
    stored: bool,
    models: list[ModelInfo],
    store: CredentialStore,
    options: OnboardingOptions | None = None,
) -> OnboardingResult:
    from unknown_agent.config.models import ProviderConfig

    profile_values = dict(entry.profile_defaults)
    profile_values["model"] = model or profile_values.get("model") or ""
    if options is not None and options.base_url:
        profile_values["base_url"] = options.base_url
    if credential_ref:
        profile_values["api_key"] = credential_ref
    profile = ProviderConfig(**profile_values)

    # Chosen model's real limits and capabilities, when the provider told us.
    # Persisting supports_vision here turns a heuristic guess into live-metadata
    # truth for every later capability resolution.
    chosen = next((item for item in models if item.id == model), None)
    if chosen and chosen.context_window:
        profile.context_window = int(chosen.context_window)
    if chosen and chosen.supports_vision:
        profile.supports_vision = True

    settings.providers[profile_name] = profile
    settings.provider = profile_name

    # The config injects a stock ``[providers.default]`` before onboarding runs.  Now
    # that a real profile exists it is noise ("which of these two is mine?"), so it is
    # dropped on the first successful setup unless it carries user-set values.
    for stale in [name for name, item in settings.providers.items() if name != profile_name and getattr(item, "is_placeholder", False)]:
        settings.providers.pop(stale, None)
        log.debug("dropped unconfigured placeholder profile %r", stale)

    writer = getattr(runtime, "save_settings", None)
    config_path = ""
    if callable(writer):
        config_path = str(writer(settings))

    result = OnboardingResult(
        provider=profile_name,
        provider_type=entry.key,
        provider_label=entry.label,
        model=model,
        credential_ref=credential_ref,
        credential_stored=stored,
        models=[item.id for item in models][:200],
        config_path=config_path,
        credential_path=str(store.path if stored else default_credentials_path()),
    )
    log.info("onboarding complete: profile=%s provider=%s model=%s", profile_name, entry.key, model)
    return result


def _capabilities_step(runtime: Any, settings: Any, profile_name: str, model: str, prompter: Prompter) -> None:
    """Tell the user what their model can see, and offer the MCP quick-start.

    Deliberately zero-friction: one line of fact about vision (no question -
    there is nothing to decide), one opt-in question about MCP. Declining
    changes nothing; accepting writes the two safest catalog servers.
    """

    from unknown_agent.providers.capabilities import resolve_capabilities

    caps = resolve_capabilities(settings.providers[profile_name], provider_name=profile_name)
    if caps.supports_vision:
        output.print(f"  [ua.ok]✓[/ua.ok] {model} accepts images - screenshot analysis and design comparison are live")
    else:
        output.print(
            f"  [ua.muted]○ {model} is text-only - set \\[vision] fallback_profile to a multimodal "
            "profile later to add sight ([cyan]ua vision status[/cyan] explains)[/ua.muted]"
        )

    if settings.mcp.servers:
        return  # already configured; nothing to offer
    if not _confirm(prompter, "Add the recommended MCP starter servers (filesystem, fetch)?", default=False):
        output.print("  [ua.muted]○ skipped - browse the ecosystem anytime with [cyan]ua mcp catalog[/cyan][/ua.muted]")
        return
    try:
        from unknown_agent.mcp.catalog import catalog_entry, config_from_entry

        added = []
        for entry_key in ("filesystem", "fetch"):
            item = catalog_entry(entry_key)
            if item is None:
                continue
            settings.mcp.servers[entry_key] = config_from_entry(item, project_root=str(getattr(runtime, "cwd", Path.cwd())))
            added.append(entry_key)
        writer = getattr(runtime, "save_settings", None)
        if added and callable(writer):
            writer(settings)
            output.print(f"  [ua.ok]✓[/ua.ok] added {', '.join(added)} - verify with [cyan]ua mcp test[/cyan]")
    except Exception as exc:  # noqa: BLE001 - optional step must never sink the setup
        output.diag_print(f"note: could not add MCP starters ({exc}); use `ua mcp add` later")


def _verify_tools(settings: Any, profile_name: str, entry: ProviderEntry, *, interactive: bool) -> None:
    """Prove the tool-call path before the user depends on it."""

    from unknown_agent.providers.registry import build_provider
    from unknown_agent.providers.toolkit import capability_report, check_tool_roundtrip

    profile = settings.providers[profile_name]
    capabilities = capability_report(profile, entry)
    if interactive:
        output.print(screens.capability_line(capabilities))
    if not entry.supports_tools:
        output.diag_print(
            f"note: {entry.label} does not support native tool calling - it works for questions and plans, "
            "but agentic editing needs a tool-capable provider."
        )
        return
    try:
        provider = build_provider(profile, name=profile_name)
    except UnknownAgentError as exc:
        output.diag_print(f"warning: could not construct the provider adapter: {exc.message}")
        return
    report = check_tool_roundtrip(provider)
    if not report.ok:
        output.diag_print("warning: the tool-call self-check did not pass:\n" + report.render())
    elif interactive:
        output.print(screens.capability_line({"tools": True}))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _with_status(label: str, coro: Any) -> Any:
    """Run an awaitable with a spinner in interactive terminals, quietly otherwise."""

    if output.interactive and output.animations:
        with output.status(label):
            return await coro
    output.diag_print(f"… {label}")
    return await coro


def _resolve_ref(reference: str) -> str | None:
    if not reference:
        return None
    from unknown_agent.config.secrets import resolve_secret

    value = resolve_secret(reference, key="provider credential")
    if value:
        REDACTOR.register(value)
    return value


def _env_key(entry: ProviderEntry) -> str | None:
    return os.environ.get(entry.env_var) if entry.env_var else None


def _env_var_present(entry: ProviderEntry) -> str | None:
    if entry.env_var and os.environ.get(entry.env_var):
        return entry.env_var
    return None


def _fingerprint(secret: str) -> str:
    """Never show a secret; show just enough to recognise it."""

    cleaned = secret.strip()
    if len(cleaned) <= 4:
        return "****"
    return f"…{cleaned[-4:]} ({len(cleaned)} chars)"


def _is_interactive(prompter: Prompter) -> bool:
    return not prompter.non_interactive and output.interactive


def _confirm(prompter: Prompter, question: str, *, default: bool = True) -> bool:
    return bool(prompter.confirm(question, default=default))


def _closest(target: str, candidates: Sequence[str]) -> str:
    import difflib

    matches = difflib.get_close_matches(target, list(candidates), n=1, cutoff=0.3)
    return matches[0] if matches else (candidates[0] if candidates else "")


def _rank_models(models: list[ModelInfo], preferred: str | None = None) -> list[ModelInfo]:
    """Best defaults first: the curated default, then tool-capable, cheap, non-experimental.

    ``preferred`` is the vendor's documented default model (``entry.default_model``).
    A curated default is a better answer than "whatever happens to be cheapest", but it
    is only a *preference*: if the vendor no longer offers it, ranking still produces a
    sensible list instead of an empty one.
    """

    def score(model: ModelInfo) -> tuple[int, int, int, int, float, int, str]:
        tools = 0 if model.supports_tools is not False else 1
        # Batch endpoints answer in hours, embedding/TTS/vision-only models cannot run
        # an agent loop - neither belongs in a default.
        weird = 1 if any(marker in model.id.lower() for marker in (":batch", ":extended", "embed", "whisper", "tts", "dall", "moderation", "vision-only")) else 0
        priced = 0 if model.input_cost_per_mtok is not None else 1
        # Free tiers are listed *below* paid models of the same shape: an agent that hits
        # a rate limit mid-task is a worse first experience than one that costs cents.
        # They stay one keystroke away in the chooser.
        free = 1 if ":free" in model.id.lower() or model.input_cost_per_mtok == 0 else 0
        cost = model.input_cost_per_mtok if model.input_cost_per_mtok is not None else 0.0
        context = -(model.context_window or 0)
        return (tools, weird, priced, free, cost, context, model.id)

    ranked = sorted(models, key=score)
    if preferred and any(model.id == preferred for model in ranked):
        ranked.sort(key=lambda model: 0 if model.id == preferred else 1)
    return ranked


def run_onboarding_sync(runtime: Any | None = None, *, options: OnboardingOptions | None = None) -> OnboardingResult | None:
    """Blocking wrapper for CLI commands, plugins and scripts.

    ``runtime`` may be omitted, in which case the standard CLI runtime (default
    config paths, project discovery, prompter) is used.
    """

    if runtime is None:
        from unknown_agent.cli.runtime import Runtime

        runtime = Runtime()
    return asyncio.run(run_onboarding(runtime, options=options))


__all__ = ["OnboardingOptions", "OnboardingResult", "catalog_payload", "run_onboarding", "run_onboarding_sync"]
