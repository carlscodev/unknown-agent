"""``ua init``, ``version``, ``log``, ``completion``, ``upgrade``, ``tui``.

``init`` is the onboarding path: it detects the stack, writes a project config
with the detected commands, creates the ``.unknown`` layout, optionally adds
guidance files and flows, and prints exactly what it did and what to do next.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.config.loader import write_default_config
from unknown_agent.context.project import load_project_context
from unknown_agent.errors import ExitCode, UsageError
from unknown_agent.host.info import detect_host, is_ci
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table, summary_panel
from unknown_agent.version import __version__


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def init_command(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite existing project config."),
    guidance: bool = typer.Option(True, "--guidance/--no-guidance", help="Create .unknown/guidance.md for repo-specific rules."),
    ci: bool = typer.Option(False, "--ci", help="Also generate a GitHub Actions workflow that runs `ua verify`."),
    flow: bool = typer.Option(False, "--flow", help="Scaffold a browser smoke flow."),
    url: str | None = typer.Option(None, "--url", help="Dev app URL for browser validation."),
) -> None:
    """Set up Unknown Agent in this repository (creates .unknown/).

    Examples:
      ua init
      ua init --url http://localhost:3000 --flow --ci
    """
    runtime: Runtime = ctx.obj
    loaded = runtime.load()
    root = Path(loaded.project_root or loaded.paths.cwd)
    settings = loaded.settings
    project = load_project_context(root, settings)

    created: list[str] = []
    loaded.paths.ensure_project_dirs()
    created.append(str(loaded.paths.project_dir))

    # project config with detected values
    config_path = loaded.paths.project_config_file
    if config_path.exists() and not force:
        output.diag_print(f"[ua.muted]keeping existing {config_path}[/ua.muted]")
    else:
        commands = dict(project.commands or {})
        if url:
            commands.setdefault("dev", "")
        lines = [
            "# Project configuration for Unknown Agent.",
            "# User-level settings (providers, credentials) stay in ~/.config/unknown-agent/config.toml.",
            "",
            "[project]",
            f'name = "{project.name}"',
            f'kind = "{project.kind}"',
            f"stack = [{', '.join(f'\"{item}\"' for item in project.frameworks[:8])}]",
        ]
        if project.package_manager:
            lines.append(f'package_manager = "{project.package_manager}"')
        if url or project.dev_url:
            lines.append(f'dev_url = "{url or project.dev_url}"')
        lines.append("")
        lines.append("# Files or globs the agent must never modify:")
        lines.append("protected_paths = []")
        if commands:
            lines.append("")
            lines.append("[project.commands]")
            for name, command in sorted(commands.items()):
                if command:
                    lines.append(f'{name} = "{command}"')
        if project.frameworks:
            lines.append("")
            lines.append("# Detected stack notes (edit freely):")
            for note in project.notes:
                lines.append(f"# {note}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        created.append(str(config_path))

    if guidance:
        guidance_path = loaded.paths.project_dir / "guidance.md"
        if not guidance_path.exists():
            guidance_path.write_text(
                f"""# {project.name}: repository guidance

Unknown Agent loads this file into every prompt for this repository. Keep it short and specific.
Delete anything that does not apply - or delete the file entirely.

## Conventions
- (e.g. "All API handlers return JSONResponse and live in app/api/")
- (e.g. "Tests use pytest fixtures in tests/conftest.py; never hit the network")

## Commands
{chr(10).join(f'- {name}: `{command}`' for name, command in sorted((project.commands or {}).items())) or '- (declare test/build/lint commands in .unknown/config.toml)'}

## Verification expectations
- (e.g. "Run `pytest -q` and `ruff check .` before declaring work complete")
- (e.g. "For UI changes, verify with `ua browser flow`")

## Hard rules
- (e.g. "Never edit generated clients under gen/")
- (e.g. "Never commit .env files")
""",
                encoding="utf-8",
            )
            created.append(str(guidance_path))

    if flow and (url or project.dev_url):
        from unknown_agent.browser.flows import smoke_steps, write_flow

        flow_path = write_flow(root, "smoke", smoke_steps(url or project.dev_url or "/"), url=url or project.dev_url)
        created.append(str(flow_path))

    if ci:
        workflow = root / ".github" / "workflows" / "unknown-agent.yml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(
            """name: unknown-agent

