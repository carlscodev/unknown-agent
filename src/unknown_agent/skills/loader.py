"""Skill discovery and resolution.

Skills arrive from four places, in this precedence order (later wins on id):

1. **builtin**  — :mod:`unknown_agent.skills.pack`, shipped with the product;
2. **user**     — ``<config dir>/skills/*.toml``, the user's own craft notes;
3. **project**  — ``<repo>/.unknown/skills/*.toml``, shared with the team via git;
4. **plugin**   — contributed by a loaded plugin through ``context.add_skill``.

Project skills are **untrusted input**: a repository you cloned can contain one.
Three consequences, all enforced here rather than left to the caller:

* they are parsed as data and never executed;
* they may not silently replace a builtin — a collision is ignored *and reported*
  unless the file declares ``overrides = "<builtin-id>"``;
* their tool grants stay requests that the policy layer decides on, and their
  origin path is always reported (``SkillSet.untrusted()``).

Loading is fail-soft: a malformed skill file produces a warning in
``SkillSet.unresolved`` and is skipped, because a broken file in someone else's
repo must never stop the agent from starting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from unknown_agent.observability.logging import get_logger
from unknown_agent.skills.pack import PACKS, Skill, SkillSet, builtin_skills, expand_pack, get_skill

log = get_logger("skills.loader")

__all__ = [
    "SkillSource",
    "skills_dir",
    "SkillLoader",
    "LoadedSkill",
    "parse_skill_file",
    "skill_from_mapping",
    "resolve_skills",
    "packs_payload",
    "SKILL_FILE_TEMPLATE",
]


@dataclass(slots=True)
class LoadedSkill:
    """A skill plus where it came from — what ``ua skills list`` reports."""

    skill: Skill
    origin: str  # builtin | user | project | plugin
    path: str = ""
    error: str | None = None
    trusted: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = self.skill.to_dict()
        payload["origin"] = self.origin
        payload["trusted"] = self.trusted
        if self.path:
            payload["path"] = self.path
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class SkillSource:
    """One discovery root."""

    kind: str
    path: Path | None = None
    skills: list[LoadedSkill] = field(default_factory=list)
    error: str | None = None


SKILL_FILE_TEMPLATE = """\
# Unknown Agent skill definition
# Drop this file in <repo>/.unknown/skills/ or ~/.config/unknown-agent/skills/
# Skills are data: they are parsed, never executed. Tool grants are still
# checked against [policy], so a skill cannot widen permissions by existing.

id = "my-skill"                 # unique, lowercase, hyphenated
label = "My skill"
summary = "One line: what this skill makes the agent better at."
category = "craft"              # understanding | verification | repair | craft | architecture | delivery | safety
version = "1.0.0"
author = "you"
priority = 5                    # higher is listed first in the prompt section
default_enabled = true

# Craft guidance injected into the system prompt when the skill is active.
instructions = \"\"\"
Describe the discipline, not the steps. What must be true before this work counts
as done? What are the common ways it goes wrong? What evidence proves it worked?
\"\"\"

tools = ["read_file", "write_file", "test_run"]     # requested, policy still decides
gates = ["tests", "syntax"]                          # what "done" means for this skill
workflows = []                                       # ids from `ua workflows list`
fact_kinds = []                                      # memory kinds this skill records
triggers = ["my-domain", "my keyword"]               # task text that activates it

