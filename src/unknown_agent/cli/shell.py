"""The interactive session: bare ``ua`` drops you into a working agent.

Design goals:

* **Instant value.** With a configured provider, ``ua`` starts a prompt where you
  type a task in plain language and watch the loop work.
* **Self-healing entrance.** Unconfigured? The shell offers onboarding instead of
  failing; broken? It names the problem and offers the fix.
* **Slash commands** for the things you need mid-session (``/model``, ``/provider``,
  ``/verify``, ``/tools``, ``/doctor``, ``/setup``, ``/help``, ``/exit``) so the user
  never has to leave and lose context.
* **Ctrl-C is not an exit.** It cancels the current task, keeps the session, and
  says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text

from unknown_agent.errors import ExitCode, UnknownAgentError
from unknown_agent.onboarding.detect import detect_setup_state
from unknown_agent.onboarding.screens import info_panel
from unknown_agent.ui.console import output
from unknown_agent.ui.prompts import Prompter

PROMPT = "ua ›"

SLASH_HELP: tuple[tuple[str, str], ...] = (
    ("/help", "show this help"),
    ("/setup", "open the interactive setup panel (provider, key, model)"),
    ("/provider", "show or switch the active provider profile"),
    ("/model [id]", "show or set the default model"),
    ("/models", "list models the active provider offers"),
    ("/tools", "list the tools available in this session"),
    ("/verify", "run the quality gates on this project"),
    ("/doctor", "diagnose configuration, credentials and toolchain"),
    ("/runs", "list recent runs"),
    ("/clear", "clear the screen"),
    ("/exit", "leave the session (also: exit, quit, Ctrl-D)"),
)


class AgentShell:
    """A small, honest REPL around the runner."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.prompter: Prompter = runtime.prompter
        self.turns = 0

    # -- entry point -------------------------------------------------------
    @property
    def interactive(self) -> bool:
        """A real terminal on both ends: prompts are meaningful, output is for a human."""

        return not self.prompter.non_interactive and output.interactive

    def run(self, *, task: str | None = None, auto_setup: bool = True) -> int:
        state = detect_setup_state(self.runtime.load().settings)
        if not state.ready:
            if auto_setup and self.interactive:
                output.print(
                    Text(
                        f"Unknown Agent is not configured yet ({state.reason}). Opening the setup panel …",
                        style="ua.warn",
                    )
                )
                if not self._offer_setup():
                    return int(ExitCode.CONFIG)
            else:
                output.error_panel(
                    "CONFIG ERROR",
                    state.reason,
                    hint=state.hint or "Run `ua setup` in a terminal to open the guided setup panel.",
                )
                return int(ExitCode.CONFIG)

        if not self.interactive:
            # Piped stdin or a CI runner: never block on a prompt.  `echo "task" | ua`
            # works, and a bare `ua` explains itself instead of hanging forever.
            return self._run_once(task)

        self._welcome()
        if task:
            self._execute(task)
        while True:
            try:
                line = self.prompter.text(f"{PROMPT}", default="", allow_empty=True)
            except (KeyboardInterrupt, EOFError):
                output.blank()
                output.print("[ua.muted]session ended[/ua.muted]")
                return int(ExitCode.OK)
            line = (line or "").strip()
            if not line:
                continue
            if line.startswith("/"):
                if not self._slash(line):
                    break
                continue
            self._execute(line)
        return int(ExitCode.OK)

    def _run_once(self, task: str | None) -> int:
        """One-shot mode: the task comes from the argument or from piped stdin."""

        if task is None:
            task = _piped_task()
            if task == _END_OF_SESSION:
                output.print("[ua.muted]session ended[/ua.muted]")
                return int(ExitCode.OK)
        if not task:
            output.print(
                info_panel(
                    "non-interactive session",
                    "No terminal and no task were given.",
                    hint=(
                        'ua "add a healthcheck endpoint"   # run one task, then exit\n'
                        'echo "fix the failing test" | ua  # read the task from stdin\n'
                        "ua run --help                      # scripted runs, --json output"
                    ),
                )
            )
            return int(ExitCode.USAGE)
        exit_code = self._execute(task)
        # Remember the task for /resume-style workflows and keep the journal honest.
        if self.turns:
            output.diag_print(f"session ended after {self.turns} task(s)")
        return exit_code

    # -- pieces ------------------------------------------------------------
    def _welcome(self) -> None:
        loaded = self.runtime.load()
        settings = loaded.settings
        profile_name = settings.provider or "default"
        profile = settings.providers.get(profile_name)
        model = profile.model if profile else "(unset)"
        output.blank()
        header = Text()
        header.append("Unknown Agent", style="ua.brand")
        header.append(f"   {profile_name}:{model}", style="ua.muted")
        header.append(f"   {Path(loaded.project_root or loaded.paths.cwd).name}", style="dim")
        output.print(header)
        output.print(Text("  type a task in plain language, or /help for commands. Ctrl-C cancels a task.", style="dim"))
        output.print(Text("  " + "─" * 64, style="ua.muted"))

    def _execute(self, task: str) -> int:
        """Run one task and return the process exit code for it."""

        from unknown_agent.core.runner import RunRequest

        # `execute_run` is a method on the runtime (it owns provider resolution, browser
        # manager and journal wiring) - not a module-level function.

        self.turns += 1
        request = RunRequest(
            task=task,
            mode=self.runtime.mode or "auto",
            read_only=self.runtime.read_only,
            dry_run=self.runtime.dry_run,
            yes=self.runtime.assume_yes,
            non_interactive=self.runtime.non_interactive,
            model=self.runtime.model,
            provider=self.runtime.provider,
            max_steps=self.runtime.max_steps,
            definition_of_done=self.runtime.definition_of_done,
            metadata={"session_turn": self.turns},
        )
        try:
            result = self.runtime.execute_run(request)
        except KeyboardInterrupt:
            output.blank()
            output.print("[ua.warn]task cancelled - the session is still open[/ua.warn]")
            return int(ExitCode.INTERRUPTED)
        except UnknownAgentError as exc:
            output.error_panel(type(exc).__name__.replace("Error", " ERROR"), exc.message, hint=exc.hint)
            return int(getattr(exc, "exit_code", ExitCode.FAILURE) or ExitCode.FAILURE)

        status = getattr(result, "state", None)
        if status is not None:
            colour = "ua.ok" if result.ok else "ua.warn"
            output.print(Text(f"  {status.status.value} · {len(status.files_changed)} file(s) changed", style=colour))
        return int(ExitCode.OK if result.ok else ExitCode.FAILURE)

    def _slash(self, line: str) -> bool:
        """Handle a slash command.  Returns False to exit the shell."""

        command, _, argument = line[1:].partition(" ")
        command = command.strip().lower()
        argument = argument.strip()

        if command in {"exit", "quit", "q"}:
            output.print("[ua.muted]session ended[/ua.muted]")
            return False
        if command in {"help", "h", "?"}:
            output.print(Text("commands:", style="ua.panel.title"))
            for name, description in SLASH_HELP:
                output.print(Text(f"  {name:<12} {description}", style="dim"))
            return True
        if command == "clear":
            output.data.clear()
            return True
        if command == "setup":
            self._setup_flow()
            return True
        if command == "provider":
            if argument:
                self._run_cli(["providers", "use", argument])
            else:
                self._run_cli(["providers", "list"])
            return True
        if command == "model":
            if argument:
                self._run_cli(["models", "use", argument])
            else:
                loaded = self.runtime.load()
                name = loaded.settings.provider or "default"
                profile = loaded.settings.providers.get(name)
                output.print(f"current model: [ua.kv.key]{(profile.model if profile else '(unset)')}[/ua.kv.key]")
            return True
        if command == "models":
            self._run_cli(["models", "list"])
            return True
        if command == "tools":
            self._run_cli(["tools", "list"])
            return True
        if command == "verify":
            self._run_cli(["verify", *(["--required-only"] if not argument else argument.split())])
            return True
        if command == "doctor":
            self._run_cli(["doctor"])
            return True
        if command == "runs":
            self._run_cli(["runs", "list"])
            return True

        output.print(f"[ua.warn]unknown command[/ua.warn] /{command} - try /help")
        return True

    def _run_cli(self, args: list[str]) -> None:
        """Run a command in-process, reusing this runtime's configuration."""

        from typer.main import get_command

        from unknown_agent.cli.app import app as cli_app

        root = get_command(cli_app)
        ctx = root.make_context("ua", [*args, "--cwd", str(self.runtime.cwd)], obj=self.runtime, resilient_parsing=False)
        try:
            root.invoke(ctx)
        except SystemExit as exc:  # typer exits with codes; keep the session alive
            code = exc.code if isinstance(exc.code, int) else 0
            if code not in (0, int(ExitCode.OK)):
                output.print(f"[ua.muted](command exited {code})[/ua.muted]")
        except UnknownAgentError as exc:
            output.error_panel("ERROR", exc.message, hint=exc.hint)

    def _offer_setup(self) -> bool:
        """Route straight into the setup panel; no command to remember.

        When the panel can run (a real terminal + textual) it *is* the setup, so the
        user is not asked to confirm something they already asked for by running the
        agent.  Without a panel we still ask before taking over the prompt.
        """

        from unknown_agent.onboarding.panel import panel_available

        if not panel_available(self.runtime) and not self.prompter.confirm("Configure a provider now?", default=True):
            return False
        return self._setup_flow()

    def _setup_flow(self) -> bool:
        from unknown_agent.onboarding.panel import launch_setup

        try:
            outcome = launch_setup(self.runtime)
        except UnknownAgentError as exc:
            output.error_panel("SETUP ERROR", exc.message, hint=exc.hint)
            return False
        except KeyboardInterrupt:
            output.print("[ua.muted]setup cancelled[/ua.muted]")
            return False
        if not outcome.ok and outcome.error:
            output.error_panel("SETUP ERROR", outcome.error, hint="Run `ua setup` to try again.")
        return outcome.ok


#: Sentinel returned by :func:`_piped_task` when the pipe simply says "quit".
_END_OF_SESSION = "\x00exit"


def _piped_task() -> str:
    """The whole of stdin when it is not a terminal (``echo "task" | ua``)."""

    import sys

    if sys.stdin is None or sys.stdin.isatty():
        return ""
    try:
        text = sys.stdin.read()
    except (OSError, ValueError):  # pragma: no cover - closed or replaced stdin
        return ""
    text = (text or "").strip()
    if text.lower() in {"exit", "quit", ":q", "q"}:
        return _END_OF_SESSION
    return text


def run_shell(runtime: Any, *, task: str | None = None, auto_setup: bool = True) -> int:
    return AgentShell(runtime).run(task=task, auto_setup=auto_setup)


__all__ = ["AgentShell", "PROMPT", "SLASH_HELP", "run_shell"]
