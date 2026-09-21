"""Exception hierarchy and process exit codes for Unknown Agent.

Every user-facing failure is a subclass of :class:`UnknownAgentError` carrying a
machine-readable ``code``, a human ``message``, and an actionable ``hint``.  The
CLI renders them as a structured error block (human mode) or a JSON object
(``--json`` mode) and maps ``exit_code`` to the process exit status so that
shell scripts and CI pipelines can branch on the failure class.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Stable process exit codes.  Do not renumber: CI and scripts depend on them."""

    OK = 0
    FAILURE = 1          # agent ran but the task did not reach a good state
    USAGE = 2            # bad flags / arguments (Typer also uses 2)
    BLOCKED = 3          # task incomplete: gates failing, needs input, budget hit
    CONFIG = 4           # configuration, credentials, or environment problem
    PROVIDER = 5         # model provider error (auth, rate limit, transport)
    POLICY = 6           # tool call denied by policy
    GATE = 7             # validation gate failed
    BUDGET = 8           # step / token / wall-clock budget exhausted
    TOOL = 9             # tool execution failed
    INTERRUPTED = 130    # SIGINT


class UnknownAgentError(Exception):
    """Base class for all Unknown Agent errors."""

    code = "unknown_agent_error"
    exit_code: ExitCode = ExitCode.FAILURE
    default_hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint or self.default_hint
        self.context: dict[str, Any] = context or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.code,
            "message": self.message,
            "exit_code": int(self.exit_code),
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.context:
            payload["context"] = self.context
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class UsageError(UnknownAgentError):
    code = "usage_error"
    exit_code = ExitCode.USAGE


class ConfigError(UnknownAgentError):
    code = "config_error"
    exit_code = ExitCode.CONFIG
    default_hint = "Run `ua config doctor` to validate your configuration."


class ProviderError(UnknownAgentError):
    code = "provider_error"
    exit_code = ExitCode.PROVIDER
    default_hint = "Check credentials with `ua providers test` and pick a model with `ua providers list`."


class ProviderAuthError(ProviderError):
    code = "provider_auth_error"


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limit"
    default_hint = "Retry shortly, lower concurrency, or switch models with `--model`."


class PolicyDenied(UnknownAgentError):
    code = "policy_denied"
    exit_code = ExitCode.POLICY
    default_hint = "Adjust `[policy]` in `.unknown/config.toml` or re-run with `--yes` / `--allow-network`."


class ToolError(UnknownAgentError):
    code = "tool_error"
    exit_code = ExitCode.TOOL


class ToolTimeout(ToolError):
    code = "tool_timeout"
    default_hint = "Raise the tool timeout in `.unknown/config.toml` under `[tools.timeouts]`."


class GateFailure(UnknownAgentError):
    code = "gate_failure"
    exit_code = ExitCode.GATE
    default_hint = "Inspect the gate output, fix the reported issue, then re-run `ua verify`."


class BudgetExceeded(UnknownAgentError):
    code = "budget_exceeded"
    exit_code = ExitCode.BUDGET
    default_hint = "Raise budgets with `--max-steps` / `[agent] max_steps` or narrow the task."


class TaskBlocked(UnknownAgentError):
    code = "task_blocked"
    exit_code = ExitCode.BLOCKED
    default_hint = "Review `ua runs show --last` for the blocking evidence."


class Interrupted(UnknownAgentError):
    code = "interrupted"
    exit_code = ExitCode.INTERRUPTED


class PluginError(UnknownAgentError):
    code = "plugin_error"
    exit_code = ExitCode.CONFIG


__all__ = [
    "ExitCode",
    "UnknownAgentError",
    "UsageError",
    "ConfigError",
    "ProviderError",
    "ProviderAuthError",
    "ProviderRateLimitError",
    "PolicyDenied",
    "ToolError",
    "ToolTimeout",
    "GateFailure",
    "BudgetExceeded",
    "TaskBlocked",
    "Interrupted",
    "PluginError",
]
