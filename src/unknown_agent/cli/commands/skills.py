"""``ua skills`` — see, scaffold and audit the craft guidance a run carries.

Skills are *data*, never code: a TOML file with instructions, the tools they would
like and the gates they consider part of finished work.  That is exactly why this
group exists — a user should be able to answer "what is steering my agent right
now, where did it come from, and did any of it fail to parse?" without reading a
directory listing or a config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

import functools
from collections.abc import Callable
from typing import TypeVar

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import ExitCode, UnknownAgentError, UsageError
from unknown_agent.skills import (
    SKILL_FILE_TEMPLATE,
    SkillLoader,
    SkillSet,
    expand_pack,
    get_skill,
    packs_payload,
    skill_ids,
)
from unknown_agent.ui.console import output
from unknown_agent.ui.renderers import data_table, kv_table, summary_panel

app = typer.Typer(
    name="skills",
    help="Craft guidance the agent runs with: list, inspect, scaffold, audit.",
    no_args_is_help=True,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)

#: Origins a user chose to install.  Anything else arrived as data with a clone.
_TRUSTED = ("builtin", "plugin", "user")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _runtime(ctx: typer.Context) -> Runtime:
    runtime = ctx.obj
    if runtime is None:  # pragma: no cover - Typer always provides it
        raise UsageError("No runtime on the Typer context.")
    return runtime


def _machine(runtime: Runtime, json_flag: bool) -> bool:
    """True when output must be JSON: global machine mode or a local ``--json``.

    ``ua --json skills list`` and ``ua skills list --json`` must behave the same;
    the local flag simply switches the output manager for the rest of the command.
    """

    if json_flag and not runtime.is_machine:
        output.mode = "json"
    return runtime.is_machine or json_flag


#: Shared ``--json`` option; every read command in this group takes it.
JSON_FLAG = typer.Option(False, "--json", help="Machine-readable JSON on stdout.")

F = TypeVar("F", bound=Callable[..., None])


def _guarded(func: F) -> F:
    """Render :class:`UnknownAgentError` as a panel + exit code, even under test.

    ``main()`` catches these at the top level, but ``CliRunner`` (and any embedder
    calling the Typer app directly) bypasses ``main`` — the error must not escape
    as a raw traceback with exit code 1.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            func(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except UnknownAgentError as exc:
            from unknown_agent.ui.errors import render_error

            raise typer.Exit(render_error(exc)) from None

    return wrapper  # type: ignore[return-value]


def _project_root(loaded: Any) -> Path:
    """The repository root, whether or not ``.unknown/`` exists yet.

    ``loaded.project_root`` is only set when the project was recognised; the skills
    loader wants a root it can resolve ``.unknown/skills`` against, so fall back to
    the parent of the (always present) project dir.
    """

    return Path(loaded.project_root or loaded.paths.project_dir.parent)


def _loader(loaded: Any) -> SkillLoader:
    config = loaded.settings.skills
    extra = [Path(item) for item in (getattr(config, "extra_dirs", None) or [])]
    return SkillLoader(
        user_dir=loaded.paths.user_skills_dir,
        project_dir=_project_root(loaded),
        extra_dirs=extra,
    )


def _resolve(ctx: typer.Context) -> tuple[Runtime, Any, SkillSet, list[Any]]:
    """Runtime + loaded settings + the resolved set + the raw discovery sources.

    ``discover()`` is called separately from ``resolve()`` because only the sources
    carry parse errors, and "this file did not load" is precisely what
    ``ua skills check`` exists to report.
    """

    runtime = _runtime(ctx)
    loaded = runtime.load()
    loader = _loader(loaded)
    sources = loader.discover()
    resolved = loader.resolve(loaded.settings.skills)
    return runtime, loaded, resolved, sources


def _origin(skill: Any) -> str:
    return str(getattr(skill, "source", "") or "?")


def _trust_marker(skill: Any) -> str:
    """How to label an origin without implying a verdict.

    ``untrusted`` means "arrived as data from a directory a clone could have
    populated" — not "malicious".  Saying so in one word matters: a scary label
    teaches users to ignore it.
    """

    return "trusted" if _origin(skill) in _TRUSTED else "project"


def _skill_row(skill: Any) -> tuple[str, ...]:
    return (
        skill.id,
        skill.label,
        skill.category,
        _origin(skill),
        _trust_marker(skill),
        str(skill.priority),
    )


_HEADERS = ["skill", "label", "category", "origin", "trust", "prio"]


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
@app.command("list")
@_guarded
def list_skills(
    ctx: typer.Context,
    all_skills: bool = typer.Option(False, "--all", help="Include skills that are disabled or failed to load."),
    category: str = typer.Option("", "--category", help="Only this category (craft, repair, verification, ...)."),
    json_output: bool = JSON_FLAG,
) -> None:
    """Show the skills active in this project, strongest first.

    Examples:
      ua skills list
      ua skills list --all
      ua skills list --category verification --json
    """

    machine = _machine(_runtime(ctx), json_output)
    runtime, loaded, resolved, sources = _resolve(ctx)
    rows = [skill for skill in resolved.skills if not category or skill.category == category]
    rows.sort(key=lambda skill: (-skill.priority, skill.id))

    if machine:
        payload: dict[str, Any] = {
            "skills": [skill.to_dict() for skill in rows],
            "disabled": list(resolved.disabled),
            "unresolved": list(resolved.unresolved),
            "packs": list(resolved.packs),
            "requested_tools": resolved.tools(),
            "requested_gates": resolved.gates(),
        }
        if all_skills:
            payload["errors"] = [
                {"path": item.path, "error": item.error}
                for source in sources
                for item in source.skills
                if item.error
            ]
        runtime.emit_json(payload)
        return

    if not rows:
        output.print("[ua.muted]no active skills[/ua.muted]")
        output.print(
            "[ua.muted]enable a pack with `ua config set skills.pack '[\"default\"]'`, "
            "or scaffold one with `ua skills new`[/ua.muted]"
        )
        return

    output.print(data_table(list(_HEADERS), [_skill_row(skill) for skill in rows], title=f"active skills ({len(rows)})"))

    if all_skills:
        errors = [(item.path, item.error) for source in sources for item in source.skills if item.error]
        if resolved.disabled:
            output.print(f"[ua.dim]disabled: {', '.join(resolved.disabled)}[/ua.dim]")
        for path, error in errors:
            output.print(f"[ua.err]failed to load[/ua.err] {path}: {error}")
    if resolved.unresolved:
        output.print(f"[ua.warn]named in config but not found: {', '.join(resolved.unresolved)}[/ua.warn]")
    untrusted = resolved.untrusted()
    if untrusted:
        output.print(
            f"[ua.dim]{len(untrusted)} skill(s) came from the project directory "
            f"({', '.join(skill.id for skill in untrusted)}) — see `ua skills check`[/ua.dim]"
        )


# --------------------------------------------------------------------------- #
# packs
# --------------------------------------------------------------------------- #
@app.command("packs")
@_guarded
def list_packs(ctx: typer.Context, json_output: bool = JSON_FLAG) -> None:
    """Show the skill packs you can enable in ``\\[skills] pack``.

    Examples:
      ua skills packs
      ua skills packs --json
    """

    runtime = _runtime(ctx)
    machine = _machine(runtime, json_output)
    loaded = runtime.load()
    active = [str(item) for item in (loaded.settings.skills.pack or [])]
    packs = packs_payload()

    if machine:
        runtime.emit_json({"packs": packs, "active": active})
        return

    rows = [
        (
            pack["key"],
            pack["label"],
            str(pack["count"]),
            "active" if pack["key"] in active else "",
            ", ".join(pack["skills"][:4]) + ("…" if pack["count"] > 4 else ""),
        )
        for pack in packs
    ]
    output.print(data_table(["pack", "label", "skills", "", "members"], rows, title=f"skill packs ({len(rows)})"))
    for pack in packs:
        output.print(f"[ua.dim]{pack['key']}[/ua.dim]: {pack['description']}")


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #
@app.command("show")
@_guarded
def show_skill(
    ctx: typer.Context,
    skill_id: str = typer.Argument(..., help="Skill id, e.g. bug-hunter."),
    json_output: bool = JSON_FLAG,
) -> None:
    """Full detail for one skill: instructions, grants, gates, origin.

    Falls back to the shipped catalog when the skill exists but is not active here,
    and says so — "not active in this project" and "does not exist" are different
    facts and the user needs to know which one they hit.

    Examples:
      ua skills show bug-hunter
      ua skills show my-skill --json
    """

    machine = _machine(_runtime(ctx), json_output)
    _runtime_obj, _loaded, resolved, _sources = _resolve(ctx)
    skill = resolved.get(skill_id)
    active = skill is not None
    if skill is None:
        try:
            skill = get_skill(skill_id)
        except KeyError:
            skill = None
    if skill is None:
        known = ", ".join(resolved.ids()[:8]) or ", ".join(skill_ids()[:8])
        raise UsageError(f"No skill named {skill_id!r}.", hint=f"Known skills: {known}… — run `ua skills list --all`.")

    if machine:
        payload = skill.to_dict()
        payload["active"] = active
        _runtime_obj.emit_json(payload)
        return

    rows = [
        ("id", skill.id),
        ("label", skill.label),
        ("summary", skill.summary),
        ("category", skill.category),
        ("version", skill.version),
        ("author", skill.author),
        ("priority", str(skill.priority)),
        ("origin", _origin(skill)),
        ("trust", _trust_marker(skill)),
        ("active here", "yes" if active else "no (not enabled in this project)"),
        ("path", str(skill.path) if skill.path else "shipped with the product"),
        ("tools requested", ", ".join(skill.tools) or "none"),
        ("gates required", ", ".join(skill.gates) or "none"),
        ("workflows", ", ".join(skill.workflows) or "none"),
        ("triggers", ", ".join(skill.triggers[:8]) or "none"),
        ("in default pack", "yes" if skill.in_default_pack else "no"),
    ]
    output.print(kv_table(rows, title=f"{skill.label} ({skill.id})"))
    if skill.instructions:
        output.blank()
        output.print(summary_panel("instructions", [(skill.id, skill.instructions.strip())]))
    if not active:
        output.print(f"[ua.warn]not active[/ua.warn] — enable it with `ua config set skills.enable '[\"{skill.id}\"]'`")


# --------------------------------------------------------------------------- #
# new
# --------------------------------------------------------------------------- #
def _render_template(name: str) -> str:
    """Substitute the id/label into the shipped template.

    The template is the documentation: it explains that skills are parsed and never
    executed, and that tool grants are still policy decisions.  Rewriting only the
    identity fields keeps that text intact.
    """

    slug = name.strip().lower().replace(" ", "-").replace("_", "-") or "my-skill"
    label = name.strip() or "My skill"
    text = SKILL_FILE_TEMPLATE
    text = text.replace('id = "my-skill"', f'id = "{slug}"', 1)
    text = text.replace('label = "My skill"', f'label = "{label}"', 1)
    return text


@app.command("new")
@_guarded
def new_skill(
    ctx: typer.Context,
    name: str = typer.Argument("", help="Skill id (lowercase, hyphenated). Empty writes the bare template."),
    scope: str = typer.Option("project", "--scope", help="project | user | <path>."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Scaffold a skill file in the project or user skills directory.

    Examples:
      ua skills new api-contracts
      ua skills new my-discipline --scope user
      ua skills new experiment --scope ./skills-scratch --force
    """

    runtime = _runtime(ctx)
    loaded = runtime.load()
    if scope == "project":
        target_dir = loaded.paths.project_skills_dir
    elif scope == "user":
        target_dir = loaded.paths.user_skills_dir
    else:
        target_dir = Path(scope).expanduser()
        if target_dir.name != "skills":
            target_dir = target_dir / "skills"

    slug = name.strip().lower().replace(" ", "-").replace("_", "-") or "my-skill"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{slug}.toml"
    if target.exists() and not force:
        raise UsageError(f"{target} already exists.", hint="Pass --force to overwrite it.")
    target.write_text(_render_template(name), encoding="utf-8")

    if runtime.is_machine:
        runtime.emit_json({"created": str(target), "id": slug, "scope": scope})
        return

    output.print(f"[ua.ok]created[/ua.ok] {target}")
    if scope == "project":
        output.print(
            "[ua.dim]project skills are read as data from this repository, so they are reported as "
            "untrusted origin — that is a label, not a verdict. Their tool grants are still "
            # Brackets are escaped: Rich parses these strings as markup, so a bare
            # "[policy]" is swallowed as a style tag and the sentence ends "decided
            # by ." — silent, which is why it survives review.
            "decided by \\[policy].[/ua.dim]"
        )
    output.print("[ua.muted]picked up on the next run — check with `ua skills list --all`[/ua.muted]")


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
@app.command("check")
@_guarded
def check_skills(ctx: typer.Context, json_output: bool = JSON_FLAG) -> None:
    """Audit the skill set: parse failures, dangling names, untrusted origins.

    Exit code is 4 (CONFIG) when something named in config could not be loaded, so
    CI can fail on a skill set that silently degraded; 0 otherwise.

    Examples:
      ua skills check
      ua skills check --json
    """

    machine = _machine(_runtime(ctx), json_output)
    runtime, loaded, resolved, sources = _resolve(ctx)
    config = loaded.settings.skills

    errors = [
        {"path": item.path, "error": item.error}
        for source in sources
        for item in source.skills
        if item.error
    ]
    source_errors = [{"kind": source.kind, "path": str(source.path or ""), "error": source.error} for source in sources if source.error]
    untrusted = resolved.untrusted()
    requested_tools = resolved.tools()
    requested_gates = resolved.gates()
    broken = bool(errors or source_errors or resolved.unresolved)

    if machine:
        runtime.emit_json(
            {
                "ok": not broken,
                "active": resolved.ids(),
                "disabled": list(resolved.disabled),
                # Bare names for assertions; the annotated form (why it failed)
                # rides alongside so nothing is lost to machine consumers.
                "unresolved": [item.split(":", 1)[0].strip() for item in resolved.unresolved],
                "unresolved_details": list(resolved.unresolved),
                "parse_errors": errors,
                "source_errors": source_errors,
                "untrusted": [
                    {"id": skill.id, "path": str(skill.path or ""), "origin": _origin(skill)} for skill in untrusted
                ],
                "requested_tools": requested_tools,
                "requested_gates": requested_gates,
                "allow_project_skills": bool(config.allow_project_skills),
                "max_active": int(config.max_active),
            }
        )
        raise typer.Exit(ExitCode.CONFIG if broken else ExitCode.OK)

    output.print(
        summary_panel(
            "skills check",
            [
                ("active", f"{len(resolved.skills)} skill(s)"),
                ("packs", ", ".join(resolved.packs) or "none"),
                ("disabled", ", ".join(resolved.disabled) or "none"),
                ("tools requested", ", ".join(requested_tools) or "none"),
                ("gates required", ", ".join(requested_gates) or "none"),
                ("project skills allowed", "yes" if config.allow_project_skills else "no"),
                ("max active", str(config.max_active)),
            ],
        )
    )

    for entry in source_errors:
        output.print(f"[ua.err]source error[/ua.err] {entry['kind']} {entry['path']}: {entry['error']}")
    for entry in errors:
        output.print(f"[ua.err]failed to parse[/ua.err] {entry['path']}: {entry['error']}")
    for name in resolved.unresolved:
        output.print(f"[ua.warn]not found[/ua.warn] {name!r} is named in \\[skills] but no such skill exists")

    if untrusted:
        output.blank()
        output.print(f"[ua.warn]{len(untrusted)} skill(s) from the project directory[/ua.warn] (untrusted origin):")
        for skill in untrusted:
            grants = ", ".join(skill.tools) or "no tools"
            output.print(f"  [ua.dim]-[/ua.dim] {skill.id} — {grants} — {skill.path or 'unknown path'}")
        output.print(
            "[ua.dim]untrusted means the content arrived with a clone, not that it is malicious. "
            "Instructions are prompt text, so review them the way you would review a "
            "contributor's PR; grants are still policy decisions. Set "
            "\\[skills] allow_project_skills = false to ignore them entirely.[/ua.dim]"
        )

    if resolved.disabled:
        output.blank()
        output.print(f"[ua.dim]disabled: {', '.join(resolved.disabled)}[/ua.dim]")

    if not broken:
        output.print("[ua.ok]✓[/ua.ok] every configured skill loaded")
    raise typer.Exit(ExitCode.CONFIG if broken else ExitCode.OK)


__all__ = ["app"]
