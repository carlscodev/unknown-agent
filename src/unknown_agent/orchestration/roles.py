"""Orchestration roles: the specialists Unknown Agent can delegate to.

A role is a **contract**, not a prompt.  It declares what the worker is for, what
it must produce, which tools it may touch, whether it may mutate the tree, which
reasoning depth it runs at, and what the conductor should verify when it returns.
That is what makes multi-agent work structured instead of chaotic: two workers can
run concurrently because their ``owned_paths`` do not overlap, and a worker cannot
wander outside its remit because its tool set is a strict subset of the session's.

Adding a role is one entry in :data:`ROLES`.  The CLI (``ua orchestrate roles``),
the router, the conductor, the ledger and the TUI orchestration monitor all read
that table, so a new specialist appears everywhere at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "Role",
    "ROLES",
    "ROLE_ORDER",
    "ROLE_ALIASES",
    "get_role",
    "has_role",
    "resolve_role_id",
    "roles_payload",
    "roles_for_pattern",
]


@dataclass(frozen=True, slots=True)
class Role:
    """One specialist worker."""

    id: str
    label: str
    mission: str
    #: What this role must hand back.  The conductor checks these exist.
    outputs: tuple[str, ...] = ()
    #: Tools this role may use.  Intersected with the session registry at build
    #: time, so a role can never grant itself a tool the session does not have.
    tools: tuple[str, ...] = ()
    #: Tools explicitly removed even if the session has them.
    deny_tools: tuple[str, ...] = ()
    #: May this role write to the tree?
    mutates: bool = True
    #: Reasoning depth this role runs at (see :mod:`unknown_agent.reasoning`).
    reasoning_mode: str = "high"
    #: Where the role sits in a pipeline (lower runs earlier).
    stage: int = 3
    #: Globs this role is allowed to own.  Overlap between two concurrently
    #: scheduled roles is a conflict the conductor refuses to run.
    owned_paths: tuple[str, ...] = ()
    #: Gates the conductor should run after this role finishes.
    verify_gates: tuple[str, ...] = ()
    #: Whether this role's failure blocks the run.
    required: bool = True
    #: Whether this role may itself delegate (reserved for the conductor).
    can_delegate: bool = False
    #: Style token used to colour the role in the UI.
    style: str = "ua.role.coder"
    glyph: str = "◆"
    #: Extra prompt guidance appended to the role's system prompt.
    guidance: str = ""
    #: Default per-role budget multipliers.
    max_steps: int = 24
    temperature: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "mission": self.mission,
            "outputs": list(self.outputs),
            "tools": list(self.tools),
            "deny_tools": list(self.deny_tools),
            "mutates": self.mutates,
            "reasoning_mode": self.reasoning_mode,
            "stage": self.stage,
            "owned_paths": list(self.owned_paths),
            "verify_gates": list(self.verify_gates),
            "required": self.required,
            "can_delegate": self.can_delegate,
            "max_steps": self.max_steps,
            "guidance": self.guidance,
        }


_FS_READ = ("read_file", "list_files", "glob_files", "project_tree", "search_code")
_REPO = ("inspect_project", "git_status", "git_diff", "git_log", "package_info")
_WORKTREE_READ = ("git_worktree_list", "git_worktree_status", "git_worktree_merge_preview")
_WORKTREE_EDIT = ("git_worktree_create", "git_worktree_commit")
_WORKTREE_MERGE = ("git_worktree_merge", "git_worktree_remove")
_WRITE = ("write_file", "apply_patch", "create_file", "move_path", "delete_path")
_EXEC = ("shell_run", "syntax_check", "read_logs")
_QUALITY = ("test_run", "lint_run", "typecheck_run", "build_run", "run_gates")
_LSP = (
    "lsp_status",
    "lsp_symbols",
    "lsp_definition",
    "lsp_references",
    "lsp_hover",
    "lsp_diagnostics",
    "lsp_rename_preview",
)
_BROWSER = ("browser_flow", "browser_screenshot", "browser_console")
_MEMORY = ("memory_search", "project_note", "procedure_add")

ROLES: dict[str, Role] = {}


def _role(**kwargs: Any) -> Role:
    role = Role(**kwargs)
    ROLES[role.id] = role
    return role


_role(
    id="planner",
    label="Planner",
    mission="Turn a request into a decision-complete plan: alternatives considered, tradeoffs scored, risks named, acceptance criteria executable.",
    outputs=("blueprint", "plan", "alternatives", "risks"),
    tools=_FS_READ + _REPO + _WORKTREE_READ + _MEMORY + ("procedure_list",),
    deny_tools=_WRITE + _EXEC,
    mutates=False,
    reasoning_mode="super",
    stage=0,
    verify_gates=(),
    style="ua.role.planner",
    glyph="◈",
    guidance=(
        "Consider at least two genuinely different approaches and score them; recommend one and say what "
        "would make you change your mind. Every acceptance criterion must be something a gate or a test can "
        "check — 'works well' is not a criterion."
    ),
    max_steps=12,
)

_role(
    id="inspector",
    label="Repo inspector",
    mission="Establish ground truth about the existing tree: stack, entry points, conventions, commands, risks, current gate status.",
    outputs=("repo_map", "conventions", "commands", "risks", "baseline_gates"),
    tools=_FS_READ + _REPO + _WORKTREE_READ + _LSP + _QUALITY[:1] + ("run_gates",) + _MEMORY,
    deny_tools=_WRITE,
    mutates=False,
    reasoning_mode="medium",
    stage=1,
    verify_gates=(),
    style="ua.role.inspector",
    glyph="◇",
    guidance=(
        "Report what is, not what should be. Where a convention is implied rather than written down, name the "
        "files that prove it. Record the baseline gate status so later workers can be blamed accurately."
    ),
    max_steps=10,
)

_role(
    id="coder",
    label="Code generator",
    mission="Implement the plan's behaviour in the repository's own idiom, with tests for the paths that can break.",
    outputs=("patch", "tests", "notes"),
    tools=(
        _FS_READ
        + _WRITE
        + _EXEC
        + ("test_run", "syntax_check")
        + _REPO[:1]
        + _WORKTREE_READ
        + _WORKTREE_EDIT
        + _LSP
    ),
    reasoning_mode="high",
    stage=2,
    owned_paths=("src/**", "app/**", "lib/**", "packages/**", "tests/**", "test/**"),
    verify_gates=("syntax", "tests"),
    style="ua.role.coder",
    glyph="▣",
    guidance=(
        "Follow the conventions the inspector recorded; do not introduce a second way of doing something the "
        "repo already does one way. Write the test that would fail without your change before you claim the "
        "change works."
    ),
    max_steps=30,
)

_role(
    id="refactorer",
    label="Refactor agent",
    mission="Improve structure without changing behaviour, proven by a green baseline before and after.",
    outputs=("baseline", "patch", "behaviour_proof"),
    tools=(
        _FS_READ
        + _WRITE
        + _EXEC
        + _QUALITY
        + _REPO[:3]
        + _WORKTREE_READ
        + _WORKTREE_EDIT
        + _LSP
    ),
    reasoning_mode="high",
    stage=3,
    owned_paths=("src/**", "app/**", "lib/**", "packages/**"),
    verify_gates=("tests", "lint", "diff"),
    style="ua.role.refactor",
    glyph="◫",
    guidance=(
        "Never refactor on red. Capture the baseline, add characterisation tests where coverage is thin, then "
        "move in small strides. A refactor commit must contain no behaviour change."
    ),
    max_steps=24,
)

_role(
    id="ux-finisher",
    label="UI/UX finisher",
    mission="Make the interface coherent and complete: design tokens, every state, hierarchy, contrast, responsive behaviour.",
    outputs=("tokens", "states", "screenshots", "a11y_notes"),
    tools=_FS_READ + _WRITE[:3] + _BROWSER + ("test_run",),
    reasoning_mode="high",
    stage=4,
    owned_paths=("**/*.css", "**/*.scss", "**/styles/**", "**/components/**", "**/ui/**", "**/*.tsx", "**/templates/**"),
    verify_gates=("build", "browser"),
    style="ua.role.ux",
    glyph="◉",
    guidance=(
        "Define tokens once and use only tokens afterwards. Every async surface needs loading, empty, error and "
        "disabled states. Look at the rendered result and compare before/after screenshots; do not ship a visual "
        "change you have not seen."
    ),
    max_steps=20,
)

_role(
    id="browser-validator",
    label="Browser validator",
    mission="Prove the running application works in a real browser: journeys completed, console clean, network clean, evidence captured.",
    outputs=("journey_result", "console_report", "network_report", "screenshots"),
    tools=_BROWSER + ("http_fetch", "read_logs", "shell_run"),
    deny_tools=_WRITE,
    mutates=False,
    reasoning_mode="medium",
    stage=5,
    verify_gates=("browser",),
    required=False,
    style="ua.role.browser",
    glyph="◎",
    guidance=(
        "Source code is not evidence. Complete the journey as a user would, at a narrow and a wide viewport. "
        "Any console error or failed request is a finding unless it is explained. Attach screenshots and the "
        "exact steps so a human can replay them."
    ),
    max_steps=14,
)

_role(
    id="verifier",
    label="Verifier",
    mission="Run the definition of done and report honestly: which gates are green, which are red, with the output attached.",
    outputs=("gate_report", "evidence"),
    tools=_QUALITY + _EXEC + _FS_READ[:2] + ("run_gates",),
    deny_tools=_WRITE,
    mutates=False,
    reasoning_mode="medium",
    stage=5,
    verify_gates=("syntax", "lint", "typecheck", "tests", "build", "diff"),
    style="ua.role.tooling",
    glyph="◍",
    guidance=(
        "Run the project's own commands, not invented ones. Never weaken a gate to make it pass: deleting a "
        "failing test or adding an ignore is a regression. If a gate cannot pass, say so and stop."
    ),
    max_steps=12,
)

_role(
    id="documenter",
    label="Documentation agent",
    mission="Write the documents a stranger needs, with every command verified by running it.",
    outputs=("readme", "runbook", "decision_records"),
    tools=_FS_READ + ("write_file", "apply_patch", "create_file") + ("shell_run",) + _REPO[:1],
    reasoning_mode="medium",
    stage=6,
    owned_paths=("README*", "docs/**", "CHANGELOG*", "CONTRIBUTING*", "*.md"),
    verify_gates=("syntax",),
    required=False,
    style="ua.role.docs",
    glyph="◌",
    guidance=(
        "Every command in the docs must be executed and must work. Record the decisions that are not obvious "
        "from the code, including what was rejected. Never document an aspiration as a feature."
    ),
    max_steps=12,
)

_role(
    id="releaser",
    label="Release agent",
    mission="Ship deliberately: version, changelog, artefact, dry-run, deploy, smoke-test, rollback recorded.",
    outputs=("version", "changelog", "artefact", "deploy_report", "rollback_plan"),
    tools=(
        _FS_READ[:1]
        + ("write_file", "apply_patch")
        + ("build_run", "shell_run", "git_status", "git_diff", "git_commit", "package_info")
        + _WORKTREE_READ
        + _WORKTREE_MERGE
    ),
    reasoning_mode="high",
    stage=7,
    owned_paths=("CHANGELOG*", "pyproject.toml", "package.json", "VERSION", "*.toml"),
    verify_gates=("tests", "build"),
    required=False,
    style="ua.role.release",
    glyph="◐",
    guidance=(
        "Never publish from a dirty tree. Bump the version by what actually changed. Dry-run the deploy before "
        "deploying, then smoke-test the deployed environment. Record how to revert within a minute."
    ),
    max_steps=14,
)

_role(
    id="tooling",
    label="Provider & tooling agent",
    mission="Keep the agent's own substrate healthy: providers, credentials, models, plugins, tool availability, browser install.",
    outputs=("diagnostics", "provider_state", "recommendation"),
    tools=_FS_READ[:2] + ("shell_run", "http_fetch", "package_info", "memory_search", "project_note"),
    deny_tools=_WRITE,
    mutates=False,
    reasoning_mode="medium",
    stage=1,
    required=False,
    style="ua.role.tooling",
    glyph="◒",
    guidance=(
        "Diagnose with evidence: run the check, show the output, name the fix. Never print a secret. Prefer "
        "the smallest configuration change that restores capability."
    ),
    max_steps=10,
)

ROLE_ORDER: tuple[str, ...] = (
    "planner",
    "inspector",
    "coder",
    "refactorer",
    "ux-finisher",
    "browser-validator",
    "verifier",
    "documenter",
    "releaser",
    "tooling",
)

#: Aliases a user may type in config or on the CLI.
ROLE_ALIASES: dict[str, str] = {
    "plan": "planner",
    "planning": "planner",
    "inspect": "inspector",
    "repo": "inspector",
    "code": "coder",
    "codegen": "coder",
    "implement": "coder",
    "refactor": "refactorer",
    "ux": "ux-finisher",
    "ui": "ux-finisher",
    "design": "ux-finisher",
    "browser": "browser-validator",
    "validate": "browser-validator",
    "verify": "verifier",
    "qa": "verifier",
    "docs": "documenter",
    "documentation": "documenter",
    "release": "releaser",
    "deploy": "releaser",
    "ship": "releaser",
    "provider": "tooling",
    "providers": "tooling",
    "tools": "tooling",
}


def get_role(role_id: str) -> Role:
    key = (role_id or "").strip().lower()
    key = ROLE_ALIASES.get(key, key)
    if key in ROLES:
        return ROLES[key]
    raise KeyError(role_id)


def has_role(role_id: str) -> bool:
    try:
        get_role(role_id)
    except KeyError:
        return False
    return True


def resolve_role_id(role_id: str) -> str | None:
    try:
        return get_role(role_id).id
    except KeyError:
        return None


def roles_payload() -> list[dict[str, Any]]:
    """All roles as data, in pipeline order — for CLI, docs and the TUI."""

    return [ROLES[key].to_dict() for key in sorted(ROLE_ORDER, key=lambda k: (ROLES[k].stage, k))]


def roles_for_pattern(pattern: str) -> list[str]:
    """The roles a given orchestration shape uses by default."""

    table: dict[str, tuple[str, ...]] = {
        "solo": ("coder",),
        "pipeline": ("inspector", "planner", "coder", "verifier"),
        "fanout": ("inspector", "planner", "coder", "ux-finisher", "documenter", "verifier"),
        "graph": ("planner", "inspector", "coder", "ux-finisher", "browser-validator", "documenter", "verifier"),
        "escalate": ("planner", "inspector", "coder", "verifier", "refactorer"),
        "greenfield": ("planner", "inspector", "coder", "ux-finisher", "browser-validator", "verifier", "documenter"),
        "ui": ("inspector", "ux-finisher", "browser-validator", "verifier"),
        "api": ("inspector", "planner", "coder", "verifier", "documenter"),
        "debug": ("inspector", "coder", "verifier"),
        "refactor": ("inspector", "verifier", "refactorer", "verifier"),
        "release": ("verifier", "documenter", "releaser"),
        "audit": ("inspector", "verifier", "documenter"),
    }
    return list(table.get(pattern, table["pipeline"]))
