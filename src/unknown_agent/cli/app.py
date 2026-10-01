"""The ``ua`` command line application.

Design rules the CLI follows:

* **Human-first, machine-capable.**  Every command renders for a person by
  default and accepts ``--json``/``--ndjson`` for scripts and CI.
* **Predictable structure.**  ``ua <group> <verb>`` - verbs are the same words
  everywhere (list/show/add/remove), so nothing has to be memorised twice.
* **Honest exit codes.**  A documented :class:`~unknown_agent.errors.ExitCode`
  for every failure class; shell scripts can branch on them.
* **Never a raw traceback.**  Errors print a structured panel with a hint;
  ``--debug`` shows the traceback on request.
* **Safe by default.**  Destructive operations confirm; ``--yes`` is explicit;
  non-interactive sessions take the documented safe default.
* **Fast startup.**  Heavy imports (providers, browser, engines) happen inside
  commands, so ``ua --help`` and ``ua version`` stay snappy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from unknown_agent.errors import ExitCode, UnknownAgentError, UsageError
from unknown_agent.ui.console import configure_output, output
from unknown_agent.ui.errors import render_error, render_warnings
from unknown_agent.ui.renderers import banner
from unknown_agent.version import __version__

APP_NAME = "ua"
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 110}

#: Value-less global flags that are safe to hoist from after a subcommand to the
#: front, so both `ua --json verify` and `ua verify --json` work.  Only flags with
#: no argument are hoisted: hoisting a flag that consumes a value could swallow a
#: task string, which is exactly the kind of cleverness that breaks tools.
HOISTABLE_FLAGS = frozenset(
    {
        "--json",
        "--ndjson",
        "--quiet",
        "-q",
        "--debug",
        "--color",
        "--no-color",
        "--ascii",
        "--no-animations",
        "--yes",
        "-y",
        "--non-interactive",
        "--read-only",
        "--dry-run",
        # Root-only options: unlike --model/-m they cannot belong to a subcommand, so
        # hoisting them is unambiguous.
        "--cwd",
        "--project",
        "--config",
    }
)

HELP_EPILOG = """
Examples:
  ua init                                   set up .unknown/ in this repository
  ua inspect                                show stack, commands, risks, layout
  ua plan "add dark mode with persistence"   produce a reviewable plan
  ua run "add Stripe checkout + tests"       execute, verify, self-heal
  ua fix "checkout 500s on empty cart"       diagnose, patch, verify
  ua verify --gate tests --json              run gates, machine-readable
  ua browser flow --steps-file flow.yaml     replay a browser journey
  ua doctor --install-browsers               install Chromium for validation

