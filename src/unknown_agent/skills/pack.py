"""Skills: packaged expertise the agent can be given.

A *skill* is not a prompt fragment and not a plugin.  It is a named bundle of

* **instructions** — how to do this kind of work properly (the craft knowledge),
* **tool grants** — which tools this kind of work needs,
* **gates** — which quality gates define "done" for it,
* **workflows** — guided step sequences to offer when the task matches,
* **memory kinds** — what is worth remembering afterwards,

with a declared ``source`` (builtin / user / project / plugin) and an explicit
enabled state.  Skills are how Unknown Agent feels alive on first run instead of
empty: :data:`unknown_agent.skills.pack.BUILTIN_SKILLS` ships fourteen of them,
ten of which are on by default.

Security rule (not a preference): a project-directory skill is *untrusted input*.
It is parsed, never executed, its tool grants are intersected with ``[policy]``,
and loading it is reported in ``ua skills list`` with its origin path so a
malicious repository cannot quietly widen the agent's permissions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "Skill",
    "SkillSet",
    "PACKS",
    "expand_pack",
    "builtin_skills",
    "get_skill",
    "skill_ids",
]


@dataclass(frozen=True, slots=True)
class Skill:
    """One unit of packaged expertise."""

    id: str
    label: str
    summary: str
    #: Longer craft guidance injected into the system prompt when active.
    instructions: str = ""
    category: str = "general"
    #: Tool names this skill needs.  Grants are *requests*: the policy layer still
    #: decides, so a skill can never escalate permissions by existing.
    tools: tuple[str, ...] = ()
    #: Quality gates this skill considers part of "done".
    gates: tuple[str, ...] = ()
    #: Workflow ids this skill can offer (from :mod:`unknown_agent.workflows`).
    workflows: tuple[str, ...] = ()
    #: Memory fact kinds it records.
    fact_kinds: tuple[str, ...] = ()
    #: Task-text signals that make this skill relevant right now.
    triggers: tuple[str, ...] = ()
    #: On unless explicitly disabled.
    default_enabled: bool = True
    #: Members of the ``default`` starter pack.
    in_default_pack: bool = True
    source: str = "builtin"
    path: str = ""
    version: str = "1.0.0"
    author: str = "Unknown Agent"
    #: Relative priority when several skills are active (higher = listed first).
    priority: int = 5
    #: Extra structured settings a skill may carry.
    settings: Mapping[str, Any] = field(default_factory=dict)
    #: The id of a builtin this skill deliberately replaces.
    #:
    #: A project skill that collides with a builtin id is *ignored and reported*
    #: unless it declares the replacement here.  Silent replacement would let a
    #: cloned repository rewrite the instructions the agent works under — a
    #: prompt-injection surface with no visible diff and no visible error.
    overrides: str = ""

    def matches(self, text: str) -> int:
        """How strongly a task text triggers this skill (0 = not relevant)."""

        lowered = (text or "").lower()
        return sum(2 if " " in trigger else 1 for trigger in self.triggers if trigger in lowered)

    def prompt_block(self, *, max_chars: int = 700) -> str:
        """The slice of this skill that goes into the system prompt."""

        body = self.instructions.strip()
        if len(body) > max_chars:
            body = body[: max_chars - 1].rstrip() + "…"
        head = f"[{self.id}] {self.summary}"
        return f"{head}\n{body}" if body else head

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "summary": self.summary,
            "category": self.category,
            "instructions": self.instructions,
            "tools": list(self.tools),
            "gates": list(self.gates),
            "workflows": list(self.workflows),
            "fact_kinds": list(self.fact_kinds),
            "triggers": list(self.triggers),
            "default_enabled": self.default_enabled,
            "in_default_pack": self.in_default_pack,
            "source": self.source,
            "path": self.path,
            "version": self.version,
            "author": self.author,
            "priority": self.priority,
            "settings": dict(self.settings),
        }


@dataclass(slots=True)
class SkillSet:
    """The resolved, active set of skills for a session."""

    skills: list[Skill] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    packs: list[str] = field(default_factory=list)

    def ids(self) -> list[str]:
        return [skill.id for skill in self.skills]

    def get(self, skill_id: str) -> Skill | None:
        for skill in self.skills:
            if skill.id == skill_id:
                return skill
        return None

    def tools(self) -> list[str]:
        """Union of requested tools, de-duplicated, in skill priority order."""

        seen: list[str] = []
        for skill in self.skills:
            for tool in skill.tools:
                if tool not in seen:
                    seen.append(tool)
        return seen

    def gates(self) -> list[str]:
        seen: list[str] = []
        for skill in self.skills:
            for gate in skill.gates:
                if gate not in seen:
                    seen.append(gate)
        return seen

    def workflows(self) -> list[str]:
        seen: list[str] = []
        for skill in self.skills:
            for workflow in skill.workflows:
                if workflow not in seen:
                    seen.append(workflow)
        return seen

    #: Origins the user chose: shipped with the product, contributed by a plugin
    #: they installed, written by them in their own config dir, or named explicitly
    #: via ``extra_dirs``.  Only ``project`` can arrive uninvited, with a clone.
    TRUSTED_SOURCES = ("builtin", "plugin", "user")

    def trusted(self) -> list[Skill]:
        """Skills whose instructions came from somewhere the user installed."""

        return [skill for skill in self.skills if skill.source in self.TRUSTED_SOURCES]

    def untrusted(self) -> list[Skill]:
        """Skills parsed from the project directory.

        Untrusted does not mean rejected: it means the content arrived as data from
        a directory a cloned repository could have populated.  Their tool grants are
        still requests that policy decides on, and their origin path is always
        reported — but a UI can show them differently, and ``allow_project_skills =
        false`` turns them off entirely.
        """

        return [skill for skill in self.skills if skill.source not in self.TRUSTED_SOURCES]

    def relevant(self, task: str, *, limit: int = 6) -> list[tuple[Skill, int]]:
        """Skills the task actually triggers, strongest first."""

        scored = [(skill, skill.matches(task)) for skill in self.skills]
        scored = [pair for pair in scored if pair[1] > 0]
        scored.sort(key=lambda pair: (-pair[1], -pair[0].priority, pair[0].id))
        return scored[:limit]

    def prompt_section(self, *, task: str = "", max_skills: int = 6, max_chars: int = 3600) -> str:
        """The system-prompt contribution of the active skills.

        Task-relevant skills come first and get more room; the rest are listed by
        name so the model knows the capability exists without paying tokens for
        instructions it will not use.
        """

        if not self.skills:
            return ""
        relevant = [skill for skill, _score in self.relevant(task, limit=max_skills)] if task else []
        primary = relevant or sorted(self.skills, key=lambda skill: (-skill.priority, skill.id))[:max_skills]
        lines: list[str] = ["ACTIVE SKILLS — apply the craft guidance for whichever is relevant to this task."]
        budget = max_chars
        for skill in primary:
            block = skill.prompt_block(max_chars=min(700, max(160, budget // max(1, len(primary)))))
            if budget - len(block) < 0:
                break
            budget -= len(block)
            lines.append(f"- {block}")
        others = [skill.id for skill in self.skills if skill not in primary]
        if others:
            lines.append(f"Also available: {', '.join(others)}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skills": [skill.to_dict() for skill in self.skills],
            "disabled": self.disabled,
            "unresolved": self.unresolved,
            # Which skills crossed the trust boundary, with the file they came from.
            # A UI can flag these; ``allow_project_skills = false`` refuses them.
            "untrusted": [
                {"id": skill.id, "source": skill.source, "path": skill.path} for skill in self.untrusted()
            ],
            "packs": self.packs,
            "tools": self.tools(),
            "gates": self.gates(),
            "workflows": self.workflows(),
        }


# --------------------------------------------------------------------------- #
# packs: named bundles for one-flag enablement
# --------------------------------------------------------------------------- #
PACKS: dict[str, dict[str, Any]] = {
    "default": {
        "label": "Starter pack",
        "description": "The ten skills that make a first run useful: repo mapping, tests, gates, debugging, refactoring, UI finish, docs, releases, git hygiene and dependency care.",
    },
    "web": {
        "label": "Web product",
        "description": "Adds browser verification, performance profiling and API contracts — the web-product craft set.",
        "skills": ("browser-verifier", "performance-profiler", "api-contractor", "ui-finisher"),
    },
    "api": {
        "label": "API & services",
        "description": "Contract design, versioning, security audit and performance targets for backend work.",
        "skills": ("api-contractor", "security-auditor", "performance-profiler", "test-smith"),
    },
    "quality": {
        "label": "Quality & safety",
        "description": "Everything that proves the work: tests, gates, browser evidence, security, performance.",
        "skills": ("test-smith", "gate-keeper", "browser-verifier", "security-auditor", "performance-profiler", "bug-hunter"),
    },
    "delivery": {
        "label": "Delivery",
        "description": "Shipping and handing over: releases, documentation, git hygiene.",
        "skills": ("release-pilot", "docs-scribe", "git-hygienist"),
    },
    "all": {"label": "Everything", "description": "Every builtin skill.", "skills": "*"},
    "none": {"label": "Nothing", "description": "Disable every skill; the agent works from its base prompt only.", "skills": ()},
}


def _skill(**kwargs: Any) -> Skill:
    return Skill(**kwargs)


# --------------------------------------------------------------------------- #
# the shipped pack
# --------------------------------------------------------------------------- #
BUILTIN_SKILLS: tuple[Skill, ...] = (
    _skill(
        id="repo-cartographer",
        label="Repo cartographer",
        summary="Read a repository like someone who will have to change it: entry points, conventions, ownership, risk.",
        category="understanding",
        instructions=(
            "Before proposing a change, produce a map: the entry points, the module boundaries, the framework "
            "conventions already in use, and the files where a change would be risky. Prefer reading the tests "
            "and the build configuration over reading the README — they cannot lie. Record discovered "
            "conventions (naming, error handling, layering) as project memory so the next run does not "
            "re-derive them, and never introduce a second way of doing something the repo already does one way."
        ),
        tools=("inspect_project", "read_file", "search_code", "glob_files", "list_files", "project_tree", "git_status", "git_log", "package_info"),
        gates=("diff",),
        fact_kinds=("convention", "structure"),
        triggers=("inspect", "understand", "explore", "map", "what does", "how does", "where is", "codebase", "unfamiliar"),
        priority=9,
        in_default_pack=True,
    ),
    _skill(
        id="test-smith",
        label="Test smith",
        summary="Turn acceptance criteria into executable checks, and know which tests are worth writing.",
        category="verification",
        instructions=(
            "Every acceptance criterion that can be executed should be. Write tests that fail for the right "
            "reason: assert behaviour, not implementation. Cover the boundaries that actually break in "
            "production — empty, null, duplicate, concurrent, unauthorised, too large, wrong timezone — before "
            "adding breadth. A test that cannot fail is worse than no test, because it buys false confidence. "
            "Match the repo's existing test style and runner; never introduce a second test framework."
        ),
        tools=("test_run", "write_file", "apply_patch", "read_file", "search_code", "syntax_check"),
        gates=("tests", "syntax"),
        fact_kinds=("test-strategy",),
        triggers=("test", "coverage", "spec", "tdd", "pytest", "vitest", "jest", "acceptance"),
        priority=9,
        in_default_pack=True,
    ),
    _skill(
        id="gate-keeper",
        label="Gate keeper",
        summary="Configure and enforce the definition of done; refuse to declare success on red.",
        category="verification",
        instructions=(
            "'Done' means the gates are green with evidence attached, not that the code looks plausible. Detect "
            "the project's own commands (test, lint, typecheck, build) and run those instead of inventing new "
            "ones. When a gate fails, fix the cause; never weaken the gate to make it pass — deleting a failing "
            "test or adding an ignore comment is a regression, not a fix. If a gate cannot pass, report it "
            "explicitly with the output and stop."
        ),
        tools=("run_gates", "test_run", "lint_run", "typecheck_run", "build_run", "syntax_check", "read_logs"),
        gates=("syntax", "lint", "typecheck", "tests", "build", "diff"),
        fact_kinds=("definition-of-done",),
        triggers=("verify", "quality", "gate", "definition of done", "ci", "lint", "typecheck", "build"),
        priority=8,
        in_default_pack=True,
    ),
    _skill(
        id="bug-hunter",
        label="Bug hunter",
        summary="Reproduce first, hypothesise second, fix third, prove it cannot come back fourth.",
        category="repair",
        instructions=(
            "Never patch a symptom you have not reproduced. Sequence: reproduce → narrow → hypothesise → fix → "
            "regression test. State the hypothesis before editing, and say what evidence would falsify it. "
            "Prefer the smallest change that makes the failing case pass and keeps the suite green. Boundary "
            "conditions (empty, null, overflow, timezone, ordering, concurrency) cause most 'impossible' bugs — "
            "check them before suspecting the framework. Finish with a test that fails on the old code."
        ),
        tools=("read_logs", "search_code", "read_file", "test_run", "apply_patch", "syntax_check", "browser_console"),
        gates=("tests", "syntax"),
        workflows=("bug-fix",),
        fact_kinds=("incident",),
        triggers=("bug", "fix", "broken", "failing", "crash", "error", "regression", "flaky", "500", "wrong"),
        priority=9,
        in_default_pack=True,
    ),
    _skill(
        id="refactor-surgeon",
        label="Refactor surgeon",
        summary="Change structure without changing behaviour, and prove the behaviour did not move.",
        category="repair",
        instructions=(
            "Never refactor on red: capture a green baseline first, and if coverage is thin, add characterisation "
            "tests that pin current behaviour before touching anything. Move in small strides, each independently "
            "green. Separate refactoring commits from behaviour-change commits — a reviewer must be able to tell "
            "them apart. Name what is explicitly out of scope so the refactor cannot grow. Finish by diffing "
            "against the baseline and confirming nothing unintended changed."
        ),
        tools=("apply_patch", "read_file", "search_code", "test_run", "run_gates", "git_diff", "lint_run", "typecheck_run"),
        gates=("tests", "lint", "diff"),
        workflows=("refactor",),
        triggers=("refactor", "restructure", "clean up", "simplify", "decouple", "extract", "duplication", "modularize"),
        priority=7,
        in_default_pack=True,
    ),
    _skill(
        id="ui-finisher",
        label="UI/UX finisher",
        summary="Take a working interface to one that feels designed: tokens, states, hierarchy, accessibility.",
        category="craft",
        instructions=(
            "Design is a system, not a set of choices: define tokens (colour, spacing scale, type scale, radii, "
            "shadows) once and use only tokens afterwards. Every async surface needs loading, empty, error and "
            "disabled states — a missing state is a bug, not a detail. Check contrast against the real surface, "
            "not a swatch. Verify focus order by keyboard. Look at the rendered result in a browser and compare "
            "before/after screenshots; do not ship a visual change you have not seen."
        ),
        tools=("browser_screenshot", "browser_flow", "browser_console", "write_file", "apply_patch", "read_file"),
        gates=("browser", "build"),
        workflows=("ui-polish",),
        fact_kinds=("design-tokens",),
        triggers=("ui", "ux", "design", "layout", "css", "style", "polish", "landing", "dashboard", "responsive", "accessible"),
        priority=7,
        in_default_pack=True,
    ),
    _skill(
        id="docs-scribe",
        label="Docs scribe",
        summary="Write the document a stranger needs: runnable quickstart, honest architecture notes, recorded decisions.",
        category="delivery",
        instructions=(
            "Documentation is verified by execution: every command in the README must be run and must work. Lead "
            "with the quickstart (clone → install → run → test in as few commands as possible), then what the "
            "system is, then the boundaries. Record decisions that are not obvious from the code, including what "
            "was rejected and why — that is the part a new maintainer cannot reconstruct. Never document an "
            "aspiration as a feature."
        ),
        tools=("write_file", "read_file", "inspect_project", "shell_run", "project_tree"),
        gates=("syntax",),
        workflows=("docs-handover",),
        fact_kinds=("decision-record",),
        triggers=("docs", "document", "readme", "handover", "onboard", "runbook", "contributing", "adr"),
        priority=6,
        in_default_pack=True,
    ),
    _skill(
        id="release-pilot",
        label="Release pilot",
        summary="Ship deliberately: version, changelog, artefact, deploy, verify in the real environment, rollback ready.",
        category="delivery",
        instructions=(
            "A release is a sequence with a rollback plan at every step, not a command. Bump the version by what "
            "actually changed (semver is a promise to callers), write the changelog from the diff rather than from "
            "memory, build the artefact, dry-run the deploy, deploy, then smoke-test the deployed environment. "
            "Record how to revert within a minute and who decides. Never publish from a dirty tree."
        ),
        tools=("build_run", "git_status", "git_diff", "git_commit", "shell_run", "write_file", "package_info"),
        gates=("tests", "build"),
        workflows=("release",),
        fact_kinds=("release",),
        triggers=("release", "ship", "deploy", "publish", "version", "tag", "roll out", "production"),
        priority=6,
        in_default_pack=True,
    ),
    _skill(
        id="git-hygienist",
        label="Git hygiene",
        summary="Small reviewable commits, messages that explain why, never a dirty surprise.",
        category="delivery",
        instructions=(
            "Commit in units a reviewer can hold in their head: one logical change, tests with the code they "
            "cover. The subject line says what changed; the body says why, and names the ticket or decision. "
            "Never commit generated files, secrets, or a lockfile change nobody intended. Check ``git status`` "
            "before and after every batch of edits so nothing is left behind, and never force-push a shared "
            "branch."
        ),
        tools=("git_status", "git_diff", "git_log", "git_commit", "read_file"),
        gates=("diff",),
        triggers=("commit", "branch", "git", "pr", "pull request", "merge", "rebase"),
        priority=5,
        in_default_pack=True,
    ),
    _skill(
        id="dependency-steward",
        label="Dependency steward",
        summary="Add dependencies reluctantly, pin them deliberately, and know what each one costs.",
        category="delivery",
        instructions=(
            "Every dependency is a long-term liability: ask what it replaces, how much of it will be used, "
            "whether it is maintained, and what happens when it is not. Prefer the standard library and the "
            "framework's own facilities over a new package for a small need. Pin exact versions in the lockfile, "
            "record why each non-obvious dependency exists, and audit for known vulnerabilities before shipping."
        ),
        tools=("package_info", "shell_run", "read_file", "write_file", "apply_patch"),
        gates=("build",),
        fact_kinds=("dependency",),
        triggers=("dependency", "package", "install", "upgrade", "version", "lockfile", "npm", "pip", "cargo"),
        priority=5,
        in_default_pack=True,
    ),
    _skill(
        id="browser-verifier",
        label="Browser verifier",
        summary="Prove the app works by driving a real browser: journeys, console, network, screenshots.",
        category="verification",
        instructions=(
            "Source code is not evidence; a rendered page is. Drive the real browser against the running dev "
            "server: complete the core journey as a user would, capture console messages and failed requests, and "
            "treat any console error or 4xx/5xx as a gate failure unless it is explained. Screenshot the states "
            "that matter (initial, loading, empty, error, success) and check the layout at both a narrow and a "
            "wide viewport. Record the URL, the steps and the evidence so a human can replay it."
        ),
        tools=("browser_flow", "browser_screenshot", "browser_console", "http_fetch", "run_gates"),
        gates=("browser",),
        workflows=("ui-polish",),
        fact_kinds=("browser-evidence",),
        triggers=("browser", "screenshot", "e2e", "playwright", "render", "page", "journey", "console error"),
        priority=8,
        in_default_pack=False,
    ),
    _skill(
        id="api-contractor",
        label="API contractor",
        summary="Design the contract first: schema, versioning, errors, pagination, idempotency.",
        category="architecture",
        instructions=(
            "Write the contract before the implementation: request and response schemas, the error envelope with "
            "machine-readable codes, pagination and filtering conventions, and the versioning policy. Every "
            "mutating endpoint needs an idempotency story. Authorisation is checked on every endpoint by a test, "
            "not by review. Never change a published field's meaning; add a field, deprecate the old one, and "
            "document the migration."
        ),
        tools=("write_file", "apply_patch", "http_fetch", "test_run", "read_file", "search_code"),
        gates=("tests", "typecheck"),
        workflows=("integration",),
        fact_kinds=("api-contract",),
        triggers=("api", "endpoint", "rest", "graphql", "schema", "openapi", "webhook", "contract", "versioning"),
        priority=7,
        in_default_pack=False,
    ),
    _skill(
        id="security-auditor",
        label="Security auditor",
        summary="Look for the ways this system leaks, breaks or exposes data — with evidence, not vibes.",
        category="safety",
        instructions=(
            "Enumerate the attack surface first: endpoints, uploads, deserialisation, shell-outs, outbound "
            "requests, secrets. Then check authorisation on every endpoint (IDOR is the most common real-world "
            "bug), injection at every query and command construction site, secret exposure in logs and error "
            "responses, and SSRF wherever a URL comes from a user. Each finding gets a severity, a reproduction, "
            "and a fix with a test that would have caught it. Report honestly: an unfixed high-severity finding "
            "is a blocked release, not a footnote."
        ),
        tools=("search_code", "read_file", "test_run", "inspect_project", "package_info"),
        gates=("tests", "lint"),
        workflows=("security-audit",),
        fact_kinds=("security-finding",),
        triggers=("security", "audit", "vulnerability", "harden", "auth", "permissions", "pii", "gdpr", "injection", "secret"),
        priority=7,
        in_default_pack=False,
    ),
    _skill(
        id="performance-profiler",
        label="Performance profiler",
        summary="Measure before optimising, and leave a number behind that proves the change worked.",
        category="craft",
        instructions=(
            "Never optimise without a measurement: record the baseline (p50/p95 latency, bundle size, query count, "
            "memory) before changing anything, and the same numbers after. Fix the dominant cost first — usually "
            "an N+1 query, an unindexed lookup, an oversized bundle or a blocking third-party call. Set a budget "
            "and make it a gate so the win cannot be silently undone later. Report the delta, not the intention."
        ),
        tools=("shell_run", "read_logs", "http_fetch", "browser_console", "test_run", "search_code"),
        gates=("tests", "browser"),
        fact_kinds=("performance-baseline",),
        triggers=("performance", "slow", "latency", "optimise", "optimize", "bundle", "n+1", "cache", "speed"),
        priority=6,
        in_default_pack=False,
    ),
)

_SKILL_INDEX: dict[str, Skill] = {skill.id: skill for skill in BUILTIN_SKILLS}


def builtin_skills() -> list[Skill]:
    """The shipped pack, in priority order."""

    return sorted(BUILTIN_SKILLS, key=lambda skill: (-skill.priority, skill.id))


def skill_ids() -> list[str]:
    return [skill.id for skill in builtin_skills()]


def get_skill(skill_id: str) -> Skill:
    try:
        return _SKILL_INDEX[(skill_id or "").strip().lower()]
    except KeyError:
        raise KeyError(skill_id) from None


def expand_pack(name: str, *, known: Iterable[str] = ()) -> list[str]:
    """Resolve a pack name to skill ids."""

    key = (name or "").strip().lower()
    available = list(known) or skill_ids()
    if key in {"", "default"}:
        return [skill.id for skill in BUILTIN_SKILLS if skill.in_default_pack]
    pack = PACKS.get(key)
    if pack is None:
        return [key] if key in available else []
    members = pack.get("skills")
    if members == "*":
        return list(available)
    # An empty member list is a *statement*, not an omission: ``none`` means "no
    # skills at all".  Falling back to the default pack here would turn the one
    # setting that disables skills into the one setting that enables them.
    return [str(item) for item in (members or ())]
