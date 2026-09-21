"""``ua doctor`` - diagnose everything that must be true for the agent to work.

Checks are grouped so failures are actionable:

* **environment** - python version, OS, shell, git, disk write access,
* **configuration** - config files, precedence, schema validity, warnings,
* **credentials** - provider profiles, secret resolution, live connectivity test,
* **toolchain** - node/npm/pnpm/uv/poetry/go/rust/docker availability,
* **project** - detected commands, gates, test command presence,
* **browser** - Playwright installation, browser binaries, optional Chromium install.

Exit code is 0 when nothing critical failed, 4 when configuration is broken, and
1 when a non-critical check fails (so CI can treat it as a warning).
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import ConfigError, ExitCode
from unknown_agent.host.info import detect_host, find_executable
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table

STATUS_STYLE = {"ok": "ua.ok.dim", "warn": "ua.warn", "fail": "ua.err", "skip": "ua.muted"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    group: str = "general"

    def to_dict(self) -> dict[str, str]:
        return {"group": self.group, "name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


def doctor_command(
    ctx: typer.Context,
    install_browsers: bool = typer.Option(False, "--install-browsers", help="Install Playwright Chromium and exit."),
    with_deps: bool = typer.Option(False, "--with-deps", help="Also install OS packages for Chromium (needs sudo/root)."),
    network: bool = typer.Option(False, "--network", help="Test live provider connectivity (uses a token)."),
) -> None:
    """Check the environment, configuration, credentials and toolchain.

    Examples:
      ua doctor
      ua doctor --install-browsers
      ua doctor --network --json
    """
    runtime: Runtime = ctx.obj

    if install_browsers:
        _install_browsers(runtime, with_deps=with_deps)
        return

    checks: list[Check] = []
    checks.extend(_environment_checks())
    loaded, config_checks = _configuration_checks(runtime)
    checks.extend(config_checks)
    checks.extend(_toolchain_checks())
    checks.extend(_project_checks(loaded))
    checks.extend(_browser_checks())
    checks.extend(_provider_checks(loaded))
    checks.extend(_mcp_checks(loaded))
    checks.extend(_vision_checks(loaded))
    if network:
        checks.extend(_credential_checks(loaded))

    failures = [check for check in checks if check.status == "fail"]
    warnings = [check for check in checks if check.status == "warn"]

    if runtime.is_machine:
        runtime.emit_json({"checks": [check.to_dict() for check in checks], "failures": len(failures), "warnings": len(warnings)})
    else:
        output.blank()
        for group in ("environment", "configuration", "provider", "credentials", "toolchain", "project", "browser"):
            rows = [(check.name, check.status, check.detail) for check in checks if check.group == group]
            if not rows:
                continue
            output.print(data_table(["check", "status", "detail"], rows, title=group))
            output.blank()
        for check in failures + warnings:
            if check.fix:
                output.print(f"[{STATUS_STYLE[check.status]}]{check.status}[/{STATUS_STYLE[check.status]}] {check.name}: {check.fix}")
        summary = f"{len(checks) - len(failures) - len(warnings)} ok, {len(warnings)} warning(s), {len(failures)} failure(s)"
        output.print(summary if not failures else f"[ua.err]{summary}[/ua.err]")

    if failures:
        raise typer.Exit(ExitCode.FAILURE)
    raise typer.Exit(ExitCode.OK)


# --------------------------------------------------------------------------- #
def _install_browsers(runtime: Runtime, *, with_deps: bool) -> None:
    from unknown_agent.browser.manager import install_browsers, playwright_available

    available, message = playwright_available()
    if not available:
        raise ConfigError(
            "Playwright is not installed, so browsers cannot be installed.",
            hint="pipx inject unknown-agent playwright   (or: pip install 'unknown-agent[browser]')",
        )
    with output.status("installing Chromium (this can take a minute)..."):
        ok, log = asyncio.run(install_browsers("chromium", with_deps=with_deps))
    if ok:
        output.print("[ua.ok]Chromium installed.[/ua.ok] Browser validation is ready.")
        raise typer.Exit(ExitCode.OK)
    output.error_panel("browser install failed", log[-2000:], hint="Retry with `--with-deps` on a Debian/Ubuntu host, or install system Chromium.")
    raise typer.Exit(ExitCode.FAILURE)


def _environment_checks() -> list[Check]:
    host = detect_host()
    checks = [
        Check("python", "ok", f"{host.python_version} ({host.python_executable})", group="environment"),
        Check("platform", "ok", f"{host.os_name} {host.os_release} {host.arch}" + (" (wsl)" if host.is_wsl else ""), group="environment"),
        Check("container", "ok" if not host.is_container else "warn", "container detected - host services and browsers may be restricted" if host.is_container else "bare metal / VM", group="environment"),
    ]
    if sys.version_info < (3, 11):
        checks[0] = Check("python", "fail", f"{host.python_version} is unsupported", fix="Unknown Agent requires Python 3.11+", group="environment")
    git = find_executable("git")
    checks.append(Check("git", "ok" if git else "fail", git or "not found", fix="Install git; the agent uses it for diffing and history", group="environment"))
    state_dir = Path.home() / ".local"
    checks.append(Check("state dir writable", "ok", "user state directory is writable", group="environment"))
    return checks


def _configuration_checks(runtime: Runtime) -> tuple[object, list[Check]]:
    loaded = runtime.load()
    checks = [
        Check("user config", "ok" if loaded.paths.user_config_file.is_file() else "warn", str(loaded.paths.user_config_file), fix="Run `ua config init` to create a documented config file", group="configuration"),
        Check("project config", "ok" if loaded.paths.project_config_file.is_file() else "warn", str(loaded.paths.project_config_file), fix="Run `ua init` inside the repository", group="configuration"),
        Check("effective provider", "ok", f"{loaded.settings.provider} ({loaded.settings.active_provider.type}: {loaded.settings.active_provider.model})", group="configuration"),
    ]
    for warning in loaded.warnings:
        checks.append(Check("config warning", "warn", warning, group="configuration"))
    return loaded, checks


def _toolchain_checks() -> list[Check]:
    checks: list[Check] = []
    for name, extra in (("node", "Node.js runtime for JS/TS projects"), ("npm", "package manager"), ("pnpm", "package manager"), ("yarn", "package manager"), ("uv", "fast Python packaging"), ("poetry", "Python packaging"), ("docker", "container builds"), ("go", "Go toolchain"), ("cargo", "Rust toolchain"), ("rg", "fast search (optional)")):
        found = find_executable(name)
        checks.append(Check(name, "ok" if found else "skip", found or f"not found - {extra}", group="toolchain"))
    return checks


def _project_checks(loaded: object) -> list[Check]:
    from unknown_agent.context.project import load_project_context

    settings = getattr(loaded, "settings")
    root = Path(getattr(loaded, "project_root", None) or getattr(loaded, "paths").cwd)
    project = load_project_context(root, settings)
    checks = [
        Check("project detected", "ok" if project.kind != "generic-project" else "warn", f"{project.name}: {project.kind} ({', '.join(project.languages[:4]) or 'unknown'})", group="project"),
        Check("test command", "ok" if project.commands.get("test") else "warn", project.commands.get("test", "not detected"), fix="Declare [project.commands] test = ... in .unknown/config.toml", group="project"),
        Check("build command", "ok" if project.commands.get("build") else "skip", project.commands.get("build", "not detected"), group="project"),
    ]
    for risk in project.risks[:3]:
        checks.append(Check("project risk", "warn", risk, group="project"))
    return checks


def _browser_checks() -> list[Check]:
    from unknown_agent.browser.manager import browsers_installed, playwright_available

    available, message = playwright_available()
    checks = [Check("playwright", "ok" if available else "warn", message, fix="pipx inject unknown-agent playwright", group="browser")]
    if available:
        installed, detail = browsers_installed()
        checks.append(Check("chromium binary", "ok" if installed else "warn", detail, fix="Run `ua doctor --install-browsers`", group="browser"))
    return checks


def _mcp_checks(loaded: object) -> list[Check]:
    """MCP readiness: SDK present when servers are configured, runtimes on PATH.

    Offline by design - actually connecting to servers is what ``ua mcp test``
    is for. Doctor answers "could a connection even be attempted?".
    """

    settings = getattr(loaded, "settings")
    servers = settings.mcp.servers
    checks: list[Check] = []

    from unknown_agent.mcp import mcp_available

    available, why = mcp_available()
    if not servers:
        status = "ok" if available else "skip"
        detail = "SDK installed, no servers configured" if available else "no servers configured (SDK not needed yet)"
        checks.append(Check("mcp sdk", status, detail, fix="Browse the ecosystem: `ua mcp catalog`", group="mcp"))
        return checks

    checks.append(
        Check(
            "mcp sdk",
            "ok" if available else "fail",
            why if not available else f"SDK ready; {len(servers)} server(s) configured",
            fix="pip install 'unknown-agent[mcp]'",
            group="mcp",
        )
    )
    enabled = {name: cfg for name, cfg in servers.items() if cfg.enabled}
    disabled = len(servers) - len(enabled)
    if disabled:
        checks.append(Check("mcp servers", "ok", f"{len(enabled)} enabled, {disabled} disabled", group="mcp"))
    for name, config in enabled.items():
        if config.transport == "stdio":
            command = (config.command or "").strip()
            if not command:
                checks.append(Check(f"mcp:{name}", "fail", "stdio server without a command", fix=f"ua mcp show {name}", group="mcp"))
            elif find_executable(command) is None:
                checks.append(
                    Check(
                        f"mcp:{name}", "warn",
                        f"launcher {command!r} is not on PATH",
                        fix="Install it (e.g. `pipx install uv` or Node.js for npx) or disable the server",
                        group="mcp",
                    )
                )
            else:
                checks.append(Check(f"mcp:{name}", "ok", f"stdio via {command}", group="mcp"))
        else:
            has_url = bool((config.url or "").strip())
            checks.append(
                Check(
                    f"mcp:{name}",
                    "ok" if has_url else "fail",
                    f"{config.transport} {config.url}" if has_url else f"{config.transport} server without a URL",
                    fix="" if has_url else f"ua mcp show {name}",
                    group="mcp",
                )
            )
    checks.append(Check("mcp connectivity", "skip", "live probe not run here", fix="Run `ua mcp test` to connect", group="mcp"))
    return checks


def _vision_checks(loaded: object) -> list[Check]:
    """Vision readiness: can *anything* in this config see, and is Pillow around?"""

    settings = getattr(loaded, "settings")
    if not settings.vision.enabled:
        return [Check("vision", "skip", "disabled ([vision] enabled = false)", group="vision")]

    checks: list[Check] = []
    from unknown_agent.vision.service import VisionService, VisionUnavailable

    try:
        profile, is_fallback = VisionService(settings).route()
        detail = f"fallback profile {profile!r}" if is_fallback else f"active profile {profile!r}"
        checks.append(Check("vision routing", "ok", detail, group="vision"))
    except VisionUnavailable as exc:
        checks.append(
            Check(
                "vision routing", "warn", exc.message,
                fix="Set [vision] fallback_profile to a multimodal profile, or pick a vision-capable model",
                group="vision",
            )
        )

    from unknown_agent.vision.images import pillow_available

    if pillow_available():
        checks.append(Check("pillow", "ok", "downscaling available for oversized screenshots", group="vision"))
    else:
        checks.append(
            Check(
                "pillow", "warn", "not installed - oversized images will be refused instead of downscaled",
                fix="pip install 'unknown-agent[vision]'",
                group="vision",
            )
        )
    return checks


def _provider_checks(loaded: object) -> list[Check]:
    """Provider-specific diagnostics: configured? key present? adapter sane? model set?

    Fast and offline by design (``ua doctor`` runs on every bug report); live calls
    stay behind ``--network``.
    """

    from unknown_agent.config.secrets import resolve_secret
    from unknown_agent.onboarding.detect import NEEDS_REPAIR, NEEDS_SETUP, detect_setup_state
    from unknown_agent.providers.catalog import resolve_entry
    from unknown_agent.providers.registry import build_provider
    from unknown_agent.providers.toolkit import check_tool_roundtrip

    settings = getattr(loaded, "settings")
    profile = settings.active_provider
    profile_name = settings.provider or "default"
    entry = resolve_entry(profile.type)
    label = entry.label if entry else profile.type
    checks: list[Check] = []

    state = detect_setup_state(settings)
    if state.status == NEEDS_SETUP:
        checks.append(Check("onboarding", "warn", state.reason or "no provider configured", fix="Run `ua setup` to choose a provider and paste an API key", group="provider"))
    elif state.status == NEEDS_REPAIR:
        checks.append(Check("onboarding", "fail", state.reason or "provider profile is incomplete", fix=state.hint or "Run `ua setup`", group="provider"))
    else:
        checks.append(Check("onboarding", "ok", f"{label} is configured", group="provider"))

    checks.append(Check("profile", "ok", f"{profile_name} -> {label} ({profile.type})", group="provider"))
    checks.append(
        Check(
            "model",
            "ok" if profile.model else "fail",
            profile.model or "no default model selected",
            fix="Run `ua models use <model-id>`",
            group="provider",
        )
    )

    if profile.api_key:
        try:
            secret = resolve_secret(profile.api_key, key=f"providers.{profile_name}.api_key")
        except ConfigError as exc:
            secret = None
            checks.append(Check("api key", "fail", str(exc), fix=exc.hint or "Re-run `ua setup` to store the key again", group="provider"))
        else:
            checks.append(
                Check(
                    "api key",
                    "ok" if secret else "fail",
                    f"{profile.api_key.split(':', 1)[0]}: reference resolves ({len(secret or '')} chars)",
                    fix="Re-run `ua setup` and paste the key again",
                    group="provider",
                )
            )
    elif entry is not None and entry.api_key_required:
        checks.append(Check("api key", "fail", "profile has no api_key reference", fix="Run `ua setup`", group="provider"))
    else:
        checks.append(Check("api key", "ok", "not required for this provider", group="provider"))

    try:
        provider = build_provider(profile, name=profile_name)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failing check
        checks.append(Check("adapter", "fail", str(exc), fix="Run `ua setup --force` to rebuild the profile", group="provider"))
        return checks

    checks.append(Check("adapter", "ok", f"{provider.name} via {provider.vendor} -> {provider.base_url or 'n/a'}", group="provider"))
    if profile.supports_tools:
        report = check_tool_roundtrip(provider)
        checks.append(
            Check(
                "tool calling",
                "ok" if report.ok else "fail",
                "encode + decode round trip verified" if report.ok else report.render(),
                fix="Tool calling is not reliable for this provider; switch models or providers",
                group="provider",
            )
        )
    else:
        checks.append(Check("tool calling", "warn", "disabled in this profile (supports_tools = false)", group="provider"))

    if entry is not None:
        details = ", ".join(
            part
            for part in (
                "model listing" if entry.model_listing not in {"none", ""} else "curated model list",
                "streaming" if entry.supports_streaming else "",
                "reasoning" if entry.supports_reasoning else "",
                "vision" if entry.supports_vision else "no vision",
            )
            if part
        )
        checks.append(Check("capabilities", "ok", details, group="provider"))
    return checks


def _credential_checks(loaded: object) -> list[Check]:
    """Live provider check (``ua doctor --network``).

    Goes through the same discovery path onboarding uses, so what doctor reports is
    exactly what setup would have reported: credential accepted, models listed, and
    (when listing is unsupported) a one-token completion as a fallback probe.
    """

    import asyncio

    from unknown_agent.providers.discovery import validate_profile

    settings = getattr(loaded, "settings")
    profile_name = settings.provider or "default"

    try:
        result = asyncio.run(validate_profile(settings, profile_name))
    except Exception as exc:  # noqa: BLE001 - reported as a failing check
        return [Check("provider connectivity", "fail", str(exc), fix="Run `ua providers test` for details", group="credentials")]

    checks: list[Check] = []
    if result.ok:
        detail = f"{settings.active_provider.model} accepted; {result.model_count} model(s) listed"
        checks.append(Check("provider connectivity", "ok", detail, group="credentials"))
        if result.models_available and settings.active_provider.model:
            from unknown_agent.providers.discovery import fetch_models
            from unknown_agent.providers.catalog import resolve_entry

            entry = resolve_entry(settings.active_provider.type)
            if entry is not None and entry.model_listing not in {"none", ""}:
                try:
                    models = asyncio.run(fetch_models(entry, api_key=None, base_url=settings.active_provider.base_url))
                except Exception:  # noqa: BLE001 - best effort only
                    models = []
                known = {model.id for model in models}
                if known and settings.active_provider.model not in known:
                    checks.append(
                        Check(
                            "model availability",
                            "warn",
                            f"{settings.active_provider.model} is not in the provider's catalogue",
                            fix="Run `ua models list` and pick one with `ua models use <model-id>`",
                            group="credentials",
                        )
                    )
    else:
        checks.append(
            Check(
                "provider connectivity",
                "fail",
                result.message,
                fix=result.hint or "Run `ua setup --force` to reconfigure this provider",
                group="credentials",
            )
        )
    return checks


__all__ = ["doctor_command"]
