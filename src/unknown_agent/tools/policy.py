"""Policy engine: what the agent is allowed to do, enforced in code.

Guarantees:

* **Workspace containment** - paths outside the project root are denied unless
  ``policy.allow_outside_workspace`` is set, and denied globs (``.env``, ``*.pem``,
  ``.git``, ``node_modules``, key material) always lose.
* **Command denylist** - catastrophic command patterns (disk formatting, ``rm -rf
  /``, pipe-to-shell, force pushes) never run, even with ``--yes``.
* **Read-only mode** - ``--read-only`` strips every mutating tool call, which is
  how ``ua plan`` and ``ua ask`` are implemented.
* **Dry-run** - mutating tools are simulated and reported as such.
* **Confirmation** - destructive actions ask first; non-interactive sessions get
  the safe default and a log line explaining why.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel
from unknown_agent.observability.logging import get_logger
from unknown_agent.tools.base import Tool

log = get_logger("tools.policy")


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False
    dry_run: bool = False
    rewritten_arguments: dict[str, Any] = field(default_factory=dict)
    matched_rule: str = ""

    @property
    def denied_reason(self) -> str:
        return self.reason or "denied by policy"


class PolicyEngine:
    def __init__(self, settings: Any, *, project_root: Path, prompter: Any = None) -> None:
        self.settings = settings
        self.policy = settings.policy
        self.project_root = Path(project_root).resolve()
        self.prompter = prompter
        self._denied_globs = tuple(self.policy.denied_paths)
        self._denied_patterns = [re.compile(pattern) for pattern in self.policy.command_denylist]
        self._allowlist = tuple(self.policy.command_allowlist or ())

    # -- paths -------------------------------------------------------------
    def resolve_path(self, raw: str | Path, *, must_exist: bool = False) -> Path:
        """Resolve ``raw`` against the project root and normalise it."""

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        return candidate.resolve()

    def _matches_glob(self, path: Path, pattern: str) -> bool:
        """Match a project-relative glob such as ``src/**/*.py`` or ``pyproject.toml``."""

        candidates = {str(path)}
        try:
            candidates.add(path.relative_to(self.project_root).as_posix())
        except ValueError:
            pass
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(candidate, pattern.lstrip("./")):
                return True
        return False

    def check_path(self, path: Path, *, mutating: bool = False) -> PolicyDecision:
        """Decide whether a path may be used at all, and whether it may be *changed*.

        ``mutating=True`` additionally enforces the project's ``protected_paths``
        (the files a repository declares off-limits) and the built-in list of files
        that should never be rewritten by an agent, such as a lockfile or a CI
        workflow, unless the project explicitly removed them from the list.
        """

        if mutating:
            protected = tuple(getattr(self.settings.project, "protected_paths", ()) or ())
            for pattern in protected:
                if self._matches_glob(path, pattern):
                    return PolicyDecision(
                        False,
                        f"{path} is protected by [project] protected_paths ({pattern!r})",
                        matched_rule=f"protected_paths:{pattern}",
                    )
        text = str(path)
        for pattern in self._denied_globs:
            normalised = pattern.lstrip("./")
            if fnmatch.fnmatch(text, f"*{normalised}") or fnmatch.fnmatch(path.name, normalised.strip("*/")) or fnmatch.fnmatch(text, f"*/{normalised}"):
                return PolicyDecision(False, f"path matches denied pattern {pattern!r}", matched_rule=f"denied_paths:{pattern}")
        if not self.policy.allow_outside_workspace:
            try:
                path.relative_to(self.project_root)
            except ValueError:
                return PolicyDecision(
                    False,
                    f"{path} is outside the project root {self.project_root}",
                    matched_rule="containment",
                )
        return PolicyDecision(True, "path allowed", matched_rule="containment")

    # -- commands ----------------------------------------------------------
    def check_command(self, command: str) -> PolicyDecision:
        for pattern, compiled in zip(self.policy.command_denylist, self._denied_patterns, strict=False):
            if compiled.search(command):
                return PolicyDecision(False, f"command matches denylist pattern {pattern!r}", matched_rule=f"command_denylist:{pattern}")
        if self._allowlist:
            head = command.strip()
            if not any(head.startswith(prefix) for prefix in self._allowlist):
                return PolicyDecision(False, f"command is not in the allowlist ({', '.join(self._allowlist)})", matched_rule="command_allowlist")
        if self.policy.confirm_destructive:
            hint = _dangerous_hint(command)
            if hint:
                return PolicyDecision(
                    True,
                    f"command looks destructive ({hint!r} in {command!r})",
                    requires_confirmation=True,
                    matched_rule="command_needs_confirmation",
                )
        return PolicyDecision(True, "command allowed", matched_rule="command_denylist:pass")

    # -- tools -------------------------------------------------------------
    def evaluate(self, tool: Tool, arguments: dict[str, Any]) -> PolicyDecision:
        if self.policy.read_only and tool.risk is not RiskLevel.READ:
            return PolicyDecision(
                False,
                f"{tool.name} has risk {tool.risk.value} but the session is read-only",
                matched_rule="read_only",
            )
        if self.policy.dry_run and tool.risk in {RiskLevel.WRITE, RiskLevel.DESTRUCTIVE}:
            return PolicyDecision(True, "dry-run: mutation simulated", dry_run=True, matched_rule="dry_run")

        mutating = tool.risk in {RiskLevel.WRITE, RiskLevel.DESTRUCTIVE}
        path_args = [value for key, value in arguments.items() if key in {"path", "file", "target", "source", "destination"} and isinstance(value, str)]
        for raw in path_args:
            decision = self.check_path(self.resolve_path(raw), mutating=mutating)
            if not decision.allowed:
                return decision
        for raw in arguments.get("paths") or []:
            if isinstance(raw, str):
                decision = self.check_path(self.resolve_path(raw), mutating=mutating)
                if not decision.allowed:
                    return decision

        if tool.risk in {RiskLevel.EXECUTE, RiskLevel.DESTRUCTIVE}:
            command = str(arguments.get("command") or "")
            if command:
                decision = self.check_command(command)
                if not decision.allowed:
                    return decision
                if decision.requires_confirmation:
                    # A destructive-looking command keeps its confirmation requirement:
                    # risk is a property of the tool, but danger is a property of *this*
                    # invocation ("rm -rf build" vs "git status").
                    return PolicyDecision(
                        True,
                        decision.reason,
                        requires_confirmation=True,
                        matched_rule=decision.matched_rule,
                    )
        if tool.risk is RiskLevel.NETWORK and not self.policy.allow_network:
            return PolicyDecision(False, "network access is disabled (policy.allow_network = false)", matched_rule="network")
        if tool.risk is RiskLevel.DESTRUCTIVE and not self.policy.allow_destructive:
            return PolicyDecision(
                False,
                f"{tool.name} is destructive and policy.allow_destructive is false",
                matched_rule="destructive",
            )
        requires_confirmation = False
        if tool.risk is RiskLevel.DESTRUCTIVE and self.policy.confirm_destructive:
            requires_confirmation = True
        elif self.policy.confirm_writes and tool.risk is RiskLevel.WRITE:
            requires_confirmation = True
        return PolicyDecision(True, "allowed", requires_confirmation=requires_confirmation, matched_rule="default")

    def confirm(self, tool: Tool, arguments: dict[str, Any], decision: PolicyDecision) -> PolicyDecision:
        """Ask for approval when the decision requires it.

        Returns the (possibly new, always denying) decision.  A refusal carries the
        reason, so the model and the user both learn *why* nothing ran: no prompter in
        a non-interactive session, or a human saying no.
        """

        if not decision.requires_confirmation:
            return decision
        summary = tool.summarise_call(arguments)
        if self.prompter is None:
            log.warning("no prompter available; refusing unconfirmed %s", tool.name)
            return PolicyDecision(
                False,
                f"{tool.name} ({tool.risk.value}) needs confirmation but no interactive prompt is available "
                f"({decision.denied_reason}). Re-run with --yes to approve it, or choose a safer command.",
                matched_rule="confirmation_unavailable",
            )
        approved = bool(self.prompter.confirm(f"Run {tool.risk.value} tool {tool.name}?", preview=summary, default=False, dangerous=True))
        if approved:
            return decision
        return PolicyDecision(False, f"the user declined to run {tool.name} ({summary})", matched_rule="confirmation_declined")


DANGEROUS_HINTS = ("rm ", "rmdir", "sudo", "chmod", "chown", "kill", "pkill", "systemctl", "dd ", "truncate", "git push", "npm publish", "drop database", "migrate")


def _dangerous_hint(command: str) -> str | None:
    lowered = command.lower()
    return next((hint for hint in DANGEROUS_HINTS if hint in lowered), None)


def _looks_dangerous(command: str) -> bool:
    return _dangerous_hint(command) is not None


__all__ = ["PolicyEngine", "PolicyDecision"]