# Only if this file is meant to REPLACE a built-in skill of the same id.  Without
# it, a project skill colliding with a builtin is ignored and reported.
# overrides = "test-smith"
"""


def parse_skill_file(path: Path, *, origin: str = "project") -> LoadedSkill:
    """Parse one skill definition file (TOML, or YAML if TOML fails)."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedSkill(
            skill=Skill(id=path.stem, label=path.stem, summary="", source=origin, path=str(path)),
            origin=origin,
            path=str(path),
            error=f"unreadable: {exc}",
            trusted=False,
        )

    payload: Mapping[str, Any] | None = None
    try:
        import tomllib

        payload = tomllib.loads(raw)
    except Exception:  # noqa: BLE001 - fall back to YAML, then report
        try:
            import yaml

            loaded = yaml.safe_load(raw)
            payload = loaded if isinstance(loaded, Mapping) else None
        except Exception as exc:  # noqa: BLE001
            return LoadedSkill(
                skill=Skill(id=path.stem, label=path.stem, summary="", source=origin, path=str(path)),
                origin=origin,
                path=str(path),
                error=f"could not parse as TOML or YAML: {exc}",
                trusted=False,
            )

    if not isinstance(payload, Mapping):
        return LoadedSkill(
            skill=Skill(id=path.stem, label=path.stem, summary="", source=origin, path=str(path)),
            origin=origin,
            path=str(path),
            error="file does not contain a mapping",
            trusted=False,
        )
    return skill_from_mapping(payload, origin=origin, path=str(path))


def skill_from_mapping(payload: Mapping[str, Any], *, origin: str = "project", path: str = "") -> LoadedSkill:
    """Build a :class:`Skill` from a parsed mapping, rejecting nonsense loudly."""

    # The id must be declared, not inferred from the filename.  Inferring it means a
    # file with a typo'd key parses fine and becomes a skill whose id matches
    # nothing — a skill that silently never activates.  Naming the file in the error
    # is what makes the failure fixable in ten seconds.
    skill_id = str(payload.get("id") or "").strip().lower().replace(" ", "-")
    if not skill_id:
        return LoadedSkill(
            skill=Skill(id="invalid", label="invalid", summary="", source=origin, path=path),
            origin=origin,
            path=path,
            error=f"{Path(path).name or 'skill'}: no id declared", 
            trusted=False,
        )
    label = str(payload.get("label") or skill_id.replace("-", " ").title())
    summary = str(payload.get("summary") or "").strip()
    instructions = str(payload.get("instructions") or "").strip()
    # Trust follows the threat model, which is a *repository*, not a directory:
    # builtin and plugin content was installed by the user, a user-dir skill was
    # written by the user, and an extra dir was named by the user on the command
    # line.  Only ``project`` can arrive without the user choosing it — it comes
    # with whatever they cloned.
    trusted = origin != "project"
    try:
        skill = Skill(
            id=skill_id,
            label=label,
            summary=summary or label,
            instructions=instructions,
            category=str(payload.get("category") or "general"),
            tools=tuple(str(item) for item in (payload.get("tools") or ())),
            gates=tuple(str(item) for item in (payload.get("gates") or ())),
            workflows=tuple(str(item) for item in (payload.get("workflows") or ())),
            fact_kinds=tuple(str(item) for item in (payload.get("fact_kinds") or ())),
            triggers=tuple(str(item).lower() for item in (payload.get("triggers") or ())),
            default_enabled=bool(payload.get("default_enabled", True)),
            in_default_pack=bool(payload.get("in_default_pack", False)),
            source=origin,
            path=path,
            version=str(payload.get("version") or "0.1.0"),
            author=str(payload.get("author") or ("Unknown Agent" if origin == "builtin" else "unknown")),
            priority=int(payload.get("priority", 5)),
            settings=dict(payload.get("settings") or {}),
            overrides=str(payload.get("overrides") or "").strip().lower(),
        )
    except Exception as exc:  # noqa: BLE001 - a bad skill must never stop startup
        return LoadedSkill(
            skill=Skill(id=skill_id, label=label, summary=summary, source=origin, path=path),
            origin=origin,
            path=path,
            error=f"{type(exc).__name__}: {exc}",
            trusted=trusted,
        )
    return LoadedSkill(skill=skill, origin=origin, path=path, trusted=trusted)


