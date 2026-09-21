"""Tool contract.

A tool is a typed, self-describing capability with an explicit risk level.  The
same object is used by the agent loop, by ``ua tools call`` and by plugin authors,
which is why the contract is strict:

* ``parameters`` is a declarative schema - it becomes the model-facing JSON
  schema, the CLI's ``--arg`` parser and the validation rules, from one source;
* ``risk`` drives policy (``read`` runs freely, ``write``/``execute`` may need a
  confirmation, ``destructive`` needs one and a backup);
* ``run`` must be side-effect-explicit and never raise for domain failures -
  it returns ``ToolResult.failure(...)`` so the agent can observe and adapt.

Tools receive a :class:`ToolContext` instead of globals, so they are testable in
isolation and safe to run concurrently.
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from unknown_agent.config.models import Settings
from unknown_agent.config.paths import AppPaths
from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.errors import ToolError, UnknownAgentError
from unknown_agent.observability.events import Event, EventBus
from unknown_agent.observability.logging import get_logger

ParamType = Literal["string", "integer", "number", "boolean", "array", "object", "null"]


@dataclass(slots=True)
class ParameterSpec:
    name: str
    type: ParamType = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    enum: Sequence[str] | None = None
    items: ParamType | None = None
    minimum: float | None = None
    maximum: float | None = None
    pattern: str | None = None
    examples: Sequence[Any] | None = None

    def to_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            schema["enum"] = list(self.enum)
        if self.type == "array":
            schema["items"] = {"type": self.items or "string"}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.pattern:
            schema["pattern"] = self.pattern
        if self.default is not None:
            schema["default"] = self.default
        if self.examples:
            schema["examples"] = list(self.examples)
        return schema

    def coerce(self, value: Any) -> Any:
        """Convert a CLI/env string into the declared type, or raise ValueError."""

        if value is None:
            return self.default
        if self.type == "string":
            return value if isinstance(value, str) else str(value)
        if self.type == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "on", "y"}:
                return True
            if text in {"0", "false", "no", "off", "n"}:
                return False
            raise ValueError(f"expected a boolean, got {value!r}")
        if self.type == "integer":
            return int(value)
        if self.type == "number":
            return float(value)
        if self.type == "array":
            if isinstance(value, (list, tuple)):
                return list(value)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.startswith("["):
                    import json

                    parsed = json.loads(stripped)
                    if not isinstance(parsed, list):
                        raise ValueError("expected a JSON array")
                    return parsed
                return [item.strip() for item in stripped.split(",") if item.strip()]
            return [value]
        if self.type == "object":
            if isinstance(value, dict):
                return value
            import json

            parsed = json.loads(str(value))
            if not isinstance(parsed, dict):
                raise ValueError("expected a JSON object")
            return parsed
        return value


@dataclass(slots=True)
class ToolContext:
    """Everything a tool may need, injected - never imported from globals."""

    settings: Settings
    paths: AppPaths
    project_root: Path
    events: EventBus
    project: Any = None          # unknown_agent.context.project.ProjectContext
    prompter: Any = None         # unknown_agent.ui.prompts.Prompter
    run_id: str = ""
    step: int = 0
    session_id: str | None = None
    artifacts_dir: Path | None = None
    backups_dir: Path | None = None
    cache_dir: Path | None = None
    http_client: Any = None
    browser: Any = None          # BrowserSessionManager
    memory: Any = None           # MemoryStore
    extra: dict[str, Any] = field(default_factory=dict)

    def emit(self, type: str, message: str = "", **data: Any) -> Event:
        return self.events.emit(Event(type=type, run_id=self.run_id, step=self.step, message=message, data=data))  # type: ignore[arg-type]

    def artifact_path(self, name: str) -> Path:
        base = self.artifacts_dir or (self.paths.artifacts_dir)
        base.mkdir(parents=True, exist_ok=True)
        safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
        return base / safe

    def backup_path(self, name: str) -> Path:
        base = self.backups_dir or self.paths.backups_dir
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
        return base / f"{stamp}-{safe}"


class Tool(abc.ABC):
    """Base class for every capability in the product."""

    name: str = ""
    description: str = ""
    category: str = "general"
    risk: RiskLevel = RiskLevel.READ
    parameters: tuple[ParameterSpec, ...] = ()
    #: Human-facing example invocations shown in ``ua tools describe``.
    examples: tuple[str, ...] = ()
    timeout_s: float = 300.0
    #: Optional extra dependency key, e.g. ``browser`` for Playwright tools.
    requires_extra: str | None = None
    #: Tags help the planner and docs group tools.
    tags: tuple[str, ...] = ()
    #: When True the tool needs a live browser session managed by the executor.
    concurrency_safe: bool = True

    # -- schema ------------------------------------------------------------
    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        properties = {param.name: param.to_schema() for param in cls.parameters}
        required = [param.name for param in cls.parameters if param.required]
        schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            schema["required"] = required
        return schema

    @classmethod
    def spec(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "description": cls.description,
            "category": cls.category,
            "risk": cls.risk.value,
            "parameters": {param.name: param.to_schema() for param in cls.parameters},
            "required": [param.name for param in cls.parameters if param.required],
            "examples": list(cls.examples),
            "timeout_s": cls.timeout_s,
            "requires_extra": cls.requires_extra,
            "tags": list(cls.tags),
        }

    # -- argument handling -------------------------------------------------
    @classmethod
    def validate_arguments(cls, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Return ``(cleaned_arguments, problems)``.  Unknown keys are dropped with a note."""

        problems: list[str] = []
        specs = {param.name: param for param in cls.parameters}
        cleaned: dict[str, Any] = {}
        for key, value in (arguments or {}).items():
            spec = specs.get(key)
            if spec is None:
                problems.append(f"unknown argument {key!r} (ignored)")
                continue
            try:
                cleaned[key] = spec.coerce(value)
            except (ValueError, TypeError, ValueError) as exc:
                problems.append(f"argument {key!r}: {exc}")
        for param in cls.parameters:
            if param.name not in cleaned:
                if param.required:
                    problems.append(f"missing required argument {param.name!r}")
                elif param.default is not None:
                    cleaned[param.name] = param.default
        if cleaned.get("path") and isinstance(cleaned["path"], str):
            cleaned["path"] = cleaned["path"].strip()
        return cleaned, problems

    @classmethod
    def summarise_call(cls, arguments: dict[str, Any]) -> str:
        """One-line, human-readable call description for the CLI and the journal."""

        if not arguments:
            return cls.name
        parts = []
        for key, value in arguments.items():
            text = str(value)
            if len(text) > 60:
                text = text[:57] + "..."
            if "\n" in text:
                text = text.replace("\n", "\\n")
            parts.append(f"{key}={text}")
        return f"{cls.name}({', '.join(parts)})"

    # -- execution ---------------------------------------------------------
    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        """Execute the capability and return a structured result."""

    async def execute(self, ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        """Validate, run, time and normalise.  The only entry point used by the executor."""

        cleaned, problems = self.validate_arguments(arguments)
        hard_problems = [problem for problem in problems if not problem.endswith("(ignored)")]
        if hard_problems:
            return ToolResult.failure("Invalid arguments: " + "; ".join(hard_problems), output="; ".join(problems))
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self.run(ctx, **cleaned), timeout=self.timeout_s)
        except TimeoutError:
            return ToolResult(ok=False, error=f"{self.name} timed out after {self.timeout_s:.0f}s", duration_ms=(time.perf_counter() - started) * 1000)
        except asyncio.CancelledError:
            raise
        except ToolError as exc:
            result = ToolResult.failure(str(exc))
        except UnknownAgentError as exc:
            # Expected, actionable condition (missing dependency, bad policy, missing
            # config). Surface the message *and* the hint so both the user and the
            # model know the next step - and do not spam a traceback.
            get_logger("tools").debug("tool %s reported: %s", self.name, exc.message)
            message = exc.message + (f" Hint: {exc.hint}" if exc.hint else "")
            result = ToolResult.failure(message)
        except Exception as exc:  # noqa: BLE001 - tools must never crash the run
            get_logger("tools").exception("tool %s raised", self.name)
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")
        result.duration_ms = result.duration_ms or (time.perf_counter() - started) * 1000
        if problems:
            result.metadata.setdefault("warnings", problems)
        return result


class ToolNotFound(ToolError):
    def __init__(self, name: str, available: Sequence[str], *, withheld_reason: str | None = None) -> None:
        if withheld_reason:
            message = f"Tool {name!r} is not available in this session."
            hint = f"{withheld_reason} (`ua tools list` shows the active set: {len(available)} registered)."
        else:
            message = f"Unknown tool {name!r}."
            hint = f"Run `ua tools list` to see available tools ({len(available)} registered)."
        super().__init__(message, hint=hint)


__all__ = ["Tool", "ToolContext", "ParameterSpec", "ToolNotFound", "ParamType"]
