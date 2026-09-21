"""Context assembly: turn the world into a token-budgeted prompt.

``ContextBuilder`` is the single place that decides what the model sees.  It
combines, in priority order, and then trims to a token budget:

1. the system prompt (identity, workflow, definition of done, policy),
2. project intelligence (stack, commands, conventions, guidance files),
3. the repository map (structure + symbols),
4. procedural memory (workflows that match the task),
5. project memory (recalled facts),
6. working memory (task, hypotheses, recent observations),
7. the conversation (recent messages, oldest dropped first).

Keeping this in one class means prompt quality is inspectable
(``ua context show --tokens``) and regressions are testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.context.project import ProjectContext
from unknown_agent.context.repo_map import RepoMap, build_repo_map
from unknown_agent.core.models import Message
from unknown_agent.host.info import detect_host
from unknown_agent.memory.procedures import ProcedureLibrary
from unknown_agent.memory.working import WorkingMemory
from unknown_agent.observability.logging import get_logger
from unknown_agent.prompts.system import build_system_prompt, tool_catalog_text

log = get_logger("context.builder")

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Cheap, conservative token estimate used for budgeting."""

    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(slots=True)
class ContextBudget:
    total_tokens: int = 48_000
    system_tokens: int = 0
    project_tokens: int = 0
    repo_map_tokens: int = 0
    memory_tokens: int = 0
    conversation_tokens: int = 0
    tools_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total_budget": self.total_tokens,
            "system": self.system_tokens,
            "project": self.project_tokens,
            "repo_map": self.repo_map_tokens,
            "memory": self.memory_tokens,
            "conversation": self.conversation_tokens,
            "tools": self.tools_tokens,
            "used": self.used,
            "remaining": max(0, self.total_tokens - self.used),
        }

    @property
    def used(self) -> int:
        return self.system_tokens + self.project_tokens + self.repo_map_tokens + self.memory_tokens + self.conversation_tokens + self.tools_tokens


@dataclass(slots=True)
class BuiltContext:
    system_prompt: str
    messages: list[Message]
    budget: ContextBudget
    tool_catalog: str = ""
    repo_map: str = ""
    recalled: list[dict[str, Any]] = field(default_factory=list)
    procedures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "system_prompt_chars": len(self.system_prompt),
            "messages": len(self.messages),
            "budget": self.budget.as_dict(),
            "tool_catalog_lines": len(self.tool_catalog.splitlines()),
            "recalled_facts": len(self.recalled),
            "procedures": self.procedures,
            "repo_map_chars": len(self.repo_map),
        }


