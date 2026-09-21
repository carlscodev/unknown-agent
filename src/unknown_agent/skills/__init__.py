"""Skills: named bundles of instructions, tool grants and gates.

A skill is the unit of *competence*.  ``test-smith`` does not mean "you may run
tests"; it means "when a change lands, write the test that would have caught the
regression, run it, and treat a red run as a blocker".  Skills therefore carry
instructions (what good looks like), tool grants (what may be touched), gates
(what must pass) and triggers (when the skill is relevant at all).

Two layers:

:mod:`~unknown_agent.skills.pack`
    The built-in catalogue — fourteen skills and six packs (``default``, ``web``,
    ``api``, ``quality``, ``delivery``, ``all``, ``none``).  These ship with the
    product and are the reason a fresh install is useful.

:mod:`~unknown_agent.skills.loader`
    Discovery and precedence.  Sources are built-in, user
    (``~/.config/unknown-agent/skills/``), project (``.unknown/skills/``) and
    plugin.  Precedence is ``disable > enable > pack membership > default``.

Security posture, deliberately: **project skills are parsed, not trusted.**  A
``.unknown/skills/*.toml`` in a cloned repository can add instructions and gates to
the prompt, so it is loaded with ``trusted=False``; it may not replace a builtin
unless it declares ``overrides = "<builtin-id>"`` (a collision is otherwise ignored
*and reported*); its tool grants stay requests that policy decides on; and the
loader fails soft on malformed files rather than letting a bad file in someone
else's repo break your setup.  :meth:`SkillSet.untrusted` exposes exactly which
skills crossed that boundary, so a UI can show them differently.

:func:`~unknown_agent.skills.loader.resolve_skills` returns a
:class:`~unknown_agent.skills.pack.SkillSet`, which is what the prompt builder and
the orchestrator consume: ``relevant(task)`` filters by trigger, and
``prompt_section()`` renders a token-budgeted block of instructions.
"""

from __future__ import annotations

from unknown_agent.skills.loader import (
    SKILL_FILE_TEMPLATE,
    LoadedSkill,
    SkillLoader,
    SkillSource,
    packs_payload,
    parse_skill_file,
    resolve_skills,
    skill_from_mapping,
    skills_dir,
)
from unknown_agent.skills.pack import (
    PACKS,
    Skill,
    SkillSet,
    builtin_skills,
    expand_pack,
    get_skill,
    skill_ids,
)

__all__ = [
    "PACKS",
    "SKILL_FILE_TEMPLATE",
    "LoadedSkill",
    "Skill",
    "SkillLoader",
    "SkillSet",
    "SkillSource",
    "builtin_skills",
    "expand_pack",
    "get_skill",
    "packs_payload",
    "parse_skill_file",
    "resolve_skills",
    "skill_from_mapping",
    "skill_ids",
    "skills_dir",
]
