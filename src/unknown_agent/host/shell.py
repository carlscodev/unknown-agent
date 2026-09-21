"""Shell/command translation layer.

The agent emits commands; this module decides how they execute on the current
host.  On POSIX we run programs directly with argv (no shell metacharacter
expansion).  On Windows we map POSIX-only invocations to their native
equivalents and use ``cmd.exe`` only for explicit pipelines.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: POSIX -> Windows executable substitutions applied to the head of a command.
WINDOWS_PROGRAM_MAP: dict[str, str] = {
    "python3": "python",
    "python": "python",
    "pip3": "pip",
    "grep": "findstr",
    "sed": "python",     # handled specially; sed rewrites are rare and risky
    "open": "start",
    "xdg-open": "start",
    "cp": "copy",
    "mv": "move",
    "rm": "del",
    "ls": "dir",
}

#: Commands that require a real shell (pipes, redirection, chaining).
SHELL_OPERATOR_RE = re.compile(r"(\|\||&&|\||;|>|<|`|\$\()")

CHAIN_OPERATORS = ("&&", "||", ";")


@dataclass(slots=True)
class CommandPlan:
    argv: list[str]
    use_shell: bool
    reason: str = ""


def uses_shell_operators(command: str) -> bool:
    return bool(SHELL_OPERATOR_RE.search(command))


def plan_command(command: str, *, allow_shell: bool = False) -> CommandPlan:
    """Translate a command string into an execution plan for this host."""

    text = command.strip()
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        head = text.split(" ", 1)[0]
        rest = text[len(head) :]
        mapped = WINDOWS_PROGRAM_MAP.get(head, head)
        if mapped != "python" or head in {"python", "python3"}:
            text = f"{mapped}{rest}"
        if uses_shell_operators(text):
            return CommandPlan(argv=["cmd", "/c", text], use_shell=True, reason="windows shell operators")
        from unknown_agent.host.process import split_command

        return CommandPlan(argv=split_command(text), use_shell=False)
    if uses_shell_operators(text):
        return CommandPlan(argv=["/bin/sh", "-c", text] if allow_shell else text.split() and ["/bin/sh", "-c", text, ""], use_shell=True, reason="posix shell operators")
    from unknown_agent.host.process import split_command

    return CommandPlan(argv=split_command(text), use_shell=False)


def normalize_package_manager_command(command: str, package_manager: str | None) -> str:
    """Rewrite generic invocations to the project's actual package manager.

    Prevents the classic failure where the agent runs ``npm test`` in a pnpm
    workspace (or ``pytest`` in a project that uses ``uv run pytest``).
    """

    if not package_manager:
        return command
    pm = package_manager.lower()
    rewrites = {
        "pnpm": [(r"^npm\s+(run\s+)?", "pnpm "), (r"^npx\s+", "pnpm dlx ")],
        "yarn": [(r"^npm\s+(run\s+)?", "yarn "), (r"^npx\s+", "yarn dlx ")],
        "bun": [(r"^npm\s+(run\s+)?", "bun "), (r"^npx\s+", "bunx ")],
        "uv": [(r"^python\s+-m\s+pytest", "uv run pytest"), (r"^pytest", "uv run pytest")],
        "poetry": [(r"^pytest", "poetry run pytest"), (r"^python\s+-m\s+pytest", "poetry run pytest")],
        "pipenv": [(r"^pytest", "pipenv run pytest")],
    }
    for pattern, replacement in rewrites.get(pm, []):
        if re.search(pattern, command):
            return re.sub(pattern, replacement, command)
    return command
