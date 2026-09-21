"""``ua setup``, ``ua providers ...`` and ``ua models ...``.

The command surface mirrors the mental model of the product:

* ``ua setup`` - the onboarding flow (choose provider -> key -> models -> save);
* ``ua providers`` - manage *accounts*: list, add, remove, use, test, catalog;
* ``ua models`` - manage *models*: list, use, refresh.

Everything is scriptable: every interactive step has a flag, every command has
``--json``, and no command ever prints a secret.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.config.credentials import CredentialStore
from unknown_agent.config.secrets import resolve_secret
from unknown_agent.errors import ConfigError, ExitCode, UsageError
from unknown_agent.onboarding.detect import detect_setup_state
from unknown_agent.onboarding.flow import OnboardingOptions, run_onboarding
from unknown_agent.onboarding.panel import launch_setup
from unknown_agent.providers.catalog import CATALOG, catalog_payload, resolve_entry
from unknown_agent.providers.discovery import ProviderProbeError, fetch_models, validate_profile
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table

app = typer.Typer(help="Configure providers and models (onboarding, credentials, live model lists).", no_args_is_help=False)
setup_app = typer.Typer(help="Provider and model management.", no_args_is_help=False)

MODELS_CACHE = "models.json"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _store() -> CredentialStore:
    return CredentialStore()


def _api_key_for(profile: Any) -> str | None:
    if not getattr(profile, "api_key", None):
        return None
    value = resolve_secret(profile.api_key, key="provider credential")
    if value:
        from unknown_agent.config.secrets import REDACTOR

        REDACTOR.register(value)
    return value


def _cache_path(runtime: Runtime) -> Path:
    return runtime.paths.project_cache_dir / MODELS_CACHE


def _read_cache(runtime: Runtime) -> dict[str, list[dict[str, Any]]]:
    path = _cache_path(runtime)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cache(runtime: Runtime, provider: str, models: list[dict[str, Any]]) -> None:
    payload = _read_cache(runtime)
    payload[provider] = models
    path = _cache_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _active(runtime: Runtime) -> tuple[str, Any]:
    loaded = runtime.load()
    name = loaded.settings.provider or "default"
    profile = loaded.settings.providers.get(name)
    if profile is None:
        raise ConfigError(
            f"No provider profile named {name!r}.",
            hint="Run `ua setup` to configure one, or `ua providers list` to see what exists.",
        )
    return name, profile


# --------------------------------------------------------------------------- #
# ua setup
# --------------------------------------------------------------------------- #
@app.command("setup")
def setup(
    ctx: typer.Context,
    provider: str | None = typer.Option(None, "--provider", "-p", help="Provider key, e.g. openrouter, openai, groq."),
    model: str | None = typer.Option(None, "--model", "-m", help="Default model id (skips the chooser)."),
    api_key: str | None = typer.Option(None, "--api-key", help="API key (prefer --api-key-env in scripts and CI)."),
    api_key_env: str | None = typer.Option(None, "--api-key-env", help="Read the key from this environment variable."),
    base_url: str | None = typer.Option(None, "--base-url", help="Override the provider base URL (Azure, self-hosted)."),
    name: str | None = typer.Option(None, "--as", help="Profile name to write (defaults to the provider key)."),
    skip_check: bool = typer.Option(False, "--skip-check", help="Do not validate the key over the network."),
    force: bool = typer.Option(False, "--force", help="Re-run setup even when the configuration is already healthy."),
    classic: bool = typer.Option(False, "--classic", help="Use the plain prompt flow instead of the interactive panel."),
) -> None:
    """Open the interactive setup panel: pick a provider, paste a key, pick a model.

    With no flags this opens a full-screen panel - choose a provider from a list,
    paste the key in a dialog, and press Enter; the key is validated and the model
    catalog is fetched automatically, then you pick a model and it is saved.

    Examples:
      ua setup                                   # the guided panel (recommended)
      ua setup --classic                         # plain prompts (no full-screen UI)
      ua setup -p groq --api-key-env GROQ_API_KEY -m llama-3.3-70b-versatile   # scripted
    """

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    state = detect_setup_state(loaded.settings)

    if state.ready and not force and provider is None:
        if runtime.is_machine:
            payload = state.to_dict()
            payload["setup_status"] = payload.pop("status")
            payload["status"] = "already_configured"
            runtime.emit_json(payload)
        else:
            output.print(kv_table([("provider", state.provider), ("model", state.model)], title="already configured"))
            output.print("[ua.muted]re-run with --force to change provider, key or model[/ua.muted]")
        raise typer.Exit(ExitCode.OK)

    options = OnboardingOptions(
        provider=provider,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        base_url=base_url,
        profile_name=name,
        skip_live_check=skip_check,
        store=_store(),
    )
    # One entry point: the interactive panel when a human is at a terminal, the
    # scripted/guided flow otherwise.  No extra commands are ever required.
    outcome = launch_setup(runtime, scripted=options, force_fallback=classic)
    if not outcome.ok:
        if runtime.is_machine:
            runtime.emit_json(outcome.to_dict())
        raise typer.Exit(ExitCode.INTERRUPTED)
    if runtime.is_machine:
        runtime.emit_json(outcome.to_dict())
    raise typer.Exit(ExitCode.OK)


# --------------------------------------------------------------------------- #
# ua providers ...
# --------------------------------------------------------------------------- #
@app.command("list")
def providers_list(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
    check: bool = typer.Option(False, "--check", help="Validate each profile against the provider (network)."),
) -> None:
    """List configured provider profiles and which one is active."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    settings = loaded.settings
    active = settings.provider or "default"
    rows: list[dict[str, Any]] = []

    for profile_name, profile in sorted(settings.providers.items()):
        # Config always carries a stock profile so the schema is inspectable; it is not
        # something the user created, so it is only listed while it is the active one
        # (that is when it explains why nothing works yet).
        if getattr(profile, "is_placeholder", False) and profile_name != active:
            continue
        entry = resolve_entry(profile.type)
        credential = _credential_state(profile)
        rows.append(
            {
                "profile": profile_name,
                "provider": entry.label if entry else _pretty_type(profile.type),
                "type": profile.type,
                "model": profile.model,
                "credential": credential,
                "tools": bool(profile.supports_tools),
                "active": profile_name == active,
            }
        )

    if check:
        for row in rows:
            result = asyncio.run(validate_profile(settings, row["profile"]))
            row["check"] = "ok" if result.ok else "failed"
            row["detail"] = result.message if not result.ok else f"{result.model_count} model(s)"

    if runtime.is_machine or json_output:
        runtime.emit_json({"active": active, "providers": rows, "credential_store": str(_store().path)})
        raise typer.Exit(ExitCode.OK)

    if not rows:
        output.print("[ua.warn]no providers configured[/ua.warn] - run [ua.kv.key]ua setup[/ua.kv.key] to add one")
        raise typer.Exit(ExitCode.OK)

    table = data_table(
        ["", "profile", "provider", "model", "credential", "tools"],
        [
            (
                "✔" if row["active"] else "",
                row["profile"],
                row["provider"],
                row["model"],
                row["credential"],
                "yes" if row["tools"] else "no",
            )
            for row in rows
        ],
        title="providers",
    )
    output.print(table)
    if check:
        for row in rows:
            if row.get("check") == "failed":
                output.print(f"[ua.err]✖ {row['profile']}: {row['detail']}[/ua.err]")
    output.print(f"[ua.muted]credential store: {_store().path}[/ua.muted]")
    raise typer.Exit(ExitCode.OK)