Docs:   https://github.com/unknown-agent/unknown-agent/tree/main/docs
Config: ~/.config/unknown-agent/config.toml (user) + <repo>/.unknown/config.toml (project)
"""

app = typer.Typer(
    name=APP_NAME,
    help="Unknown Agent - a Python-first hyperagent for building, fixing and shipping real software.",
    epilog=HELP_EPILOG,
    context_settings=CONTEXT_SETTINGS,
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
    pretty_exceptions_enable=False,
)


# --------------------------------------------------------------------------- #
# global callback
# --------------------------------------------------------------------------- #
@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Print the version and exit.", is_eager=True),
    resume: str | None = typer.Option(None, "--resume", help="Resume a TUI session by id (shorthand for 'ua tui --resume <id>').", show_default=False),
    config: Path | None = typer.Option(None, "--config", help="Use this config file instead of the user config.", show_default=False),
    cwd: Path | None = typer.Option(None, "--cwd", help="Run as if started in this directory.", show_default=False),
    project: Path | None = typer.Option(None, "--project", help="Project root (defaults to the nearest .unknown/ or .git/).", show_default=False),
    mode: str | None = typer.Option(None, "--mode", help="Agent mode: auto | plan | react.", show_default=False),
    model: str | None = typer.Option(None, "--model", help="Override the model for this invocation.", show_default=False),
    provider: str | None = typer.Option(None, "--provider", help="Use a named provider profile (or 'offline' for no API key).", show_default=False),
    max_steps: int | None = typer.Option(None, "--max-steps", help="Hard limit on agent steps for this invocation.", show_default=False),
    dod: str | None = typer.Option(None, "--dod", "--definition-of-done", help="strict | balanced | advisory.", show_default=False),
    read_only: bool = typer.Option(False, "--read-only", help="Forbid every mutating tool (plan/ask sessions)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate writes and mutations instead of applying them."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Assume 'yes' for confirmations (destructive actions included)."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Never prompt; use documented defaults (CI mode)."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON on stdout."),
    ndjson: bool = typer.Option(False, "--ndjson", help="Newline-delimited JSON events (streaming)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress non-essential output."),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity (-v, -vv)."),
    debug: bool = typer.Option(False, "--debug", help="Show tracebacks and debug logs."),
    color: bool = typer.Option(False, "--color", help="Force colour even when piped."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable colour."),
    no_animations: bool = typer.Option(False, "--no-animations", help="Disable spinners and live updates."),
    no_unicode: bool = typer.Option(False, "--ascii", help="ASCII-only output (no box drawing or emoji)."),
) -> None:
    """Unknown Agent - global options apply to every command."""

    from unknown_agent.cli.runtime import Runtime

    if version:
        typer.echo(f"unknown-agent {__version__} ({sys.version.split()[0]}, {sys.platform})")
        raise typer.Exit(ExitCode.OK)

    output_mode = "ndjson" if ndjson else ("json" if json_output else "human")
    force_color = True if color else (False if no_color else None)
    configure_output(
        mode=output_mode,
        quiet=quiet,
        verbose=verbose + (1 if debug else 0),
        unicode_ok=not no_unicode,
        animations=not no_animations,
        force_color=force_color,
    )

    runtime = Runtime(
        config_file=config,
        cwd=cwd or Path.cwd(),
        project=project,
        mode=mode,
        model=model,
        provider=provider,
        max_steps=max_steps,
        definition_of_done=dod,
        read_only=read_only,
        dry_run=dry_run,
        assume_yes=yes,
        non_interactive=non_interactive or json_output or ndjson,
        debug=debug,
        verbosity=verbose,
        force_color=force_color,
        ascii_only=no_unicode,
        animations=(False if no_animations else None),
    )
    ctx.obj = runtime

    # ``ua --resume <id>`` is a convenience alias for ``ua tui --resume <id>``.
    # It is handled here before the banner so ``ua --resume latest`` just works,
    # and ``ua --resume`` is discoverable as a top-level option.
    if resume is not None:
        # If a subcommand was also given, prefer that subcommand's handling;
        # otherwise, synthesize a ``tui --resume <id>`` invocation.
        if ctx.invoked_subcommand is None:
            # Re-use the same store/session logic as ``tui --resume`` by directly
            # invoking the tui command with a synthetic context.
            from unknown_agent.cli.commands.misc import tui_command
            import typer as _typer
            # Build a fake context that carries our runtime and the resume arg
            fake_ctx = _typer.Context(tui_command, obj=runtime)
            # ``resume`` may be a string value or empty string when ``--resume`` is bare.
            # Bare ``--resume`` (no value) yields True-ish via typer? In that case,
            # treat as "latest".
            _resume_val = resume if isinstance(resume, str) and resume.strip() else "latest"
            # Strip surrounding = if user passed ``--resume=abc``
            if isinstance(_resume_val, str) and _resume_val.startswith("="):
                _resume_val = _resume_val[1:]
            fake_ctx.args = ["--resume", _resume_val] if _resume_val else ["--resume", "latest"]
            tui_command(fake_ctx)
            raise _typer.Exit(0)
        # If a subcommand *was* given alongside ``--resume``, let the subcommand win
        # but note the ambiguity.

    if ctx.invoked_subcommand is None:
        output.print(banner(compact=True, subtitle="run `ua --help` to see what I can do"))
        return

    runtime.start_logging()
    render_warnings(runtime.load().warnings)


# --------------------------------------------------------------------------- #
# command registration (lazy imports keep startup fast)
# --------------------------------------------------------------------------- #
def _shell_command(
    ctx: typer.Context,
    task: str | None = typer.Argument(None, help="Optional task to run before the prompt appears."),
    provider: str | None = typer.Option(None, "--provider", "-p", help="Provider profile or type for this session.", show_default=False),
    model: str | None = typer.Option(None, "--model", "-m", help="Model for this session.", show_default=False),
    mode: str | None = typer.Option(None, "--mode", help="Agent mode: auto | plan | react.", show_default=False),
    max_steps: int | None = typer.Option(None, "--max-steps", help="Hard limit on agent steps.", show_default=False),
    no_setup: bool = typer.Option(False, "--no-setup", help="Never start onboarding from the shell."),
) -> None:
    """Start an interactive session: type tasks in plain language.

    With no provider configured, the shell offers onboarding first, so `ua` is
    always a usable entry point.  `ua --provider offline "add tests"` works too:
    the flags land here because the task text is routed to this command.
    """

    from unknown_agent.cli.shell import run_shell

    runtime: Runtime = ctx.obj
    # Per-session overrides: `ua -m gpt-4o-mini "..."` applies to every task typed in
    # this session, exactly like the global flags do for a single `ua run`.
    if provider:
        runtime.provider = provider
    if model:
        runtime.model = model
    if mode:
        runtime.mode = mode
    if max_steps:
        runtime.max_steps = max_steps
    runtime.start_logging()
    code = run_shell(runtime, task=task, auto_setup=not no_setup)
    raise typer.Exit(code)


def _register_commands() -> None:
    """Register command groups and top-level commands.

    Groups (``ua <group> <verb>``) are used where a namespace is genuinely useful;
    short, high-frequency actions are top-level commands so they are one word away.
    """

    from unknown_agent.cli.commands import (
        acp,
        agent,
        benchmark,
        browser,
        client_app,
        config_cmd,
        context_cmd,
        doctor,
        gateway,
        inspect,
        marketplace,
        mcp_cmd,
        memory_cmd,
        misc,
        orchestration,
        plugins,
        providers_cmd,
        release_cmd,
        runs,
        sandbox,
        scheduler,
        service,
        sessions,
        skills,
        tools,
        verify,
        vision_cmd,
        web as web_cmd,
    )

    # -- agent (top level, most used) -------------------------------------
    app.command("run", rich_help_panel="Agent")(agent.run_command)
    app.command("build", rich_help_panel="Agent")(agent.build_command)
    app.command("fix", rich_help_panel="Agent")(agent.fix_command)
    app.command("plan", rich_help_panel="Agent")(agent.plan_command)
    app.command("ask", rich_help_panel="Agent")(agent.ask_command)

    # -- onboarding, providers, models (first-run experience) --------------
    app.command("setup", rich_help_panel="Setup")(providers_cmd.setup)
    app.command("onboard", rich_help_panel="Setup", hidden=True)(providers_cmd.setup)
    app.command("web", rich_help_panel="Setup")(web_cmd.web_command)
    app.command("client", rich_help_panel="Setup")(client_app.client_command)
    app.add_typer(
        web_cmd.approve_app,
        name="approve",
        rich_help_panel="Setup",
        help=web_cmd.approve_app.info.help,
    )
    app.add_typer(providers_cmd.app, name="providers", rich_help_panel="Setup", help=providers_cmd.app.info.help)
    app.add_typer(providers_cmd.models_app, name="models", rich_help_panel="Setup", help=providers_cmd.models_app.info.help)
    app.add_typer(
        orchestration.app,
        name="orchestrate",
        help="Multi-worker delegation: plan a shape, run it, audit it.",
        rich_help_panel="Agent",
    )
    app.command("shell", rich_help_panel="Agent")(_shell_command)
    app.command("acp", rich_help_panel="Agent")(acp.acp_command)

    # -- project -----------------------------------------------------------
    app.command("init", rich_help_panel="Project")(misc.init_command)
    # The shell is a first-class surface, so it needs a first-class command. It was
    # previously defined but never registered, which is why nobody could reach it.
    app.command(
        "tui",
        rich_help_panel="Project",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(misc.tui_command)
    app.command("doctor", rich_help_panel="Project")(doctor.doctor_command)
    app.command("version", rich_help_panel="Project")(misc.version_command)
    app.command("upgrade", rich_help_panel="Project")(misc.upgrade_command)
    app.command("log", rich_help_panel="Project")(misc.log_command)
    app.add_typer(service.app, name="service", help="Optional background maintenance: systemd user units, manual runs.", rich_help_panel="Project")
    app.add_typer(scheduler.app, name="scheduler", help="Persistent cron and webhook automation.", rich_help_panel="Project")
    app.add_typer(sandbox.app, name="sandbox", help="Native subprocess containment profiles.", rich_help_panel="Project")
    app.add_typer(gateway.app, name="gateway", help="Durable channel-neutral inbox and outbox.", rich_help_panel="Project")
    app.add_typer(release_cmd.app, name="release", help="SBOM and signed provenance artifacts.", rich_help_panel="Project")
    app.command("completion", rich_help_panel="Project")(misc.completion_command)
    app.add_typer(inspect.app, name="inspect", help="Repository intelligence: stack, commands, layout, risks.", rich_help_panel="Project")

    # -- quality -----------------------------------------------------------
    app.command("verify", rich_help_panel="Quality")(verify.verify_command)
    app.add_typer(benchmark.app, name="benchmark", help="Reproducible performance and reliability regression gates.", rich_help_panel="Quality")
    app.add_typer(browser.app, name="browser", help="Chromium validation: flows, screenshots, console, traces.", rich_help_panel="Quality")
    app.add_typer(vision_cmd.app, name="vision", help="Vision: capability matrix, routing status, image analysis.", rich_help_panel="Quality")

    # -- knowledge and history --------------------------------------------
    app.add_typer(context_cmd.app, name="context", help="Inspect exactly what the model is told.", rich_help_panel="Knowledge")
    app.add_typer(memory_cmd.app, name="memory", help="Long-term project memory and procedures.", rich_help_panel="Knowledge")
    app.add_typer(runs.app, name="runs", help="Past runs: list, inspect, replay evidence.", rich_help_panel="History")
    app.add_typer(sessions.app, name="sessions", help="Session index across projects.", rich_help_panel="History")

    # -- tooling and configuration ----------------------------------------
    app.add_typer(tools.app, name="tools", help="Tool catalog: list, describe, call directly.", rich_help_panel="Tools")
    app.add_typer(mcp_cmd.app, name="mcp", help="MCP servers: add, inspect, test, trust, expose.", rich_help_panel="Tools")
    app.add_typer(plugins.app, name="plugins", help="Install, inspect and scaffold plugins.", rich_help_panel="Tools")
    app.add_typer(marketplace.app, name="marketplace", help="Install signed packages from a verified registry.", rich_help_panel="Tools")
    app.add_typer(skills.app, name="skills", help="Craft guidance the agent runs with: list, inspect, scaffold, audit.", rich_help_panel="Tools")
    app.add_typer(config_cmd.app, name="config", help="Inspect, validate and edit configuration.", rich_help_panel="Configuration")


_register_commands()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def normalize_argv(argv: list[str]) -> list[str]:
    """Move hoistable global flags to the front of the argument list.

    ``ua verify --json`` and ``ua --json verify`` then behave identically, which is
    what users expect from a modern CLI.  Unknown tokens keep their order, so
    command arguments and task strings are never reordered.
    """

    front: list[str] = []
    rest: list[str] = []
    for index, token in enumerate(argv):
        if token in HOISTABLE_FLAGS and index > 0:
            front.append(token)
        else:
            rest.append(token)
    return [*front, *rest]


#: Exit-code-agnostic route check.
_COMMANDS_REGISTERED = False


#: Global options that take a value; the token after them is not a positional.
_VALUE_FLAGS = frozenset(
    {
        "--config",
        "--cwd",
        "--project",
        "--mode",
        "--model",
        "-m",
        "--provider",
        "-p",
        "--max-steps",
        "--dod",
        "--definition-of-done",
        "--color",
        "--resume",
    }
)


def _first_positional(argv: list[str]) -> str | None:
    """The first token that is neither a flag nor a flag's value.

    `ua --provider offline` must not treat `offline` as the command name, and
    `ua --cwd /tmp "add tests"` must still route the task to the shell.
    """

    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token.startswith("-") and token != "-":
            if "=" in token or token not in _VALUE_FLAGS:
                index += 1
            else:
                index += 2
            continue
        return token
    return None


def _route_default(argv: list[str]) -> list[str]:
    """`ua` with no command enters the interactive session.

    Also routes `ua "<free text task>"` into the shell, so the product has one
    obvious entry point after installation instead of a wall of subcommands.
    """

    first = _first_positional(argv)
    if first is None:
        return [*argv, "shell"]
    if first not in _known_commands():
        return ["shell", *argv]
    return argv


def _known_commands() -> set[str]:
    from typer.main import get_command

    command = get_command(app)
    return set(getattr(command, "commands", {}) or ())


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point (``ua`` / ``unknown-agent``)."""

    resolved = list(argv) if argv is not None else sys.argv[1:]
    debug = "--debug" in resolved
    argv = normalize_argv(resolved)
    argv = _route_default(argv)
    try:
        app(prog_name=APP_NAME, args=argv)
    except typer.Exit as exc:  # normal Typer exits
        raise SystemExit(int(exc.exit_code or 0)) from None
    except KeyboardInterrupt:
        output.diag_print("[ua.warn]interrupted[/ua.warn]")
        raise SystemExit(ExitCode.INTERRUPTED) from None
    except UnknownAgentError as exc:
        raise SystemExit(render_error(exc, debug=debug)) from None
    except UsageError as exc:
        raise SystemExit(render_error(exc, debug=debug)) from None
    except Exception as exc:  # noqa: BLE001 - last line of defence
        raise SystemExit(render_error(exc, debug=debug)) from None


def run() -> None:  # pragma: no cover - alias for packaging tools
    main()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main", "run"]