def skills_dir(root: Path | str | None, *, kind: str = "project") -> Path | None:
    """Public form of the directory resolution, used by the CLI to print paths."""

    return _skills_dir(root, kind)


#: Candidate subdirectories, tried in order, per source kind.  Callers hold
#: whatever level of the tree is convenient for them — a project root, ``~``,
#: ``~/.config``, or the leaf ``skills`` directory itself — and guessing wrong
#: yields zero skills with no error, which is the quietest possible failure.  So
#: every plausible spelling resolves to the same directory.
_CONVENTIONS: dict[str, tuple[str, ...]] = {
    "project": (".unknown/skills", "skills"),
    "user": (".config/unknown-agent/skills", "unknown-agent/skills", "skills"),
    "extra": ("skills",),
}


def _has_skill_files(path: Path) -> bool:
    try:
        return any(path.glob("*.toml")) or any(path.glob("*.y*ml"))
    except OSError:  # pragma: no cover - unreadable directory
        return False


#: Files that mean "this directory is a project or a config root", not "this
#: directory holds skills".  Without them, a project root containing
#: ``pyproject.toml`` looks exactly like a skills directory, and the loader parses
#: the build manifest as a skill — which produces a warning about a file the user
#: never meant to be a skill, on every single run.
_ROOT_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    ".git",
    ".unknown",
)


def _looks_like_root(path: Path) -> bool:
    return any((path / marker).exists() for marker in _ROOT_MARKERS)


def _skills_dir(root: Path | str | None, kind: str) -> Path | None:
    """Resolve ``root`` to the directory that actually holds skill files.

    A directory is treated as the skills directory itself only when it says so:
    it is named ``skills``, or it holds skill files *and* no project marker.  A
    project or config root always resolves through the convention
    (``.unknown/skills``, ``.config/unknown-agent/skills``), even when that
    directory does not exist yet — "no skills" is the right answer for a project
    that never created one, and it must not come with a parse warning about
    ``pyproject.toml``.
    """

    if root is None:
        return None
    path = Path(root).expanduser()
    is_skills_dir = path.name == "skills" or (_has_skill_files(path) and not _looks_like_root(path))
    if is_skills_dir:
        return path
    conventions = _CONVENTIONS.get(kind, ())
    for convention in conventions:
        candidate = path / convention
        if candidate.is_dir():
            return candidate
    if _looks_like_root(path) and conventions:
        # A project or config root with no skills directory yet: point at where one
        # would live.  Returning the root itself would make the scanner glob
        # ``*.toml`` and parse ``pyproject.toml`` as a skill, warning about a file
        # the user never meant to be one — on every run, in every project.
        return path / conventions[0]
    return path