on:
  pull_request:
  workflow_dispatch:

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pipx install unknown-agent
      - run: pipx inject unknown-agent playwright
      - run: ua doctor --install-browsers
      - run: ua verify --json | tee verification.json
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: ua-verification
          path: |
            verification.json
            .unknown/artifacts
""",
            encoding="utf-8",
        )
        created.append(str(workflow))

    if runtime.is_machine:
        runtime.emit_json({"created": created, "project": project.to_dict()})
        return

    output.blank()
    output.print(summary_panel("initialised", [("project", f"{project.name} ({project.kind})"), ("root", root)]))
    output.blank()
    output.print("[ua.muted]created[/ua.muted]")
    for path in created:
        output.print(f"  + {path}")
    if project.commands:
        output.blank()
        output.print(data_table(["detected command", "invocation"], sorted((project.commands or {}).items()), title="commands"))
    output.blank()
    next_steps = ["ua doctor                 # verify environment and credentials"]
    if not loaded.paths.user_config_file.exists():
        next_steps.insert(0, "ua config init            # create your user config with an API key reference")
    next_steps.append('ua inspect                # see what the agent knows about this repo')
    next_steps.append('ua plan "your first task" # review a plan before executing')
    output.print("[ua.muted]next[/ua.muted]")
    for step in next_steps:
        output.print(f"  {step}")


# --------------------------------------------------------------------------- #
# version
# --------------------------------------------------------------------------- #
def version_command(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Include environment fingerprint."),
) -> None:
    """Print version and build information."""

    runtime: Runtime = ctx.obj
    host = detect_host()
    payload = {
        "version": __version__,
        "python": host.python_version,
        "executable": sys.executable,
        "platform": f"{host.os_name} {host.os_release} {host.arch}",
        "install_mode": _install_mode(),
        "config_dir": str(runtime.paths.config_dir),
        "state_dir": str(runtime.paths.state_dir),
    }
    if runtime.is_machine:
        runtime.emit_json(payload)
        return
    if verbose:
        from unknown_agent.providers.registry import available_providers

        payload["provider_types"] = available_providers()
        payload["ci"] = is_ci()
        output.print(kv_table(sorted(payload.items()), title=f"unknown-agent {__version__}"))
    else:
        output.raw(f"unknown-agent {__version__}")


def _install_mode() -> str:
    from unknown_agent.host.install import detect_install

    return detect_install().mode


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #
def log_command(
    ctx: typer.Context,
    lines: int = typer.Option(50, "--lines", "-n", help="Trailing lines to print."),
    level: str | None = typer.Option(None, "--level", help="Filter by level (debug|info|warning|error)."),
    path_only: bool = typer.Option(False, "--path", help="Print the log file path only."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow the log (like tail -f)."),
) -> None:
    """Read Unknown Agent's own log file."""

    runtime: Runtime = ctx.obj
    log_path = runtime.paths.log_file
    if path_only:
        output.raw(str(log_path))
        return
    if not log_path.is_file():
        output.print(f"[ua.muted]no log file yet at {log_path}[/ua.muted]")
        return

    def render() -> bool:
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = content[-lines:]
        if level:
            selected = [line for line in selected if f'"level": "{level}"' in line or level.upper() in line]
        for line in selected:
            output.print(line)
        return True

    if follow:
        import time

        output.print(f"[ua.muted]following {log_path} (Ctrl-C to stop)[/ua.muted]")
        position = log_path.stat().st_size
        try:
            while True:
                size = log_path.stat().st_size
                if size < position:  # rotated
                    position = 0
                if size > position:
                    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(position)
                        for line in handle:
                            output.print(line.rstrip())
                        position = handle.tell()
                time.sleep(0.5)
        except KeyboardInterrupt:
            raise typer.Exit(ExitCode.INTERRUPTED) from None
    render()


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #
def completion_command(
    ctx: typer.Context,
    shell: str = typer.Argument(..., help="bash | zsh | fish | powershell"),
    install: bool = typer.Option(False, "--install", help="Print install instructions instead of the script."),
) -> None:
    """Emit shell completion for the ``ua`` command.

    Examples:
      ua completion bash > ~/.local/share/bash-completion/completions/ua
      ua completion zsh --install
    """
    shell = shell.lower()
    scripts = {
        "bash": 'eval "$(_UA_COMPLETE=bash_source ua)"',
        "zsh": 'eval "$(_UA_COMPLETE=zsh_source ua)"',
        "fish": "_UA_COMPLETE=fish_source ua | source",
        "powershell": "$env:_UA_COMPLETE='powershell_source'; ua",
    }
    if shell not in scripts:
        raise UsageError(f"Unsupported shell {shell!r}.", hint="Use bash, zsh, fish or powershell.")
    if install:
        target = {
            "bash": "~/.local/share/bash-completion/completions/ua",
            "zsh": "~/.zfunc/_ua",
            "fish": "~/.config/fish/completions/ua.fish",
            "powershell": "$PROFILE",
        }[shell]
        output.print(f"Add this to {target}:\n\n  {scripts[shell]}\n")
        return
    output.print(scripts[shell], force=True)


