"""Runner: assemble everything a run needs, then execute it.

The runner is the composition root of the application logic.  It owns:

* configuration snapshot and per-run overrides (``--model``, ``--read-only``, ...),
* the provider instance and its lifecycle,
* the tool registry, policy engine, executor and tool context,
* memory (persistent, procedural, working) and the context builder,
* the validation engine and the browser session manager,
* the run directory (journal, plan, verification, diff) and session indexing,
* cancellation (Ctrl-C) and post-run housekeeping (memory extraction, pruning).

Keeping this in one class means the CLI commands stay thin, tests can drive a
complete run headlessly, and a future TUI or HTTP surface reuses the identical
execution path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from contextvars import ContextVar
from typing import Any

from unknown_agent.config.loader import LoadedSettings
from unknown_agent.config.paths import AppPaths, resolve_paths
from unknown_agent.context.builder import ContextBuilder
from unknown_agent.context.project import ProjectContext, load_project_context
from unknown_agent.core.loop import AgentLoop, LoopHooks, LoopOutcome
from unknown_agent.core.models import Plan, RunState, RunStatus, VerificationReport
from unknown_agent.core.planner import Planner
from unknown_agent.errors import ConfigError, Interrupted, UnknownAgentError
from unknown_agent.host.process import run_process
from unknown_agent.memory.procedures import ProcedureLibrary
from unknown_agent.memory.sessions import SessionStore
from unknown_agent.memory.store import MemoryStore
from unknown_agent.memory.working import WorkingMemory
from unknown_agent.observability.events import Event, EventBus
from unknown_agent.observability.logging import get_logger
from unknown_agent.observability.telemetry import load_telemetry
from unknown_agent.providers.registry import build_provider
from unknown_agent.tools.base import ToolContext
from unknown_agent.tools.executor import ToolExecutor
from unknown_agent.tools.factory import build_default_registry
from unknown_agent.tools.policy import PolicyEngine
from unknown_agent.tools.registry import ToolRegistry

log = get_logger("core.runner")


@dataclass(slots=True)
class RunRequest:
    """Everything a caller can influence about a run."""

    task: str
    mode: str = "auto"                 # auto | plan | react
    kind: str = "run"                  # run | build | fix | ask | plan
    read_only: bool = False
    dry_run: bool = False
    yes: bool = False
    non_interactive: bool = False
    model: str | None = None
    provider: str | None = None
    max_steps: int | None = None
    definition_of_done: str | None = None
    gates: list[str] | None = None
    #: Explicit tool allow-list.  ``None`` means "whatever the session grants";
    #: ``[]`` means "no tools at all"; a non-empty list restricts the registry to
    #: exactly those names and *withholds* the
    #: rest with a reason, so an attempted call is refused with an explanation
    #: rather than silently missing from the schema.  Orchestration uses this to
    #: make a role's grant a boundary instead of a suggestion.
    tools: list[str] | None = None
    #: ``None`` = let ``[orchestration]`` and the task's own complexity decide;
    #: ``True`` = delegate to the conductor even for a simple task;
    #: ``False`` = stay solo (``--solo``).  Explicit beats inferred, because a user
    #: who typed a flag has more context than a heuristic does.
    orchestrate: bool | None = None
    browser_url: str | None = None
    invoke_browser: bool | None = None
    resume_run_id: str | None = None
    extra_instructions: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    state: RunState
    outcome: LoopOutcome | None
    run_dir: Path
    verification: VerificationReport | None = None
    diff: str = ""
    exit_status: RunStatus = RunStatus.COMPLETED

    # -- ergonomic accessors for library users ---------------------------
    @property
    def run_id(self) -> str:
        return self.state.id

    @property
    def status(self) -> RunStatus:
        return self.state.status

    @property
    def ok(self) -> bool:
        """True when the run finished with every required gate passing."""

        return self.state.status is RunStatus.COMPLETED and (
            self.verification is None or self.verification.passed
        )

    @property
    def summary(self) -> str:
        return self.state.summary or ""

    @property
    def blockers(self) -> list[str]:
        return list(self.state.blockers)

    @property
    def files_changed(self) -> list[str]:
        return list(self.state.files_changed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.state.to_dict(),
            "run_dir": str(self.run_dir),
            "verification": self.verification.to_dict() if self.verification else None,
            "steps": [step.to_dict() for step in (self.outcome.steps if self.outcome else [])],
            "diff_stat": _diff_stat(self.diff),
            "blocked": bool(self.outcome.blocked) if self.outcome else False,
        }


#: Nesting depth of delegated runs.  A worker's own Runner must never start a
#: second conductor: the decomposition already happened one level up, and
#: conductor -> worker -> conductor multiplies workers per level until the process
#: runs out of file descriptors.  ``AgentExecutor`` sets ``orchestrate=False``
#: explicitly; this guard covers embedders that build a ``Runner`` by hand inside a
#: worker.
_DELEGATION_DEPTH: ContextVar[int] = ContextVar("ua_delegation_depth", default=0)


class Runner:
    """Composition root + execution facade used by the CLI and the Python API."""

    #: Kinds that can be delegated.  ``ask`` is a question and ``plan`` is
    #: explicitly plan-only, so neither belongs to a conductor; ``run``/``build``/
    #: ``fix`` are the shapes that benefit from specialised workers.
    _ORCHESTRABLE_KINDS = frozenset({"run", "build", "fix"})

    def __init__(
        self,
        loaded: LoadedSettings,
        *,
        request: RunRequest,
        hooks: LoopHooks | None = None,
        event_sink: Any = None,
    ) -> None:
        self.loaded = loaded
        self.settings = loaded.settings
        self.paths: AppPaths = loaded.paths
        self.request = request
        self.hooks = hooks or LoopHooks()
        self.events = EventBus(sinks=[event_sink] if event_sink else [])
        self._event_sink = event_sink
        self._apply_overrides()
        self.paths.ensure_project_dirs()
        from unknown_agent.extensions.hooks import HookBus

        self.hook_bus = HookBus(self.paths.project_dir / "hooks.db")
        self.events.attach_hook_bus(self.hook_bus)
        self.project: ProjectContext | None = None
        self.provider = None
        self.registry: ToolRegistry | None = None
        self.executor: ToolExecutor | None = None
        self.validators: Any = None
        self.browser: Any = None
        self.memory: MemoryStore | None = None
        self.sessions: SessionStore | None = None
        self.procedures: ProcedureLibrary | None = None
        #: The conductor's outcome when this run delegated, else ``None``.  Written
        #: to the run journal as ``orchestration.json`` so a delegated run is
        #: auditable with the same detail as a solo one.
        self._orchestration: Any = None
        #: Resolved :class:`~unknown_agent.skills.pack.SkillSet` for this run.
        self.skills: Any = None
        #: :class:`~unknown_agent.mcp.manager.McpManager` when [mcp] has servers.
        self.mcp: Any = None
        #: :class:`~unknown_agent.vision.service.VisionService` shared by vision tools
        #: so a fallback provider is built once per run and closed exactly once.
        self.vision: Any = None
        self.working = WorkingMemory(
            token_budget=self.settings.memory.working_memory_tokens,
            observation_budget=self.settings.agent.observation_token_budget,
        )
        self.state: RunState | None = None
        self._cancelled = False
        self._cancel_event = asyncio.Event()
        self.telemetry = load_telemetry(
            enabled=self.settings.telemetry.enabled,
            local_metrics=self.settings.telemetry.local_metrics,
            endpoint=self.settings.telemetry.endpoint,
            state_dir=self.paths.state_dir,
            anonymous_id=self.settings.telemetry.anonymous_id,
        )

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #
    def _apply_overrides(self) -> None:
        request = self.request
        if request.provider:
            name = request.provider
            if name not in self.settings.providers:
                # `--provider offline` / `--provider ollama` work with no config at
                # all: a named provider *type* is materialised into a profile so the
                # product is usable before any setup is done.
                from unknown_agent.providers.registry import ensure_provider_profile

                materialised = ensure_provider_profile(self.settings, name)
                if not materialised:
                    raise ConfigError(
                        f"No provider profile named {name!r}.",
                        hint=(
                            f"Configured profiles: {', '.join(sorted(self.settings.providers))}. "
                            "Add one under [providers.<name>] in your config, or pass a provider type "
                            "(offline, ollama, anthropic, openai-compatible)."
                        ),
                    )
            self.settings.provider = name
        if request.model:
            self.settings.active_provider.model = request.model
        if request.max_steps:
            self.settings.agent.max_steps = request.max_steps
        if request.definition_of_done:
            self.settings.agent.definition_of_done = request.definition_of_done  # type: ignore[assignment]
        if request.read_only:
            self.settings.policy.read_only = True
        if request.dry_run:
            self.settings.policy.dry_run = True
        if request.browser_url:
            self.settings.browser.base_url = request.browser_url
        if request.invoke_browser is not None:
            self.settings.browser.enabled = request.invoke_browser
        if request.mode and request.mode != "auto":
            self.settings.agent.mode = request.mode  # type: ignore[assignment]

    def _resolve_skills(self, root: Path) -> None:
        """Load the active skills once per run and fold their gates into "done".

        A skill is a unit of *competence*: instructions for the prompt, plus the
        gates it considers part of finished work.  Both halves only matter if they
        reach the run, so this does two things:

        * resolves the set (builtin pack + user dir + project dir, precedence
          ``disable > enable > pack > default``) and hands it to the context
          builder, which renders the task-relevant slice into the system prompt;
        * enables each gate the active skills name.  Enabling is the only direction
          a skill may push validation — a project skill can make a run stricter,
          never laxer, so an untrusted ``.unknown/skills/*.toml`` cannot weaken the
          definition of done by asking for fewer gates.

        Fail-soft by design: skills are an enhancement, and a broken skill file in
        someone else's repository must never stop the agent from starting.
        """

        try:
            from unknown_agent.skills import resolve_skills

            # ``user_dir`` is not optional: without it ``_skills_dir(None, "user")``
            # resolves to None and every skill in ~/.config/unknown-agent/skills is
            # silently invisible, which makes `ua skills new --scope user` write a
            # file nothing ever reads.
            resolved = resolve_skills(
                self.settings,
                user_dir=self.paths.user_skills_dir,
                project_dir=root,
            )
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.warning("skills unavailable: %s", exc)
            self.skills = None
            return
        self.skills = resolved
        for name in resolved.gates():
            setting = self.settings.validation.gate(name)
            if not setting.enabled:
                setting.enabled = True
                self.settings.validation.gates[name] = setting
                log.debug("skill gate enabled: %s", name)
        if resolved.unresolved:
            for note in resolved.unresolved[:5]:
                log.warning("skill: %s", note)

    def _apply_tool_allow_list(self) -> None:
        """Restrict the registry to ``request.tools`` when the caller set one.

        A role's grant is a boundary, not a preference: a documenter that can call
        ``shell_run`` is not a documenter, and a read-only inspector that can write
        is not read-only.  Withholding (instead of merely passing fewer schemas to
        the model) matters because the executor reports the *reason* when a withheld
        tool is called, so the worker learns the boundary exists rather than getting
        a confusing "unknown tool".

        Names that are not registered — browser tools without Playwright, plugin
        tools that failed to load — are ignored: an allow-list narrows what exists,
        it does not conjure tools into being.
        """

        allowed = self.request.tools
        if allowed is None or self.registry is None:
            return
        keep = {str(name).strip() for name in allowed if str(name).strip()}
        # An explicit empty list means "no tools at all", not "unrestricted".
        # Silently ignoring it would turn the strictest possible boundary into the
        # loosest one — the same class of quiet inversion as pack = ["none"].
        orchestration = self.request.metadata.get("orchestration") or {}
        role = str(orchestration.get("role") or "")
        reason = f"not granted to the {role} role" if role else "not granted for this run"
        for tool in list(self.registry.all()):
            if tool.name not in keep:
                self.registry.withhold(tool.name, reason)

    async def setup(self) -> Runner:
        """Build every subsystem.  Idempotent."""

        root = self.paths.project_dir.parent if self.paths.project_dir.exists() else Path.cwd()
        self.project = load_project_context(root, self.settings)

        # memory
        self.memory = MemoryStore(
            self.paths.project_dir / "memory" / "memory.db",
            markdown_dir=self.paths.project_memory_dir if self.settings.memory.write_markdown_mirror else None,
            fts_enabled=self.settings.memory.fts_enabled,
        )
        self.sessions = SessionStore(self.paths.sessions_db)
        self.procedures = ProcedureLibrary(
            self.paths.project_dir / "procedures",
            self.paths.procedures_dir,
        )

        # provider
        provider_config = self.settings.active_provider
        if self.request.mode == "plan" or self.request.kind == "plan":
            self.settings.policy.read_only = True
        self.provider = build_provider(provider_config, name=self.settings.provider)

        # tools
        browser_enabled = bool(self.settings.browser.enabled) and not self.settings.policy.read_only
        self.registry = build_default_registry(self.settings, include_browser=browser_enabled)
        self._load_plugins()
        await self._load_mcp_tools()
        self._apply_tool_allow_list()

        if browser_enabled:
            from unknown_agent.browser.manager import BrowserSessionManager

            self.browser = BrowserSessionManager(self.settings.browser, artifacts_dir=self.paths.artifacts_dir)

        run_id = self.request.resume_run_id or RunState.new(task=self.request.task).id
        run_dir = self.paths.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.events.attach_journal(run_dir / "events.jsonl")

        tool_context = ToolContext(
            settings=self.settings,
            paths=self.paths,
            project_root=Path(self.project.root),
            events=self.events,
            project=self.project,
            prompter=getattr(self.hooks, "prompter", None) or _default_prompter(self.request),
            run_id=run_id,
            artifacts_dir=self.paths.artifacts_dir,
            backups_dir=self.paths.backups_dir,
            cache_dir=self.paths.project_cache_dir,
            browser=self.browser,
            memory=self.memory,
            extra=self._build_tool_extra(),
        )
        self.tool_context = tool_context
        policy = PolicyEngine(self.settings, project_root=Path(self.project.root), prompter=tool_context.prompter)
        self.executor = ToolExecutor(
            registry=self.registry,
            settings=self.settings,
            context=tool_context,
            policy=policy,
        )

        self._resolve_skills(Path(self.project.root))

        if self.settings.validation.auto_detect or self.settings.validation.gates:
            from unknown_agent.validation.engine import ValidationEngine

            self.validators = ValidationEngine(
                self.settings,
                project_root=Path(self.project.root),
                events=self.events,
                run_id=run_id,
                browser=self.browser,
                project=self.project,
            )

        self.builder = ContextBuilder(
            self.settings,
            project=self.project,
            memory=self.memory,
            procedures=self.procedures,
            working=self.working,
            skills=self.skills,
        )
        self.state = RunState.new(
            task=self.request.task,
            cwd=str(self.project.root),
            provider=provider_config.type,
            model=provider_config.model,
            mode=self.settings.agent.mode if self.settings.agent.mode != "auto" else self.request.mode,
        )
        self.state.id = run_id
        return self

    def _build_tool_extra(self) -> dict[str, Any]:
        """Shared services handed to tools through ``ToolContext.extra``."""

        extra: dict[str, Any] = {}
        if self.mcp is not None:
            extra["mcp_manager"] = self.mcp
        if self.settings.vision.enabled:
            from unknown_agent.vision.service import VisionService

            self.vision = VisionService(self.settings)
            extra["vision_service"] = self.vision
        return extra

    async def _load_mcp_tools(self) -> None:
        """Register MCP-backed tools, fail-soft.

        Ordering matters: after plugins (MCP proxies are namespaced ``mcp.*`` so
        they can never shadow either) and *before* the allow-list, so a role's
        grant applies to MCP tools exactly like builtins.  A read-only session
        registers no MCP tools at all - server-side effects cannot be proven
        read-only from here, and "read-only" must mean something.
        """

        config = self.settings.mcp
        if not (config.enabled and config.expose_tools and config.servers) or self.registry is None:
            return
        if self.settings.policy.read_only:
            log.debug("mcp tools skipped: session is read-only")
            return
        from unknown_agent.mcp import mcp_available

        available, why = mcp_available()
        if not available:
            log.debug("mcp tools skipped: %s", why)
            return
        from unknown_agent.mcp.bridge import build_proxies
        from unknown_agent.mcp.manager import McpManager

        self.mcp = McpManager(self.settings)
        try:
            selected, warnings = await self.mcp.agent_tools()
        except Exception as exc:  # noqa: BLE001 - fail_soft=False propagates above this
            log.warning("mcp tool discovery failed: %s", exc)
            return
        for warning in warnings:
            log.warning("%s", warning)
            self.events.emit(Event(type="mcp.warning", message=warning))  # type: ignore[arg-type]
        for proxy in build_proxies(selected):
            self.registry.register(proxy, source=f"mcp:{proxy.server}")

    def _load_plugins(self) -> None:
        if not self.settings.plugins.enabled or self.registry is None:
            return
        from unknown_agent.extensions.loader import PluginManager

        manager = PluginManager(
            allow_entry_points=self.settings.plugins.allow_entry_points,
            user_dir=self.paths.user_plugins_dir,
            project_dir=self.paths.project_dir / "plugins",
            disabled=self.settings.plugins.disabled,
            require_signed=self.settings.plugins.require_signed,
            trust_store_path=self.paths.config_dir / "trusted_publishers.json",
        )
        self.plugins = manager.load(
            registry=self.registry, settings=self.settings, hook_bus=self.hook_bus
        )

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    async def execute(self) -> RunResult:
        assert self.state is not None and self.registry is not None and self.executor is not None
        plan: Plan | None = None
        if self.request.resume_run_id:
            plan = self._load_plan(self.request.resume_run_id)

        planner = Planner(self.provider, builder=self.builder, settings=self.settings)
        loop = AgentLoop(
            settings=self.settings,
            provider=self.provider,
            registry=self.registry,
            executor=self.executor,
            tool_context=self.tool_context,
            context_builder=self.builder,
            planner=planner,
            validators=self.validators,
            working=self.working,
            events=self.events,
            hooks=self.hooks,
        )

        mode = self.settings.agent.mode
        if mode == "auto":
            mode = "plan" if self.request.kind == "plan" else "react"
        self.state.mode = mode

        delegate, delegate_reason = self._orchestration_decision()
        started = time.perf_counter()
        outcome: LoopOutcome | None = None
        try:
            with _interrupt_as_cancellation(self):
                if delegate and plan is None:
                    outcome = await self._run_orchestrated(delegate_reason)
                elif self.request.kind == "plan" or mode == "plan":
                    outcome = await self._plan_only(loop, plan)
                else:
                    outcome = await loop.run(self.state, plan=plan)
        except Interrupted:
            self.state.finish(RunStatus.INTERRUPTED, summary="Interrupted by the user (Ctrl-C).")
            self.events.emit(Event(type="interrupt", run_id=self.state.id, message="run interrupted"))
        except UnknownAgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert to a controlled failure
            log.exception("run failed")
            self.state.blockers.append(f"{type(exc).__name__}: {exc}")
            self.state.finish(RunStatus.FAILED, summary=f"Run failed: {type(exc).__name__}: {exc}")
        finally:
            self.state.finished_at = self.state.finished_at or time.time()

        duration_ms = (time.perf_counter() - started) * 1000
        await self._post_run(plan or self.state.plan)

        diff = await self._capture_diff()
        run_dir = self._write_run_artifacts(diff)
        self.telemetry.record(
            "run.finished",
            properties={
                "status": self.state.status.value,
                "kind": self.request.kind,
                "mode": self.state.mode,
                "orchestrated": self._orchestration is not None,
            },
            duration_ms=duration_ms,
        )
        return RunResult(
            state=self.state,
            outcome=outcome,
            run_dir=run_dir,
            verification=self.state.verification,
            diff=diff,
            exit_status=self.state.status,
        )

    async def _plan_only(self, loop: AgentLoop, plan: Plan | None) -> LoopOutcome:
        assert self.state is not None and self.registry is not None
        if plan is None:
            result = await loop.planner.plan(self.state.task, tool_schemas=self.registry.schemas())
            plan = result.plan
            self.events.emit(
                Event(type="plan.created", run_id=self.state.id, message=f"plan with {len(plan.steps)} step(s)", data={"source": result.source, "warnings": result.warnings})
            )
        self.state.plan = plan
        self.state.finish(RunStatus.COMPLETED, summary="Plan produced. No files were modified (plan mode is read-only).")
        return LoopOutcome(state=self.state, summary=self.state.summary)

    # ------------------------------------------------------------------ #
    # orchestration (delegating to the conductor)
    # ------------------------------------------------------------------ #
    def _orchestration_decision(self) -> tuple[bool, str]:
        """Should this run be delegated to the conductor, and why?

        The decision is deliberately explainable: it returns a reason string that
        ends up in the run journal and in the ``orchestration.started`` event, so
        "why did this run use four workers?" has an answer without reading code.

        Order matters.  An explicit ``--solo`` / ``--orchestrate`` beats config,
        config beats the complexity heuristic, and anything that cannot be
        delegated at all (a question, a plan-only run, a resumed solo plan) is
        rejected before the heuristic is even consulted.
        """

        request = self.request
        config = getattr(self.settings, "orchestration", None)
        if _DELEGATION_DEPTH.get() > 0:
            return False, "already inside a delegated run"
        if request.orchestrate is False:
            return False, "solo requested"
        if config is None or not config.enabled:
            return False, "orchestration disabled in config"
        if str(config.mode) == "solo":
            return False, "[orchestration] mode = solo"
        if request.kind not in self._ORCHESTRABLE_KINDS:
            return False, f"kind '{request.kind}' is not delegable"
        if request.resume_run_id:
            return False, "resuming a previous plan"
        if request.orchestrate is True:
            return True, "orchestration requested"
        if str(config.mode) == "orchestrated":
            return True, "[orchestration] mode = orchestrated"

        from unknown_agent.orchestration import Router, analyze_task

        try:
            analysis = analyze_task(request.task, repo_known=self.project is not None)
        except Exception as exc:  # noqa: BLE001 - a scoring bug must not block a run
            log.warning("task analysis failed, staying solo: %s", exc)
            return False, "task analysis failed"
        floor = int(config.min_complexity)
        if analysis.complexity < floor:
            return False, f"complexity {analysis.complexity} < min_complexity {floor}"
        # Complexity is necessary but not sufficient.  If the router would hand the
        # whole task to a single worker, delegating buys nothing and costs a
        # conductor, a graph, a merge and a second audit trail — so the loop stays
        # solo.  ``pattern_for`` is deterministic and does no I/O, which is what
        # makes it cheap enough to ask up front.
        try:
            pattern, rationale = Router(config=config).pattern_for(analysis)
        except Exception as exc:  # noqa: BLE001 - routing must not block a run
            log.warning("orchestration routing failed, staying solo: %s", exc)
            return False, "routing failed"
        if pattern == "solo":
            return False, f"complexity {analysis.complexity} but the shape is solo ({rationale})"
        return True, f"complexity {analysis.complexity} >= min_complexity {floor}, shape {pattern}"

    async def _run_orchestrated(self, reason: str) -> LoopOutcome:
        """Delegate the whole run to the conductor instead of the solo loop.

        The conductor owns decomposition, role grants, retries and merging; this
        method owns the translation back into a :class:`RunState` so that every
        downstream consumer (journal, CLI renderers, ``RunResult``) sees one shape
        regardless of which path produced the work.

        Workers run through :class:`~unknown_agent.orchestration.AgentExecutor`,
        which builds a bounded ``Runner`` per role — so a delegated worker gets the
        same policy engine, skills and tool allow-list a solo run would, and cannot
        widen its own grant.
        """

        assert self.state is not None
        from unknown_agent.orchestration import AgentExecutor, orchestrate

        self.state.mode = "orchestrated"
        self.events.emit(
            Event(
                type="orchestration.started",
                run_id=self.state.id,
                message=f"delegating to the conductor ({reason})",
                data={"reason": reason, "kind": self.request.kind},
            )
        )
        executor = AgentExecutor(
            self.loaded,
            model=self.request.model,
            provider=self.request.provider,
            dry_run=self.request.dry_run,
        )
        root = str(self.project.root) if self.project is not None else str(self.paths.cwd)

        def forward(kind: str, payload: Any) -> None:
            """Conductor event -> run journal + caller hooks.

            ``LoopHooks.on_event`` may be sync *or* async (``ASYNC_HOOK``), and this
            callback is invoked from the conductor's own synchronous event points,
            so a coroutine result has to be scheduled on the running loop rather
            than awaited — the same arrangement ``AgentLoop.emit`` uses.  Dropping
            it on the floor would silently disable live progress for every async
            consumer (the TUI, ``--ndjson``).
            """

            data = dict(payload or {})
            event = Event(
                type=f"orchestration.{kind}",
                run_id=self.state.id,
                message=str(data.get("role") or data.get("work_id") or kind),
                data=data,
            )
            self.events.emit(event)
            if self.hooks.on_event is None:
                return
            try:
                result = self.hooks.on_event(event)
                if asyncio.iscoroutine(result):
                    with contextlib.suppress(RuntimeError):
                        asyncio.get_running_loop().create_task(result)
            except Exception:  # noqa: BLE001 - a bad hook must not kill the run
                log.debug("orchestration event hook failed", exc_info=True)

        token = _DELEGATION_DEPTH.set(_DELEGATION_DEPTH.get() + 1)
        try:
            outcome = await orchestrate(
                self.state.task,
                config=self.settings.orchestration,
                executor=executor,
                project_root=root,
                on_event=forward,
                run_id=self.state.id,
                dry_run=self.request.dry_run,
            )
        finally:
            _DELEGATION_DEPTH.reset(token)
        self._orchestration = outcome
        self._absorb_orchestration(outcome)
        self.events.emit(
            Event(
                type="orchestration.finished",
                run_id=self.state.id,
                message=f"orchestration {outcome.status}",
                data={"status": outcome.status, "ok": outcome.ok, "workers": len(outcome.graph.items)},
            )
        )
        return LoopOutcome(
            state=self.state,
            summary=self.state.summary,
            blocked=not outcome.ok,
            blocker_reason="; ".join(self.state.blockers),
        )

    def _absorb_orchestration(self, outcome: Any) -> None:
        """Map an :class:`OrchestrationOutcome` onto this run's state.

        Honesty rules, in order of how easy they are to get wrong:

        * ``ok`` decides the status, not the label.  A ``partial`` outcome is work
          that did not finish, so it is BLOCKED — reporting it as COMPLETED would
          make ``RunResult.ok`` true for a run with missing deliverables.
        * ``cancelled`` means the budget *stopped* work, which is a failure of the
          run even though nothing errored.
        * Workers whose gates never ran (``unverified``) are recorded as blockers.
          A run that checked nothing must not look like a run that passed.
        * Files come from the merge, not from what workers claimed.
        """

        assert self.state is not None
        state = self.state
        status = str(getattr(outcome, "status", "") or "")
        if outcome.ok:
            state.status = RunStatus.COMPLETED
        elif status == "cancelled":
            state.status = RunStatus.FAILED
        elif status in {"blocked", "partial"}:
            state.status = RunStatus.BLOCKED
        else:
            state.status = RunStatus.FAILED

        blockers = list(getattr(outcome, "blockers", []) or [])
        merge = getattr(outcome, "merge", None)
        if merge is not None:
            for conflict in list(getattr(merge, "conflicts", []) or []):
                blockers.append(f"merge conflict: {conflict}")
            state.files_changed = list(dict.fromkeys(list(getattr(merge, "files", []) or [])))
        unverified = list(getattr(outcome, "unverified", []) or [])
        if unverified:
            blockers.append(f"unverified workers (no gate ran): {', '.join(unverified)}")
        state.blockers = list(dict.fromkeys(blockers))

        summary = str(getattr(outcome, "summary", "") or "").strip()
        if not summary:
            succeeded = len(outcome.succeeded()) if hasattr(outcome, "succeeded") else 0
            total = len(getattr(outcome.graph, "items", []) or [])
            summary = f"Orchestration {status}: {succeeded}/{total} workers succeeded."
        state.finish(state.status, summary=summary)
        state.step = max(state.step, len(getattr(outcome.graph, "items", []) or []))

    async def _post_run(self, plan: Plan | None) -> None:
        """Memory extraction, indexing, pruning and cleanup."""

        assert self.state is not None
        try:
            if self.settings.memory.auto_remember and not self.settings.policy.read_only:
                await self._extract_memories(plan)
            if self.settings.memory.retention_days and self.memory:
                removed = self.memory.prune(self.settings.memory.retention_days)
                if removed:
                    log.info("pruned %s stale memory fact(s)", removed)
            if self.sessions:
                self.sessions.record_from_run_state(self.state, run_path=self.paths.run_dir(self.state.id))
                self.sessions.prune(self.settings.memory.sessions_retention)
        except Exception:  # pragma: no cover - housekeeping must never fail a run
            log.exception("post-run housekeeping failed")
        finally:
            if self.browser is not None:
                with contextlib.suppress(Exception):
                    await self.browser.close()
            if self.mcp is not None:
                with contextlib.suppress(Exception):
                    await self.mcp.close()
            if self.vision is not None:
                with contextlib.suppress(Exception):
                    await self.vision.aclose()
            tool_context = getattr(self, "tool_context", None)
            cu_session = (tool_context.extra or {}).get("computer_use_session") if tool_context else None
            if cu_session is not None:
                with contextlib.suppress(Exception):
                    await cu_session.close()
            if self.provider is not None and hasattr(self.provider, "aclose"):
                with contextlib.suppress(Exception):
                    await self.provider.aclose()  # type: ignore[func-returns-value]
            if self.memory is not None:
                self.memory.close()
            if self.sessions is not None:
                self.sessions.close()

    async def _extract_memories(self, plan: Plan | None) -> None:
        """Ask the model for durable facts learned during the run, then store them.

        Deliberately conservative: at most five facts, no secrets, and nothing is
        stored when the provider is offline or the run made no changes.
        """
        assert self.state is not None and self.memory is not None
        if not self.state.files_changed:
            return
        from unknown_agent.prompts.selfheal import MEMORY_EXTRACT_SYSTEM, memory_extraction_prompt
        from unknown_agent.providers.base import ModelRequest

        from unknown_agent.core.models import Message

        changes = "\n".join(f"- {path}" for path in self.state.files_changed[:20])
        request = ModelRequest(
            messages=[
                Message.system(MEMORY_EXTRACT_SYSTEM),
                Message.user(memory_extraction_prompt(task=self.state.task, changes=changes, observations=self.working.recent(count=10))),
            ],
            temperature=0.1,
            max_output_tokens=800,
            json_mode=True,
            reasoning=_reasoning_params(self.settings, "summary", max_output_tokens=800),
        )
        with contextlib.suppress(Exception):
            response = await self.provider.complete(request)
            if not response.ok or not response.content:
                return
            from unknown_agent.core.planner import _extract_json

            payload = _extract_json(response.content)
            if not payload:
                return
            for fact in (payload.get("facts") or [])[:5]:
                if not isinstance(fact, dict) or not fact.get("text"):
                    continue
                fact_id = self.memory.remember(
                    str(fact["text"]),
                    kind=str(fact.get("kind", "fact")),
                    tags=[str(tag) for tag in (fact.get("tags") or [])],
                    source=f"run:{self.state.id}",
                    run_id=self.state.id,
                    confidence=float(fact.get("confidence", 0.6)),
                )
                self.events.emit(
                    Event(type="memory.write", run_id=self.state.id, message=f"learned: {str(fact['text'])[:120]}", data={"fact_id": fact_id})
                )

    # ------------------------------------------------------------------ #
    # artifacts
    # ------------------------------------------------------------------ #
    async def _capture_diff(self) -> str:
        if self.project is None or not (Path(self.project.root) / ".git").exists():
            return ""
        result = await run_process(["git", "diff", "--no-color"], cwd=self.project.root, timeout=60)
        return result.stdout if result.exit_code == 0 else ""

    def _write_run_artifacts(self, diff: str) -> Path:
        assert self.state is not None
        run_dir = self.paths.run_dir(self.state.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps(self.state.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        if self.state.plan is not None:
            (run_dir / "plan.json").write_text(json.dumps(self.state.plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        if self.state.verification is not None:
            (run_dir / "verification.json").write_text(json.dumps(self.state.verification.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        if diff:
            (run_dir / "diff.patch").write_text(diff, encoding="utf-8")
        if self.executor is not None:
            (run_dir / "tools.json").write_text(json.dumps(self.executor.ledger(), indent=2, ensure_ascii=False), encoding="utf-8")
        if self._orchestration is not None:
            (run_dir / "orchestration.json").write_text(
                json.dumps(self._orchestration.to_dict(), indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        (run_dir / "task.md").write_text(f"# {self.state.task}\n\n- run: `{self.state.id}`\n- status: {self.state.status.value}\n- model: {self.state.model}\n", encoding="utf-8")
        return run_dir

    def _load_plan(self, run_id: str) -> Plan | None:
        path = self.paths.run_dir(run_id) / "plan.json"
        if not path.is_file():
            return None
        try:
            return Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return None

    def request_cancel(self) -> None:
        self._cancelled = True
        self._cancel_event.set()


@contextlib.contextmanager
def _interrupt_as_cancellation(runner: Runner) -> Any:
    """Translate SIGINT into a clean cancellation instead of a stack trace."""

    previous = signal.getsignal(signal.SIGINT)

    def handler(signum: int, frame: Any) -> None:  # pragma: no cover - signal path
        runner.request_cancel()
        raise Interrupted("Interrupted")

    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGINT, previous)


def _reasoning_params(settings: Any, surface: str, *, max_output_tokens: int | None = None) -> dict[str, Any]:
    """Reasoning wire params for a side-channel call (summaries, extraction).

    These are cheap, structured, and not the user's main work, so they resolve to
    the quick rung rather than inheriting whatever the run is using.
    """

    from unknown_agent.reasoning import params_for_settings

    params, _resolved = params_for_settings(settings, surface, max_output_tokens=max_output_tokens)
    return params


def _default_prompter(request: RunRequest) -> Any:
    from unknown_agent.ui.prompts import Prompter

    return Prompter(assume_yes=request.yes, non_interactive=request.non_interactive)


def _diff_stat(diff: str) -> dict[str, int]:
    files = sum(1 for line in diff.splitlines() if line.startswith("diff --git"))
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return {"files": files, "added": added, "removed": removed}


async def run_task(loaded: LoadedSettings, request: RunRequest, *, hooks: LoopHooks | None = None, event_sink: Any = None) -> RunResult:
    """Convenience entry point used by the API and tests."""

    runner = Runner(loaded, request=request, hooks=hooks, event_sink=event_sink)
    await runner.setup()
    return await runner.execute()


__all__ = ["Runner", "RunRequest", "RunResult", "run_task"]