class SkillLoader:
    """Discovers skills and resolves them against ``[skills]`` config."""

    def __init__(
        self,
        *,
        user_dir: Path | None = None,
        project_dir: Path | None = None,
        extra_dirs: Sequence[Path] = (),
    ) -> None:
        self.user_dir = _skills_dir(user_dir, "user")
        self.project_dir = _skills_dir(project_dir, "project")
        self.extra_dirs = [_skills_dir(path, "extra") or path for path in extra_dirs]
        self.plugin_skills: list[Skill] = []

    # -- discovery --------------------------------------------------------- #
    def add_plugin_skill(self, skill: Skill) -> None:
        self.plugin_skills.append(skill)

    def discover(self) -> list[SkillSource]:
        sources: list[SkillSource] = []
        sources.append(
            SkillSource(
                kind="builtin",
                skills=[LoadedSkill(skill=skill, origin="builtin", trusted=True) for skill in builtin_skills()],
            )
        )
        for kind, root in (("user", self.user_dir), ("project", self.project_dir)):
            if root is None:
                continue
            sources.append(self._scan(kind, root))
        for index, root in enumerate(self.extra_dirs):
            sources.append(self._scan(f"extra:{index}", root))
        if self.plugin_skills:
            sources.append(
                SkillSource(
                    kind="plugin",
                    skills=[
                        LoadedSkill(
                            skill=Skill(
                                id=skill.id,
                                label=skill.label,
                                summary=skill.summary,
                                instructions=skill.instructions,
                                category=skill.category,
                                tools=skill.tools,
                                gates=skill.gates,
                                workflows=skill.workflows,
                                fact_kinds=skill.fact_kinds,
                                triggers=skill.triggers,
                                default_enabled=skill.default_enabled,
                                source="plugin",
                                path=skill.path,
                                version=skill.version,
                                author=skill.author,
                                priority=skill.priority,
                                settings=dict(skill.settings),
                            ),
                            origin="plugin",
                            path=skill.path,
                        )
                        for skill in self.plugin_skills
                    ],
                )
            )
        return sources

    def _scan(self, kind: str, root: Path) -> SkillSource:
        source = SkillSource(kind=kind, path=root)
        if not root.exists():
            return source
        if root.is_file():
            source.skills.append(parse_skill_file(root, origin=kind))
            return source
        try:
            files = sorted(list(root.glob("*.toml")) + list(root.glob("*.yaml")) + list(root.glob("*.yml")))
        except OSError as exc:
            source.error = str(exc)
            return source
        for path in files:
            source.skills.append(parse_skill_file(path, origin=kind))
        return source

    # -- resolution -------------------------------------------------------- #
    def resolve(self, config: Any) -> SkillSet:
        """Apply ``[skills]`` config to the discovered skills.

        Precedence for the enabled decision:
        ``disable`` list > ``enable`` list > pack membership > skill default.
        """

        enabled_flag = bool(getattr(config, "enabled", True))
        packs = [str(item) for item in (getattr(config, "pack", None) or ["default"])]
        enable = {str(item).strip().lower() for item in (getattr(config, "enable", None) or [])}
        disable = {str(item).strip().lower() for item in (getattr(config, "disable", None) or [])}
        allow_project = bool(getattr(config, "allow_project_skills", True))
        auto_discover = bool(getattr(config, "auto_discover", True))
        max_active = int(getattr(config, "max_active", 12) or 12)

        sources = self.discover() if auto_discover else [
            SkillSource(kind="builtin", skills=[LoadedSkill(skill=skill, origin="builtin") for skill in builtin_skills()])
        ]

        # ``pack = ["none"]`` means "no skills at all"; ``pack = []`` means "fall back
        # to each skill's own default".  Those are different answers and must not
        # collapse into the same behaviour.
        explicit_none = any(pack.strip().lower() == "none" for pack in packs)
        packed: set[str] = set()
        for pack in packs:
            packed.update(expand_pack(pack))
        if explicit_none:
            packed = set()

        by_id: dict[str, LoadedSkill] = {}
        unresolved: list[str] = []
        for source in sources:
            if source.kind == "project" and not allow_project:
                continue
            for loaded in source.skills:
                if loaded.error:
                    unresolved.append(f"{loaded.skill.id}: {loaded.error}")
                    continue
                existing = by_id.get(loaded.skill.id)
                if existing is not None and existing.origin == "builtin" and loaded.origin == "project":
                    # A project may replace a builtin only by declaring it.  Without
                    # that declaration the builtin wins and the file is reported, so
                    # a repository you cloned cannot quietly rewrite the instructions
                    # your agent works under.
                    if loaded.skill.overrides != existing.skill.id:
                        unresolved.append(
                            f"{loaded.skill.id}: project skill ignored - it would replace a builtin; "
                            f'set overrides = "{existing.skill.id}" to mean it'
                        )
                        continue
                    loaded.skill = Skill(
                        id=loaded.skill.id,
                        label=loaded.skill.label,
                        summary=loaded.skill.summary,
                        instructions=loaded.skill.instructions or existing.skill.instructions,
                        category=loaded.skill.category,
                        tools=loaded.skill.tools or existing.skill.tools,
                        gates=loaded.skill.gates or existing.skill.gates,
                        workflows=loaded.skill.workflows or existing.skill.workflows,
                        fact_kinds=loaded.skill.fact_kinds or existing.skill.fact_kinds,
                        triggers=loaded.skill.triggers or existing.skill.triggers,
                        default_enabled=loaded.skill.default_enabled,
                        source="project",
                        path=loaded.path,
                        version=loaded.skill.version,
                        author=loaded.skill.author,
                        priority=loaded.skill.priority,
                        settings=dict(loaded.skill.settings),
                        overrides=loaded.skill.overrides,
                    )
                by_id[loaded.skill.id] = loaded

        for pack in packs:
            key = pack.strip().lower()
            if key not in PACKS:
                unresolved.append(f"pack {key!r}: unknown pack (known: {', '.join(PACKS)})")
        for wanted in sorted(enable | packed):
            if wanted in PACKS:
                continue
            if wanted not in by_id:
                unresolved.append(f"{wanted}: not found in any skill source")

        active: list[Skill] = []
        disabled: list[str] = []
        for skill_id, loaded in by_id.items():
            skill = loaded.skill
            if not enabled_flag or skill_id in disable:
                disabled.append(skill_id)
                continue
            if skill_id in enable:
                active.append(skill)
                continue
            if skill_id in packed:
                active.append(skill)
                continue
            if skill.default_enabled and (not packs or loaded.origin != "builtin"):
                # Packs govern the *builtin catalogue*: they are the answer to
                # "which of the shipped skills do I want".  A file the user or the
                # team placed on disk is already an explicit act, so requiring a
                # pack to list it too would make dropping a skill in do nothing —
                # the quietest possible failure.  Builtins with no pack configured
                # fall back to their own default.
                active.append(skill)
                continue
            disabled.append(skill_id)

        active.sort(key=lambda skill: (-skill.priority, skill.id))
        truncated = False
        if len(active) > max_active:
            disabled.extend(skill.id for skill in active[max_active:])
            active = active[:max_active]
            truncated = True
        if truncated:
            unresolved.append(f"more than {max_active} skills were enabled; the lowest-priority ones were dropped")

        return SkillSet(
            skills=active,
            disabled=sorted(set(disabled) - {skill.id for skill in active}),
            unresolved=unresolved,
            packs=list(packs),
        )