# --------------------------------------------------------------------------- #
# upgrade
# --------------------------------------------------------------------------- #
def upgrade_command(
    ctx: typer.Context,
    check: bool = typer.Option(False, "--check", help="Only check whether a newer release exists."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Check for and perform an upgrade through the original installer."""

    runtime: Runtime = ctx.obj
    mode = _install_mode()
    latest = _latest_version()
    payload = {"installed": __version__, "latest": latest, "install_mode": mode, "outdated": bool(latest and latest != __version__)}

    if check or runtime.is_machine:
        if runtime.is_machine:
            runtime.emit_json(payload)
            return
        if latest is None:
            output.print("[ua.warn]could not reach PyPI to check for updates[/ua.warn]")
            return
        if latest == __version__:
            output.print(f"[ua.ok]up to date[/ua.ok] ({__version__})")
        else:
            output.print(f"[ua.warn]update available[/ua.warn] {__version__} -> {latest}")
            output.print(f"  run: ua upgrade    (installed via {mode})")
        return

    from unknown_agent.host.install import detect_install, upgrade_command_for

    command = upgrade_command_for(detect_install())
    if not command:
        raise UsageError(f"Cannot determine how to upgrade a {mode} installation.", hint="Run: pip install --upgrade unknown-agent")
    if not yes and not runtime.assume_yes:
        if not runtime.prompter.confirm(f"Upgrade unknown-agent via {' '.join(command)}?", default=True):
            output.print("[ua.muted]cancelled[/ua.muted]")
            return
    if shutil.which(command[0]) is None and command[0] != sys.executable:
        raise UsageError(f"{command[0]} is not available.", hint=f"Run manually: {' '.join(command)}")
    output.print(f"[ua.muted]running:[/ua.muted] {' '.join(command)}")
    result = subprocess.run(command, check=False)  # noqa: S603 - fixed argv, user-confirmed
    if result.returncode == 0:
        output.print("[ua.ok]upgrade complete[/ua.ok]")
    else:
        output.error_panel("upgrade failed", f"exit code {result.returncode}", hint="Check permissions, or upgrade with your package manager.")
        raise typer.Exit(ExitCode.FAILURE)


def _latest_version() -> str | None:
    import json as _json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen("https://pypi.org/pypi/unknown-agent/json", timeout=6) as response:  # noqa: S310
            payload = _json.load(response)
        return str(payload["info"]["version"])
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# tui (phase two surface, honest stub)
# --------------------------------------------------------------------------- #
def tui_command(ctx: typer.Context) -> None:
    """Launch the interactive TUI shell.

    The shell starts from local configuration only.  It is deliberately *not* gated on
    provider health: a missing key, an expired subscription, an exhausted account
    (HTTP 402) or a dead network all resolve into status inside the running UI, where
    the user can switch provider, switch model, reconfigure or retry.  Refusing to
    launch because a remote account cannot pay is the bug this replaced.
    """

    import asyncio

    runtime = ctx.obj
    settings = None
    try:
        settings = runtime.load().settings if runtime is not None else None
    except Exception as exc:  # noqa: BLE001 - even a broken config still gets a UI
        output.print(f"[ua.warn]config could not be loaded ({exc}); starting with defaults[/ua.warn]")

    try:
        from unknown_agent.tui.shell_app import run_app_shell
    except ImportError:
        output.print("[ua.warn]The TUI needs Textual.[/ua.warn]")
        output.print("  pipx inject unknown-agent textual")
        raise typer.Exit(ExitCode.USAGE) from None

    first_task = " ".join(ctx.args) if getattr(ctx, "args", None) else ""
    asyncio.run(run_app_shell(settings, first_task=first_task))
    raise typer.Exit(ExitCode.OK)


__all__ = [
    "init_command",
    "version_command",
    "log_command",
    "completion_command",
    "upgrade_command",
    "tui_command",
]