class ContextBuilder:
    """Builds prompts for planning and execution."""

    def __init__(
        self,
        settings: Settings,
        *,
        project: ProjectContext | None = None,
        memory: Any = None,
        procedures: ProcedureLibrary | None = None,
        working: WorkingMemory | None = None,
        skills: Any = None,
    ) -> None:
        self.settings = settings
        self.project = project
        self.memory = memory
        self.procedures = procedures
        #: A resolved :class:`~unknown_agent.skills.pack.SkillSet`, or ``None``.
        #: Resolved once per run by the runner (it reads the disk) and rendered per
        #: task here, so relevance is task-specific without re-scanning directories.
        self.skills = skills
        self.working = working or WorkingMemory(
            token_budget=settings.memory.working_memory_tokens,
            observation_budget=settings.agent.observation_token_budget,
        )
        self._repo_map: RepoMap | None = None

    # -- components --------------------------------------------------------
    def repo_map(self, *, refresh: bool = False) -> RepoMap:
        if self._repo_map is None or refresh:
            root = self.project.root if self.project else Path.cwd()
            self._repo_map = build_repo_map(
                root,
                cache_dir=(root / ".unknown" / "cache"),
                refresh=refresh,
            )
        return self._repo_map

    def environment_block(self) -> str:
        host = detect_host()
        lines = [
            f"host: {host.os_name} {host.os_release} ({host.arch}), python {host.python_version}",
            f"shell: {host.shell or 'unknown'}",
            f"cwd: {self.project.root if self.project else Path.cwd()}",
            f"interactive: {'yes' if _is_interactive() else 'no (non-interactive: never block on a prompt)'}",
        ]
        if host.is_container:
            lines.append("container: yes (filesystem may be ephemeral; do not rely on host services)")
        if host.is_windows:
            lines.append("windows: prefer cross-platform commands; avoid POSIX-only tools")
        if host.is_wsl:
            lines.append("wsl: cross-filesystem operations under /mnt are slow")
        return "\n".join(lines)

    def recall(self, task: str) -> list[dict[str, Any]]:
        if self.memory is None or not self.settings.memory.auto_remember:
            return []
        try:
            return self.memory.recall_for_task(task, limit=self.settings.memory.max_recall_items)
        except Exception:  # pragma: no cover - memory must never break a run
            log.exception("memory recall failed")
            return []

    def matching_procedures(self, task: str) -> list[Any]:
        if self.procedures is None or not self.settings.memory.procedural_autoload:
            return []
        return self.procedures.match(task)

    def tool_catalog(self, specs: list[dict[str, Any]]) -> str:
        return tool_catalog_text(specs)

    # -- prompt assembly ---------------------------------------------------
    def build_system_message(
        self,
        *,
        tool_specs: list[dict[str, Any]],
        task: str = "",
        mode: str | None = None,
        plan_summary: str = "",
    ) -> Message:
        recalled = self.recall(task)
        procedures = self.matching_procedures(task)
        catalog = self.tool_catalog(tool_specs)
        extra = None
        if self.project and self.settings.project.extra_instructions:
            extra = self.settings.project.extra_instructions
        guidance = self._guidance_block()
        if guidance:
            extra = (extra + "\n\n" if extra else "") + guidance
        system_prompt = build_system_prompt(
            mode=mode or self.settings.agent.mode,
            definition_of_done=self.settings.agent.definition_of_done,
            environment=self.environment_block(),
            project_summary=self.project.prompt_summary() if self.project else "",
            procedures=procedures,
            memories=recalled,
            tool_catalog=catalog,
            skills=self.skill_section(task),
            plan_summary=plan_summary,
            extra_instructions=extra,
            max_steps=self.settings.agent.max_steps,
        )
        return Message.system(system_prompt)

    def skill_section(self, task: str = "") -> str:
        """The active skills' craft guidance, budgeted for this task.

        Returns "" when skills are disabled or unresolved, so the prompt is
        byte-identical to the pre-skills prompt for a project that never opted in.
        """

        if self.skills is None:
            return ""
        try:
            return self.skills.prompt_section(task=task)
        except Exception:  # noqa: BLE001 - a bad skill file must not stop a run
            log.debug("skill prompt section failed", exc_info=True)
            return ""

    def build_plan_messages(self, *, task: str, tool_specs: list[dict[str, Any]], constraints: str = "") -> tuple[list[Message], ContextBudget]:
        from unknown_agent.prompts.planner import PLANNER_SYSTEM, planner_user_prompt

        recalled = self.recall(task)
        memories_text = "\n".join(f"- ({item['kind']}) {item['text']}" for item in recalled)
        catalog = self.tool_catalog(tool_specs)
        user = planner_user_prompt(
            task=task,
            project_summary=self.project.prompt_summary() if self.project else "",
            tool_catalog=catalog,
            memories=memories_text,
            mode=self.settings.agent.mode,
            constraints=constraints or self._policy_summary(),
        )
        messages = [Message.system(PLANNER_SYSTEM), Message.user(user)]
        budget = ContextBudget(total_tokens=self.settings.agent.context_token_budget)
        budget.system_tokens = estimate_tokens(PLANNER_SYSTEM)
        budget.project_tokens = estimate_tokens(self.project.prompt_summary() if self.project else "")
        budget.repo_map_tokens = estimate_tokens(self.repo_map_block())
        budget.tools_tokens = estimate_tokens(catalog)
        budget.memory_tokens = estimate_tokens(memories_text)
        budget.conversation_tokens = estimate_tokens(user)
        return messages, budget

    def repo_map_block(self) -> str:
        if not self.settings.agent.include_repo_map:
            return ""
        try:
            return self.repo_map().render(max_chars=8000)
        except Exception:  # pragma: no cover - map is best-effort
            log.exception("repo map failed")
            return ""

    def _guidance_block(self) -> str:
        if not self.project or not self.project.pointers:
            return ""
        blocks: list[str] = []
        for name, content in self.project.pointers.items():
            if name.lower().startswith("readme"):
                continue
            blocks.append(f"--- {name} ---\n{content[:1200]}")
        return "\n\n".join(blocks)

    def _policy_summary(self) -> str:
        policy = self.settings.policy
        lines = [
            f"read-only: {policy.read_only}",
            f"allow network: {policy.allow_network}",
            f"allow outside workspace: {policy.allow_outside_workspace}",
            f"destructive actions allowed: {policy.allow_destructive}",
        ]
        return "\n".join(lines)

    # -- conversation trimming --------------------------------------------
    def fit_messages(self, messages: list[Message], *, budget: ContextBudget, reserved_tokens: int = 4000) -> list[Message]:
        """Drop oldest conversation turns until the prompt fits the budget."""

        available = max(2_000, budget.total_tokens - budget.system_tokens - reserved_tokens)
        system = [message for message in messages if message.role == "system"]
        conversation = [message for message in messages if message.role != "system"]
        kept: list[Message] = []
        used = 0
        for message in reversed(conversation):
            cost = estimate_tokens(message.content) + 20
            if used + cost > available and kept:
                break
            kept.append(message)
            used += cost
        kept.reverse()
        budget.conversation_tokens = used
        return [*system, *kept]

    def describe(self, built: BuiltContext) -> str:
        budget = built.budget.as_dict()
        lines = [
            f"context budget: {budget['used']:,} / {budget['total_budget']:,} tokens",
            f"  system: {budget['system']:,}",
            f"  project: {budget['project']:,}",
            f"  repo map: {budget['repo_map']:,}",
            f"  memory: {budget['memory']:,}",
            f"  tools: {budget['tools']:,}",
            f"  conversation: {budget['conversation']:,}",
            f"messages: {len(built.messages)}",
            f"recalled facts: {len(built.recalled)}",
            f"procedures: {', '.join(built.procedures) or 'none'}",
        ]
        return "\n".join(lines)


def _is_interactive() -> bool:
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


__all__ = ["ContextBuilder", "BuiltContext", "ContextBudget", "estimate_tokens"]