def resolve_skills(settings: Any, *, user_dir: Path | None = None, project_dir: Path | None = None) -> SkillSet:
    """Convenience entry point used by the runtime.

    Accepts either a :class:`~unknown_agent.config.models.Settings` (it takes
    ``settings.skills``) or a ``SkillsConfig`` directly, because callers that
    already hold the section should not have to rebuild a root object around it.
    Passing a section and silently getting the defaults back would be the worst
    possible failure mode: every ``enable``/``disable``/``pack`` choice ignored.
    """

    if settings is None:
        config: Any = None
    elif hasattr(settings, "pack") or hasattr(settings, "enable"):
        config = settings
    else:
        config = getattr(settings, "skills", None)
    if config is None:
        config = type("SkillsCfg", (), {"enabled": True, "pack": ["default"], "enable": [], "disable": [], "allow_project_skills": True, "auto_discover": True, "max_active": 12})()
    extra = [Path(item) for item in (getattr(config, "extra_dirs", None) or [])]
    loader = SkillLoader(user_dir=user_dir, project_dir=project_dir, extra_dirs=extra)
    return loader.resolve(config)


def packs_payload() -> list[dict[str, Any]]:
    """Pack metadata for ``ua skills packs`` and onboarding."""

    out: list[dict[str, Any]] = []
    for key, pack in PACKS.items():
        members = expand_pack(key)
        out.append(
            {
                "key": key,
                "label": pack["label"],
                "description": pack["description"],
                "skills": members,
                "count": len(members),
            }
        )
    return out
