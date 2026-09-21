"""Procedural memory: reusable workflows and behavioural rules.

Procedures are Markdown documents with optional YAML front-matter.  They are the
difference between an agent that re-derives a workflow every time and one that
learns the house rules of a team: "how we ship a migration", "how we add an
endpoint", "never edit generated clients".

Locations (project overrides user):
* ``<repo>/.unknown/procedures/*.md``
* ``~/.config/unknown-agent/procedures/*.md``
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from unknown_agent.observability.logging import get_logger

log = get_logger("memory.procedures")

FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(slots=True)
class Procedure:
    name: str
    body: str
    scope: str = "project"
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    path: Path | None = None
    updated_at: float = 0.0

    def matches(self, task: str) -> bool:
        haystack = (task or "").lower()
        if not self.triggers:
            return self.name.replace("-", " ") in haystack
        return any(re.search(trigger, haystack, re.IGNORECASE) for trigger in self.triggers)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "scope": self.scope,
            "description": self.description,
            "triggers": self.triggers,
            "path": str(self.path) if self.path else None,
            "body": self.body,
        }


class ProcedureLibrary:
    """Loads procedures from one or more directories."""

    def __init__(self, *directories: Path | str | None) -> None:
        self.directories = [Path(item) for item in directories if item]
        self._cache: tuple[float, list[Procedure]] | None = None

    # -- reading -----------------------------------------------------------
    def list(self, *, refresh: bool = False) -> list[Procedure]:
        stamp = max((path.stat().st_mtime for directory in self.directories if directory.exists() for path in directory.glob("*.md")), default=0.0)
        if self._cache and not refresh and self._cache[0] >= stamp:
            return self._cache[1]
        procedures: list[Procedure] = []
        seen: set[str] = set()
        for directory in self.directories:
            if not directory.is_dir():
                continue
            scope = "project" if directory.name == "procedures" and ".unknown" in str(directory) else "project" if ".unknown" in str(directory) else "user"
            for path in sorted(directory.glob("*.md")):
                procedure = self._load(path, scope=scope)
                if procedure is None or procedure.name in seen:
                    continue
                seen.add(procedure.name)
                procedures.append(procedure)
        self._cache = (stamp, procedures)
        return procedures

    def _load(self, path: Path, *, scope: str) -> Procedure | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            return None
        metadata: dict[str, object] = {}
        body = text
        match = FRONT_MATTER_RE.match(text)
        if match:
            try:
                parsed = yaml.safe_load(match.group(1)) or {}
                if isinstance(parsed, dict):
                    metadata = parsed
            except yaml.YAMLError:
                log.warning("procedure %s has invalid front-matter; ignoring metadata", path.name)
            body = text[match.end() :]
        name = str(metadata.get("name") or path.stem).strip()
        triggers = metadata.get("triggers") or metadata.get("when") or []
        if isinstance(triggers, str):
            triggers = [triggers]
        return Procedure(
            name=name,
            body=body.strip(),
            scope=str(metadata.get("scope") or scope),
            description=str(metadata.get("description") or body.strip().splitlines()[0] if body.strip() else ""),
            triggers=[str(item) for item in triggers],
            path=path,
            updated_at=path.stat().st_mtime,
        )

    def match(self, task: str, *, limit: int = 4) -> list[Procedure]:
        matched = [procedure for procedure in self.list() if procedure.matches(task)]
        return matched[:limit]

    # -- writing -----------------------------------------------------------
    def save(self, name: str, body: str, *, scope: str = "project", description: str = "", triggers: list[str] | None = None, directory: Path | None = None) -> Path:
        target_dir = directory or (self.directories[0] if self.directories else Path.cwd())
        target_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"procedure-{int(time.time())}"
        front = {"name": name, "description": description or body.strip().splitlines()[0][:120] if body.strip() else "", "scope": scope}
        if triggers:
            front["triggers"] = triggers
        path = target_dir / f"{slug}.md"
        path.write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + body.strip() + "\n", encoding="utf-8")
        self._cache = None
        return path

    def delete(self, name: str) -> bool:
        for procedure in self.list(refresh=True):
            if procedure.name == name and procedure.path:
                procedure.path.unlink(missing_ok=True)
                self._cache = None
                return True
        return False

    def render(self) -> str:
        procedures = self.list()
        if not procedures:
            return "No procedures stored."
        blocks = []
        for procedure in procedures:
            header = f"{procedure.name} [{procedure.scope}]"
            if procedure.triggers:
                header += f"  triggers: {', '.join(procedure.triggers)}"
            blocks.append(f"{header}\n{' ' * 2}" + procedure.body.replace("\n", "\n  ")[:1200])
        return "\n\n".join(blocks)


__all__ = ["Procedure", "ProcedureLibrary"]