@app.command("add")
def providers_add(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider key from the catalog."),
    name: str | None = typer.Option(None, "--as", help="Profile name (defaults to the provider key)."),
    model: str | None = typer.Option(None, "--model", help="Default model."),
    api_key_env: str | None = typer.Option(None, "--api-key-env", help="Environment variable holding the key."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
) -> None:
    """Add a provider profile without walking the whole flow.

    Examples:
      ua providers add groq --api-key-env GROQ_API_KEY
      ua providers add openai --model gpt-4.1-mini
    """

    runtime: Runtime = ctx.obj
    options = OnboardingOptions(
        provider=provider,
        model=model,
        api_key_env=api_key_env,
        base_url=base_url,
        profile_name=name,
        store=_store(),
    )
    result = asyncio.run(run_onboarding(runtime, options=options, prompter=runtime.prompter))
    if result is None:
        raise typer.Exit(ExitCode.INTERRUPTED)
    if runtime.is_machine:
        runtime.emit_json(result.to_dict())
    raise typer.Exit(ExitCode.OK)


@app.command("use")
def providers_use(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name to make active."),
) -> None:
    """Switch the default provider profile."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    if name not in loaded.settings.providers:
        raise UsageError(
            f"No provider profile named {name!r}.",
            hint=f"Available: {', '.join(sorted(loaded.settings.providers)) or 'none - run `ua setup`'}.",
        )
    loaded.settings.provider = name
    path = runtime.save_settings(loaded.settings)
    if runtime.is_machine:
        runtime.emit_json({"active": name, "config": str(path)})
    else:
        output.print(f"[ua.ok]✔[/ua.ok] active provider is now [ua.kv.key]{name}[/ua.kv.key] ({loaded.settings.providers[name].model})")
    raise typer.Exit(ExitCode.OK)


@app.command("remove")
def providers_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile name to remove."),
    keep_credential: bool = typer.Option(False, "--keep-credential", help="Do not delete the stored API key."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Remove a provider profile (and its stored key, unless --keep-credential)."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    profile = loaded.settings.providers.get(name)
    if profile is None:
        raise UsageError(f"No provider profile named {name!r}.")
    if not yes and not runtime.prompter.confirm(f"Remove provider profile {name!r}?", default=False, dangerous=True):
        raise typer.Exit(ExitCode.INTERRUPTED)

    del loaded.settings.providers[name]
    if loaded.settings.provider == name:
        loaded.settings.provider = next(iter(loaded.settings.providers), "")
    path = runtime.save_settings(loaded.settings)

    removed_key = False
    if not keep_credential and profile.api_key and profile.api_key.startswith("store:"):
        removed_key = _store().delete(profile.api_key.split(":", 1)[1])

    if runtime.is_machine:
        runtime.emit_json({"removed": name, "credential_removed": removed_key, "config": str(path)})
    else:
        output.print(f"[ua.ok]✔[/ua.ok] removed {name}" + (" and its stored key" if removed_key else ""))
        if not loaded.settings.providers:
            output.print("[ua.warn]no providers left - run `ua setup`[/ua.warn]")
    raise typer.Exit(ExitCode.OK)


@app.command("test")
def providers_test(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Profile to test (default: the active one)."),
    model: str | None = typer.Option(None, "--model", help="Check a specific model id instead of the configured one."),
) -> None:
    """Validate a profile end to end: key, endpoint, model and tool calling."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    profile_name = name or loaded.settings.provider or "default"
    result = asyncio.run(validate_profile(loaded.settings, profile_name, model=model))
    tool_report = _tool_check(loaded.settings, profile_name) if result.ok else None

    payload = {
        "provider": profile_name,
        "ok": result.ok and (tool_report is None or tool_report["ok"]),
        "message": result.message,
        "hint": result.hint,
        "model_count": result.model_count,
        "tools": tool_report,
    }
    if runtime.is_machine:
        runtime.emit_json(payload)
    else:
        mark = "[ua.ok]✔[/ua.ok]" if result.ok else "[ua.err]✖[/ua.err]"
        output.print(f"{mark} {result.message}")
        if result.hint:
            output.print(f"   [ua.muted]{result.hint}[/ua.muted]")
        if tool_report:
            mark = "[ua.ok]✔[/ua.ok]" if tool_report["ok"] else "[ua.err]✖[/ua.err]"
            output.print(f"{mark} tool calling: {tool_report['detail']}")
    raise typer.Exit(ExitCode.OK if payload["ok"] else ExitCode.PROVIDER)


@app.command("catalog")
def providers_catalog(
    ctx: typer.Context,
    search: str | None = typer.Option(None, "--search", help="Filter by name."),
    tools_only: bool = typer.Option(False, "--tools-only", help="Only tool-capable providers."),
) -> None:
    """Show every provider the product can actually talk to, with capabilities."""

    runtime: Runtime = ctx.obj
    entries = [entry for entry in CATALOG if not search or search.lower() in entry.key or search.lower() in entry.label.lower()]
    if tools_only:
        entries = [entry for entry in entries if entry.supports_tools]
    if runtime.is_machine:
        runtime.emit_json({"providers": [entry.to_dict() for entry in entries]})
        raise typer.Exit(ExitCode.OK)
    output.print(
        data_table(
            ["provider", "protocol", "tools", "models", "key from", "notes"],
            [
                (
                    entry.label,
                    entry.protocol,
                    "yes" if entry.supports_tools else "no",
                    "live" if entry.model_listing != "none" else "curated",
                    entry.env_var or "-",
                    entry.notes[:48],
                )
                for entry in entries
            ],
            title=f"provider catalog ({len(entries)})",
        )
    )
    raise typer.Exit(ExitCode.OK)


# --------------------------------------------------------------------------- #
# ua models ...
# --------------------------------------------------------------------------- #
models_app = typer.Typer(help="List and select models for the active provider.", no_args_is_help=False)


@models_app.command("list")
def models_list(
    ctx: typer.Context,
    provider: str | None = typer.Option(None, "--provider", help="Query a specific profile."),
    search: str | None = typer.Option(None, "--search", "-s", help="Filter by id or name."),
    tools_only: bool = typer.Option(False, "--tools", help="Only models that support tool calling."),
    limit: int = typer.Option(40, "--limit", help="Rows to show."),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the cache and hit the provider."),
) -> None:
    """List the models the active provider offers (live, cached for speed)."""

    runtime: Runtime = ctx.obj
    profile_name, profile = _active(runtime) if provider is None else (provider, runtime.load().settings.providers.get(provider))
    if profile is None:
        raise UsageError(f"No provider profile named {provider!r}.")

    rows = _models_for(runtime, profile_name, profile, refresh=refresh)
    if search:
        needle = search.lower()
        rows = [row for row in rows if needle in str(row.get("id", "")).lower() or needle in str(row.get("name", "")).lower()]
    if tools_only:
        rows = [row for row in rows if row.get("supports_tools") is not False]

    if runtime.is_machine:
        runtime.emit_json({"provider": profile_name, "count": len(rows), "models": rows})
        raise typer.Exit(ExitCode.OK)

    if not rows:
        output.print("[ua.warn]no models matched[/ua.warn]")
        raise typer.Exit(ExitCode.OK)

    output.print(
        data_table(
            ["model", "context", "tools", "in $/Mtok", "out $/Mtok"],
            [
                (
                    row.get("id", ""),
                    f"{int(row['context_window']) // 1000}k" if row.get("context_window") else "-",
                    {True: "yes", False: "no", None: "?"}[row.get("supports_tools")],
                    f"{row['input_cost_per_mtok']:.3g}" if row.get("input_cost_per_mtok") is not None else "-",
                    f"{row['output_cost_per_mtok']:.3g}" if row.get("output_cost_per_mtok") is not None else "-",
                )
                for row in rows[:limit]
            ],
            title=f"{profile_name} models ({len(rows)})",
        )
    )
    output.print(f"[ua.muted]select with: ua models use <model-id>[/ua.muted]")
    raise typer.Exit(ExitCode.OK)


@models_app.command("use")
def models_use(
    ctx: typer.Context,
    model: str = typer.Argument(..., help="Model id to make default."),
    provider: str | None = typer.Option(None, "--provider", help="Set it on another profile."),
    check: bool = typer.Option(True, "--check/--no-check", help="Verify the provider knows this model."),
    context_window: int | None = typer.Option(None, "--context", help="Override the context window."),
) -> None:
    """Set the default model for a provider profile."""

    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    profile_name = provider or loaded.settings.provider or "default"
    profile = loaded.settings.providers.get(profile_name)
    if profile is None:
        raise UsageError(f"No provider profile named {profile_name!r}.")

    verified = False
    if check:
        result = asyncio.run(validate_profile(loaded.settings, profile_name, model=model))
        if not result.ok:
            raise ConfigError(result.message, hint=result.hint)
        verified = True
        rows = _models_for(runtime, profile_name, profile, refresh=False)
        match = next((row for row in rows if row["id"] == model), None)
        if match and match.get("context_window"):
            profile.context_window = int(match["context_window"])

    profile.model = model
    if context_window:
        profile.context_window = context_window
    path = runtime.save_settings(loaded.settings)

    if runtime.is_machine:
        runtime.emit_json({"provider": profile_name, "model": model, "verified": verified, "config": str(path)})
    else:
        output.print(f"[ua.ok]✔[/ua.ok] {profile_name} now uses [ua.kv.key]{model}[/ua.kv.key]")
    raise typer.Exit(ExitCode.OK)


@models_app.command("refresh")
def models_refresh(ctx: typer.Context, provider: str | None = typer.Option(None, "--provider")) -> None:
    """Refresh the cached model list for a provider."""

    runtime: Runtime = ctx.obj
    profile_name, profile = _active(runtime) if provider is None else (provider, runtime.load().settings.providers.get(provider))
    if profile is None:
        raise UsageError(f"No provider profile named {provider!r}.")
    rows = _models_for(runtime, profile_name, profile, refresh=True)
    if runtime.is_machine:
        runtime.emit_json({"provider": profile_name, "count": len(rows)})
    else:
        output.print(f"[ua.ok]✔[/ua.ok] cached {len(rows)} model(s) for {profile_name}")
    raise typer.Exit(ExitCode.OK)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _pretty_type(provider_type: str) -> str:
    """Human label for a profile that is not one of the catalog vendors."""

    names = {
        "openai-compatible": "OpenAI-compatible (custom)",
        "anthropic": "Anthropic (custom)",
        "ollama": "Ollama (local)",
        "offline": "Offline (no model)",
    }
    return names.get(provider_type, provider_type.replace("-", " ").title())


def _credential_state(profile: Any) -> str:
    ref = getattr(profile, "api_key", None)
    if not ref:
        return "missing" if profile.type != "offline" and getattr(profile, "type", "") != "ollama" else "not needed"
    if ref.startswith("env:"):
        import os

        return f"env:{ref.split(':', 1)[1]}" if os.environ.get(ref.split(":", 1)[1]) else f"env:{ref.split(':', 1)[1]} (unset)"
    if ref.startswith("store:"):
        return "stored" if _store().get(ref.split(":", 1)[1]) else "store (missing)"
    return "set"


def _models_for(runtime: Runtime, profile_name: str, profile: Any, *, refresh: bool) -> list[dict[str, Any]]:
    cache = _read_cache(runtime)
    if not refresh and cache.get(profile_name):
        return list(cache[profile_name])

    entry = resolve_entry(profile.type)
    if entry is None:
        from unknown_agent.providers.discovery import resolve_entry_for

        entry = resolve_entry_for(profile)
    try:
        models = asyncio.run(
            fetch_models(entry, api_key=_api_key_for(profile), base_url=profile.base_url or entry.base_url)
        )
    except ProviderProbeError as exc:
        if cache.get(profile_name):
            output.diag_print(f"[ua.warn]{exc.message} - showing the cached list[/ua.warn]")
            return list(cache[profile_name])
        raise ConfigError(exc.message, hint=exc.hint) from exc
    payload = [model.to_dict() for model in models]
    _write_cache(runtime, profile_name, payload)
    return payload


def _tool_check(settings: Any, profile_name: str) -> dict[str, Any]:
    from unknown_agent.providers.registry import build_provider
    from unknown_agent.providers.toolkit import check_tool_roundtrip

    try:
        provider = build_provider(settings.providers[profile_name], name=profile_name)
    except Exception as exc:  # noqa: BLE001 - report, never crash the CLI
        return {"ok": False, "detail": f"adapter could not be built: {exc}"}
    report = check_tool_roundtrip(provider)
    return {"ok": report.ok, "detail": "round-trip verified" if report.ok else report.render()}


@app.command("detect", hidden=True)
def providers_detect(ctx: typer.Context) -> None:
    """Internal: report readiness (used by the shell and by tests)."""

    runtime: Runtime = ctx.obj
    state = detect_setup_state(runtime.load().settings)
    if runtime.is_machine:
        runtime.emit_json(state.to_dict())
    else:
        output.print(kv_table([(key, value) for key, value in state.to_dict().items() if value not in ("", [], None)], title="setup state"))
    raise typer.Exit(ExitCode.OK if state.ready else ExitCode.CONFIG)


__all__ = ["app", "models_app", "providers_catalog", "catalog_payload"]
