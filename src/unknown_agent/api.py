"""Programmatic API for embedding Unknown Agent.

The CLI is the primary product surface, but everything it does is available as a
library: CI pipelines, editors, dashboards and the future web console all use
this module rather than shelling out to the binary.

```python
import asyncio
from unknown_agent.api import UnknownAgent

async def main() -> None:
    agent = UnknownAgent(cwd="/path/to/repo")          # or cwd=".", provider="offline"
    result = await agent.run("add a /health endpoint with a test")
    print(result.state.status, result.state.files_changed)
    await agent.aclose()

asyncio.run(main())
```

Stability: everything exported here follows semantic versioning.  Internals
(modules without a leading ``api`` surface) may change between minor releases.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unknown_agent.config.loader import LoadedSettings, load_settings
from unknown_agent.core.loop import LoopHooks
from unknown_agent.core.models import Plan
from unknown_agent.core.runner import RunRequest, RunResult, Runner
from unknown_agent.errors import ExitCode, UnknownAgentError
from unknown_agent.validation.engine import ValidationEngine
from unknown_agent.version import __version__

__all__ = ["UnknownAgent", "RunRequest", "RunResult", "InspectionReport", "ExitCode", "UnknownAgentError", "__version__"]


@dataclass(slots=True)
class InspectionReport:
    """Result of a read-only repository inspection."""

    kind: str
    languages: list[str]
    frameworks: list[str]
    commands: dict[str, str]
    risks: list[str]
    root: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "languages": self.languages,
            "frameworks": self.frameworks,
            "commands": self.commands,
            "risks": self.risks,
            "root": self.root,
        }


class UnknownAgent:
    """Embeddable agent session bound to one project directory."""

    def __init__(
        self,
        *,
        cwd: str | Path = ".",
        config_file: str | Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        read_only: bool = False,
        definition_of_done: str | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.cwd = Path(cwd).expanduser().resolve()
        self.loaded: LoadedSettings = load_settings(self.cwd, config_file=Path(config_file) if config_file else None)
        self.provider = provider
        self.model = model
        self.read_only = read_only
        self.definition_of_done = definition_of_done
        self.max_steps = max_steps

    # ------------------------------------------------------------------ #
    async def run(self, task: str, *, kind: str = "run", hooks: LoopHooks | None = None, event_sink: Any = None) -> RunResult:
        """Execute a task end-to-end and return the structured result."""

        request = RunRequest(
            task=task,
            kind=kind,
            read_only=self.read_only,
            non_interactive=True,
            provider=self.provider,
            model=self.model,
            definition_of_done=self.definition_of_done,
            max_steps=self.max_steps,
        )
        runner = Runner(self.loaded, request=request, hooks=hooks, event_sink=event_sink)
        await runner.setup()
        return await runner.execute()

    async def plan(self, task: str) -> Plan:
        """Produce a typed :class:`~unknown_agent.core.models.Plan` without touching a file.

        Raises :class:`~unknown_agent.errors.UnknownAgentError` when planning fails
        (for example a provider without credentials), rather than returning an empty
        object that hides the problem.
        """

        result = await self.run(task, kind="plan")
        if result.state.plan is None:
            raise UnknownAgentError("The provider returned no plan for this task.")
        return result.state.plan

    async def plan_dict(self, task: str) -> dict[str, Any]:
        """JSON-serialisable form of :meth:`plan`, for callers that just want data."""

        return (await self.plan(task)).to_dict()

    def inspect(self) -> InspectionReport:
        """Deterministic repository intelligence (no model call)."""

        from unknown_agent.context.project import load_project_context

        root = Path(self.loaded.project_root or self.cwd)
        project = load_project_context(root, self.loaded.settings)
        return InspectionReport(
            kind=project.kind,
            languages=project.languages,
            frameworks=project.frameworks,
            commands=dict(project.commands or {}),
            risks=project.risks,
            root=str(project.root),
        )

    async def verify(self, *, gates: list[str] | None = None, base_url: str | None = None) -> dict[str, Any]:
        """Run quality gates and return the report as a dictionary."""

        from unknown_agent.observability.events import EventBus

        root = Path(self.loaded.project_root or self.cwd)
        engine = ValidationEngine(self.loaded.settings, project_root=root, events=EventBus())
        report = await engine.run(gates=gates, base_url=base_url)
        return report.to_dict()

    async def aclose(self) -> None:
        """Release cached resources.  Safe to call multiple times."""

        return None

    async def __aenter__(self) -> UnknownAgent:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def run_sync(task: str, **kwargs: Any) -> RunResult:
    """Synchronous convenience wrapper (scripts, notebooks)."""

    agent = UnknownAgent(**{key: kwargs.pop(key) for key in ("cwd", "provider", "model", "read_only") if key in kwargs})
    return asyncio.run(agent.run(task, **kwargs))
