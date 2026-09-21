"""The default entry point: `ua`, `ua "<task>"` and piped usage.

These are the paths a new user hits first, and the ones a CI pipeline hits by accident,
so they are tested without a terminal: routing must be flag-aware and the shell must
never block waiting for a prompt that cannot come.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import _route_default, app
from unknown_agent.cli.shell import AgentShell

runner = CliRunner()


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ["shell"]),
        (["--json"], ["--json", "shell"]),
        (["verify"], ["verify"]),
        (["--json", "verify"], ["--json", "verify"]),
        (["add dark mode"], ["shell", "add dark mode"]),
        # The value of a global flag is not a command name.
        (["--provider", "offline"], ["--provider", "offline", "shell"]),
        (["--cwd", "/tmp"], ["--cwd", "/tmp", "shell"]),
        (["--provider", "offline", "fix the tests"], ["shell", "--provider", "offline", "fix the tests"]),
        # -m is a root option, so it stays before the command the router appends ...
        (["-m", "gpt-4.1-mini"], ["-m", "gpt-4.1-mini", "shell"]),
        # ... and when a task follows, the shell command receives it (it declares -m too).
        (["-m", "gpt-4.1-mini", "add tests"], ["shell", "-m", "gpt-4.1-mini", "add tests"]),
        (["--provider=offline"], ["--provider=offline", "shell"]),
    ],
)
def test_default_routing_is_flag_aware(argv: list[str], expected: list[str]) -> None:
    assert _route_default(list(argv)) == expected


# --------------------------------------------------------------------------- #
# non-interactive sessions
# --------------------------------------------------------------------------- #
def test_bare_ua_without_a_terminal_explains_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--provider", "offline", "shell"])
    assert result.exit_code == 2, result.output
    assert "non-interactive session" in result.output
    assert "echo" in result.output  # the piped-task hint


def test_piped_task_runs_once_and_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--provider", "offline", "shell"], input="say hello\n")
    assert result.exit_code == 0, result.output
    assert "run completed" in result.output or "completed" in result.output
    assert "session ended after 1 task(s)" in result.output


def test_shell_never_blocks_on_a_prompt_without_a_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: piped input used to hang forever waiting for the prompt."""

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--provider", "offline", "shell"], input="")
    assert result.exit_code in {0, 2}
    assert "non-interactive session" in result.output


def test_task_argument_takes_precedence_over_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--provider", "offline", "shell", "inspect this project"], input="ignored\n")
    assert result.exit_code == 0, result.output
    assert "session ended after 1 task(s)" in result.output


# --------------------------------------------------------------------------- #
# the shell object itself
# --------------------------------------------------------------------------- #
class _StubRuntime:
    """Minimal runtime for AgentShell: no provider calls, no journal."""

    def __init__(self, settings: object) -> None:
        self._settings = settings
        self.mode = None
        self.model = None
        self.provider = None
        self.max_steps = None
        self.read_only = False
        self.dry_run = False
        self.assume_yes = False
        self.non_interactive = True
        self.definition_of_done = None

    @property
    def prompter(self) -> object:
        from unknown_agent.ui.prompts import Prompter

        return Prompter(non_interactive=True)

    def load(self, **_: object) -> object:
        return type("Loaded", (), {"settings": self._settings})()

    def execute_run(self, request: object, **_: object) -> object:  # noqa: ANN401
        raise AssertionError("execute_run should not be called by these tests")


def test_one_shot_mode_returns_usage_without_a_task() -> None:
    from unknown_agent.config.models import ProviderConfig, Settings

    settings = Settings()
    settings.providers = {"offline": ProviderConfig(type="offline", model="offline-deterministic")}
    settings.provider = "offline"
    shell = AgentShell(_StubRuntime(settings))
    assert shell._run_once(None) == 2


def test_provider_scoped_shell_flags_reach_the_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ua --provider offline` must reach the shell even without a command name."""

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--provider", "offline", "shell", "say hello"], input="")
    assert result.exit_code == 0, result.output
    assert "completed" in result.output
    # The provider override is the offline one: no key, no network, deterministic.
    assert "offline" in result.output.lower()
