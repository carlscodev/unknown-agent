"""Typed configuration schema.

The schema is the contract between the CLI, the runtime, and every file on disk.
Unknown keys are rejected (``extra="forbid"``) so that typos surface immediately
instead of silently doing nothing, and every field has a sane default so a brand
new installation works with no configuration file at all.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Adapter dialects the registry ships with.  A profile's ``type`` may also be a
#: catalog vendor key (``openrouter``, ``groq``, ...), which the registry maps onto
#: the right dialect - that is what makes adding a vendor a data change.
ProviderType = Literal["openai-compatible", "anthropic", "ollama", "offline"]
AdapterProtocol = Literal["openai-compatible", "openai", "anthropic", "ollama", "offline"]
OutputMode = Literal["human", "json", "ndjson"]
LogFormat = Literal["text", "json"]

#: Visual identity.  ``auto`` resolves to a concrete palette at load time from the
#: terminal environment; the resolved value is what gets reported by
#: ``ua config show --explain`` so nothing is ambiguous after the fact.
ThemeChoice = Literal[
    "auto",
    "dark",
    "light",
    "obsidian-emerald",
    "midnight",
    "abyss",
    "ember",
    "aurora",
    "nocturne",
    "paper",
    "linen",
    "frost",
]
ThemeMode = Literal["dark", "light"]
GlyphSet = Literal["unicode", "ascii", "nerd"]
UiDensity = Literal["comfortable", "compact", "spacious"]
MotionLevel = Literal["full", "reduced", "off"]
ColorDepth = Literal["auto", "truecolor", "256", "standard", "none"]

#: Reasoning depth ladder.  Names are the product vocabulary; ``default`` means
#: "let the task and the model decide" and is resolved per request.
ReasoningMode = Literal["default", "medium", "high", "super", "max", "ultra"]

#: How aggressively the agent plans before it acts.
PlanningDepth = Literal["quick", "standard", "deep", "exhaustive"]
ClarifyMode = Literal["off", "auto", "always"]
DecisionSource = Literal["user", "default", "model", "inferred", "flag", "preset"]

#: Orchestration shapes the conductor may choose from.
OrchestrationPattern = Literal["solo", "pipeline", "fanout", "graph", "escalate"]
OrchestrationMode = Literal["auto", "solo", "orchestrated"]
MergeStrategy = Literal["owner_partition", "sequential_apply", "union", "best_of", "manual"]

#: Streaming render styles.
StreamStyle = Literal["live", "buffered", "silent", "structured"]
TokenPace = Literal["raw", "smoothed", "chunked"]
GateName = Literal[
    "syntax",
    "lint",
    "format",
    "typecheck",
    "tests",
    "build",
    "browser",
    "diff",
    "custom",
]
RiskLevel = Literal["read", "write", "execute", "network", "destructive"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)


class ProviderConfig(_Base):
    """A single model provider profile.  Multiple profiles may coexist."""

    @field_validator("type", mode="before")
    @classmethod
    def _check_type(cls, value: str) -> str:
        return _validate_provider_type(value)

    @property
    def is_placeholder(self) -> bool:
        """True for the stock profile injected before onboarding has run.

        A first run therefore reports "needs setup" (welcome, pick a provider) instead
        of "needs repair" (something broke), even though the config always carries an
        inspectable ``[providers.default]`` block.
        """

        if self.api_key or self.base_url:
            return False
        from unknown_agent.providers.catalog import resolve_entry

        return resolve_entry(self.type) is None

    type: str = Field(
        "openai-compatible",
        description="Adapter dialect (openai-compatible, anthropic, ollama, offline) or a vendor key from the provider catalog.",
    )
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    api_key: str | None = Field(
        None,
        description="Secret reference, e.g. env:OPENAI_API_KEY, file:~/.keys/openai, keyring:unknown-agent/openai.",
    )
    #: Which of the provider's declared authentication modes is active
    #: (``chatgpt`` / ``api_key`` / ``routed`` / ``endpoint`` / ``none``).  ``None``
    #: means "the provider's recommended mode": providers are not assumed key-only,
    #: so this is what distinguishes a Codex subscription session from an API key.
    auth_mode: str | None = Field(
        None,
        description="Active authentication mode declared by the provider (see `ua providers auth`).",
    )
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    max_output_tokens: int = Field(8000, ge=256)
    context_window: int = Field(128_000, ge=4_000)
    request_timeout_s: float = Field(120.0, gt=0)
    connect_timeout_s: float = Field(15.0, gt=0)
    max_retries: int = Field(3, ge=0, le=10)
    supports_tools: bool = True
    supports_streaming: bool = True
    json_mode: bool = True
    #: Per-profile reasoning depth.  ``None`` inherits ``[reasoning].default_mode``,
    #: which lets a cheap fast profile stay quick while a "big" profile thinks hard.
    reasoning_mode: ReasoningMode | None = Field(
        None,
        description="Override the reasoning depth for this profile (default | medium | high | super | max | ultra).",
    )
    reasoning_effort: str | None = Field(
        None,
        description="Vendor-native reasoning effort sent on the wire (e.g. minimal/low/medium/high). Overrides the depth mapping.",
    )
    #: Capability overrides resolved from live model metadata.  ``None`` = "ask the
    #: catalog / the model listing"; an explicit bool always wins.
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)


def _validate_provider_type(value: str) -> str:
    from unknown_agent.providers.catalog import resolve_entry

    key = (value or "").strip().lower()
    if key in {"openai-compatible", "anthropic", "ollama", "offline", "openai"}:
        return "openai-compatible" if key == "openai" else key
    entry = resolve_entry(key)
    if entry is None:
        raise ValueError(
            f"unknown provider type {value!r}: use an adapter dialect "
            "(openai-compatible, anthropic, ollama, offline) or a provider key from `ua providers catalog`"
        )
    return entry.key


class AgentConfig(_Base):
    """Execution-loop behaviour and budgets."""

    mode: Literal["auto", "plan", "react"] = "auto"
    max_steps: int = Field(60, ge=1, le=1000)
    max_tool_calls_per_step: int = Field(6, ge=1, le=32)
    max_tokens: int = Field(900_000, ge=1_000)
    max_wall_clock_minutes: float = Field(45.0, gt=0)
    max_self_heal_attempts: int = Field(4, ge=0, le=20)
    max_consecutive_failures: int = Field(3, ge=1, le=20)
    reflection_every_steps: int = Field(8, ge=1)
    include_repo_map: bool = True
    context_token_budget: int = Field(48_000, ge=2_000)
    observation_token_budget: int = Field(3_000, ge=200)
    enable_subagents: bool = True
    streaming: bool = True
    require_plan_approval: bool = False
    definition_of_done: Literal["strict", "balanced", "advisory"] = "balanced"


class PolicyConfig(_Base):
    """Tool-permission policy.  Deny-by-default for anything outside the workspace."""

    allow_outside_workspace: bool = False
    allow_network: bool = True
    allow_destructive: bool = False
    confirm_destructive: bool = True
    confirm_writes: bool = False
    read_only: bool = False
    dry_run: bool = False
    denied_paths: list[str] = Field(
        default_factory=lambda: [
            "**/.git/**",
            "**/.ssh/**",
            "**/.aws/**",
            "**/.gnupg/**",
            "**/.unknown/secrets/**",
            "**/*.pem",
            "**/*.key",
            "**/.env",
            "**/.env.*",
            "**/id_rsa*",
            "**/node_modules/**",
            "**/.venv/**",
            "**/site-packages/**",
        ]
    )
    command_denylist: list[str] = Field(
        default_factory=lambda: [
            r"\brm\s+-rf\s+/(\s|$)",
            r"\bmkfs(\.\w+)?\b",
            r"\bdd\s+if=",
            r":\(\)\s*\{.*\};\s*:",
            r"\bshutdown\b",
            r"\breboot\b",
            r"\bchown\s+-R\s+root",
            r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh",
            r"\bwget\b[^|]*\|\s*(sudo\s+)?(ba)?sh",
            r"\bgit\s+push\b[^\n]*--force",
            r"\bnpm\s+publish\b",
            r"\btwine\s+upload\b",
        ]
    )
    command_allowlist: list[str] = Field(default_factory=list, description="If non-empty, only these prefixes run.")
    max_write_bytes: int = Field(2_000_000, ge=1024)
    max_delete_bytes: int = Field(50_000_000, ge=0)
    allow_subprocess_env: bool = False


class GateSetting(_Base):
    enabled: bool = True
    required: bool | None = Field(
        None,
        description=(
            "Block the run when this gate fails. Unset (null) means: decide from "
            "agent.definition_of_done (strict = every gate, balanced = syntax/tests/build, "
            "advisory = none). Set it explicitly to override."
        ),
    )
    command: str | None = None
    timeout_s: float = Field(600.0, gt=0)
    fail_fast: bool = False


class ValidationConfig(_Base):
    """Quality gates that define 'done'."""

    gates: dict[str, GateSetting] = Field(default_factory=dict)
    fail_fast: bool = False
    auto_detect: bool = True
    parallel: bool = False
    browser_url: str | None = None
    browser_routes: list[str] = Field(default_factory=list)
    browser_flow_file: str | None = None
    max_output_lines: int = Field(200, ge=10)
    custom_gate_commands: dict[str, str] = Field(default_factory=dict)

    #: Gates that block a run per definition-of-done level.
    REQUIRED_BY_DOD: ClassVar[dict[str, set[str]]] = {
        "strict": {"syntax", "lint", "format", "typecheck", "tests", "build", "browser", "diff"},
        "balanced": {"syntax", "tests", "build"},
        "advisory": set(),
    }

    def gate(self, name: str) -> GateSetting:
        return self.gates.get(name, GateSetting())

    def is_required(self, name: str, definition_of_done: str) -> bool:
        """Resolve whether a gate blocks the run.

        Explicit configuration always wins; otherwise the definition-of-done level
        decides, so ``--dod advisory`` reports without blocking and ``--dod strict``
        refuses to declare success while any gate is red.
        """

        setting = self.gate(name)
        if setting.required is not None:
            return bool(setting.required)
        return name in self.REQUIRED_BY_DOD.get(definition_of_done, self.REQUIRED_BY_DOD["balanced"])


class BrowserConfig(_Base):
    """Playwright/Chromium validation settings."""

    enabled: bool = True
    browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    viewport_width: int = Field(1440, ge=320)
    viewport_height: int = Field(900, ge=240)
    device_scale_factor: float = 1.0
    timeout_ms: int = Field(30_000, ge=1000)
    navigation_timeout_ms: int = Field(45_000, ge=1000)
    base_url: str | None = None
    start_command: str | None = None
    start_timeout_s: float = Field(90.0, gt=0)
    flows_dir: str = ".unknown/flows"
    flow_file: str | None = Field(
        None,
        description="Flow the browser gate must execute (project-relative). Discovered flows live in flows_dir.",
    )
    screenshots: bool = True
    screenshot_on_failure: bool = True
    console_error_budget: int = Field(0, ge=0)
    failed_request_budget: int = Field(0, ge=0)
    trace: bool = False
    video: bool = False
    ignore_https_errors: bool = True
    record_har: bool = False
    extra_args: list[str] = Field(default_factory=list)


class MemoryConfig(_Base):
    """Layered memory behaviour."""

    backend: Literal["sqlite"] = "sqlite"
    auto_remember: bool = True
    remember_conventions: bool = True
    max_recall_items: int = Field(12, ge=1, le=100)
    max_fact_chars: int = Field(2000, ge=100)
    retention_days: int = Field(180, ge=0, description="0 disables pruning.")
    fts_enabled: bool = True
    write_markdown_mirror: bool = True
    working_memory_tokens: int = Field(6_000, ge=500)
    procedural_autoload: bool = True
    sessions_retention: int = Field(200, ge=10)


class LoggingConfig(_Base):
    level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    format: LogFormat = "text"
    file_enabled: bool = True
    file_max_bytes: int = Field(5_000_000, ge=10_000)
    file_backup_count: int = Field(5, ge=0, le=50)
    include_timestamps: bool = True
    log_tool_payloads: bool = False
    log_http_requests: bool = False


class TelemetryConfig(_Base):
    """Opt-in, local-first observability.  Off by default; no code or prompts leave the machine."""

    enabled: bool = False
    anonymous_id: str | None = None
    endpoint: str | None = None
    include_command_names: bool = True
    include_error_classes: bool = True
    local_metrics: bool = True


class PluginsConfig(_Base):
    enabled: bool = True
    allow_entry_points: bool = True
    disabled: list[str] = Field(default_factory=list)
    extra_paths: list[str] = Field(default_factory=list)
    auto_load_procedures: bool = True


class UiConfig(_Base):
    output: OutputMode = "human"
    color: bool = True
    unicode: bool = True
    animations: bool = True
    show_timings: bool = True
    diff_context_lines: int = Field(3, ge=0, le=20)
    pager: bool = False
    theme: str = "default"


class ToolsConfig(_Base):
    """Per-tool tuning that is user-visible and supported."""

    timeouts: dict[str, float] = Field(
        default_factory=lambda: {
            "shell_run": 900.0,
            "test_run": 1800.0,
            "build_run": 1800.0,
            "lint_run": 600.0,
            "typecheck_run": 600.0,
            "browser_flow": 180.0,
            "http_fetch": 60.0,
            "git_diff": 60.0,
        }
    )
    max_output_bytes: int = Field(400_000, ge=1000)
    truncate_head_lines: int = Field(120, ge=1)
    truncate_tail_lines: int = Field(160, ge=1)
    search_max_results: int = Field(200, ge=1)
    read_max_bytes: int = Field(600_000, ge=1000)


class ProjectConfig(_Base):
    """`.unknown/config.toml` project overrides and detected project facts."""

    name: str | None = None
    kind: str | None = None
    package_manager: str | None = None
    stack: list[str] = Field(default_factory=list)
    conventions: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    commands: dict[str, str] = Field(
        default_factory=dict,
        description="Named commands: dev, test, build, lint, format, typecheck, migrate, seed.",
    )
    dev_url: str | None = None
    protected_paths: list[str] = Field(default_factory=list)
    pointer_files: list[str] = Field(default_factory=lambda: ["AGENTS.md", "CLAUDE.md", "README.md", "CONTRIBUTING.md"])
    extra_instructions: str | None = None


class AppearanceConfig(_Base):
    """Visual identity of the product.

    The palette is the single source of truth for colour: the CLI (Rich), the TUI
    (Textual) and the previews all resolve ``theme`` through
    :mod:`unknown_agent.ui.theme`, so a theme change is one line of config and
    every surface follows.
    """

    theme: ThemeChoice = Field(
        "auto",
        description="Palette name, or auto/dark/light to resolve from the terminal environment.",
    )
    mode: ThemeMode | None = Field(None, description="Pin the light/dark mode used by theme = auto.")
    glyphs: GlyphSet = "unicode"
    color_depth: ColorDepth = "auto"
    density: UiDensity = "comfortable"
    motion: MotionLevel = "full"
    banner: bool = True
    compact_banner: bool = False
    accent_emoji: bool = False
    max_width: int = Field(120, ge=60, le=400)
    progress_rail: bool = Field(True, description="Show the step rail on the left of long output.")
    show_token_usage: bool = True
    show_cost: bool = True
    show_timings: bool = True

    @property
    def wants_motion(self) -> bool:
        return self.motion == "full"


class ReasoningConfig(_Base):
    """Reasoning / thinking depth.

    The ladder is a *product* concept, not a vendor one: ``medium`` … ``ultra``
    describe how much thinking Unknown Agent will spend, and
    :mod:`unknown_agent.reasoning` translates that into whatever each vendor
    actually supports (``reasoning_effort``, ``thinking.budget_tokens``,
    ``reasoning_tokens``, planning depth, verification strictness) — or into
    prompt-level scaffolding when the model has no native control at all.
    """

    default_mode: ReasoningMode = "high"
    #: Per-surface overrides, so a quick question never pays for deep thinking.
    planning_mode: ReasoningMode = "super"
    coding_mode: ReasoningMode = "high"
    reasoning_intensive_mode: ReasoningMode = "ultra"
    review_mode: ReasoningMode = "high"
    quick_mode: ReasoningMode = "medium"
    budget_tokens: int = Field(24_000, ge=0, le=200_000, description="Hard ceiling on thinking tokens per request.")
    auto_escalate: bool = Field(
        True,
        description="Raise depth automatically after a failed step or a blocked gate, then settle back down.",
    )
    escalate_on_failure: bool = True
    escalation_steps: int = Field(1, ge=0, le=3)
    show_thinking: bool = Field(True, description="Surface reasoning traces in the transcript (never sent back to the model as history).")
    persist_thinking: bool = Field(False, description="Store reasoning traces in the run journal.")
    thinking_style: Literal["concise", "structured", "exploratory"] = "structured"
    #: When a model has no native reasoning control, degrade honestly instead of
    #: pretending: ``scaffold`` adds explicit think/verify passes, ``ignore`` does not.
    fallback: Literal["scaffold", "ignore"] = "scaffold"
    max_alternatives: int = Field(3, ge=1, le=6, description="Candidate approaches considered in deep planning.")
    verification_strictness: Literal["lenient", "balanced", "strict"] = "balanced"

    #: Which configured rung each surface uses.  Surfaces not listed fall back to
    #: ``default_mode``, so adding a surface never requires a config migration.
    SURFACE_MODES: ClassVar[dict[str, str]] = {
        "plan": "planning_mode",
        "planning": "planning_mode",
        "architecture": "planning_mode",
        "code": "coding_mode",
        "coding": "coding_mode",
        "run": "coding_mode",
        "build": "coding_mode",
        "fix": "coding_mode",
        "refactor": "coding_mode",
        "review": "review_mode",
        "verify": "review_mode",
        "heal": "review_mode",
        "chat": "quick_mode",
        "ask": "quick_mode",
        "quick": "quick_mode",
        "clarify": "quick_mode",
        "summary": "quick_mode",
        "deep": "reasoning_intensive_mode",
        "reasoning_intensive": "reasoning_intensive_mode",
    }

    def mode_for_surface(self, surface: str) -> ReasoningMode:
        """The configured depth for one surface.

        This is the whole point of per-surface rungs: a quick question should never
        pay for the thinking budget of an architecture pass.  Unknown surfaces get
        ``default_mode`` rather than an error, because a new call site must not be
        able to break a run.
        """

        field_name = self.SURFACE_MODES.get((surface or "").strip().lower(), "default_mode")
        return getattr(self, field_name, self.default_mode)


class StreamingConfig(_Base):
    """Streaming behaviour and how streamed output is rendered."""

    enabled: bool = True
    partial: bool = Field(True, description="Show partial output live as tokens arrive.")
    style: StreamStyle = "live"
    token_pace: TokenPace = Field(
        "smoothed",
        description="raw = print as received, smoothed = even cadence, chunked = word/line batches.",
    )
    min_chunk_chars: int = Field(24, ge=1, le=512)
    flush_interval_ms: int = Field(60, ge=0, le=2000)
    max_buffer_chars: int = Field(200_000, ge=1_000)
    markdown: bool = Field(True, description="Render streamed markdown live (falls back to plain text on narrow terminals).")
    live_thinking: bool = Field(True, description="Stream reasoning deltas in their own dimmed lane.")
    live_tools: bool = Field(True, description="Stream tool-call arguments as they are generated.")
    spinner: bool = True
    spinner_style: str = "dots"
    show_tokens_per_second: bool = True
    interrupt_on_key: bool = Field(True, description="Esc/Ctrl-C stops generation but keeps the transcript.")
    keep_partial_on_interrupt: bool = True
    #: Machine output never streams decoration: --json/--ndjson force this off.
    quiet_when_machine: bool = True


class RoleSettings(_Base):
    """Tuning for one orchestration role (worker)."""

    enabled: bool = True
    provider: str | None = Field(None, description="Use a different provider profile for this role (e.g. a cheap model for docs).")
    model: str | None = Field(None, description="Pin a model for this role.")
    reasoning_mode: ReasoningMode | None = None
    max_steps: int | None = Field(None, ge=1, le=400)
    max_tokens: int | None = Field(None, ge=1_000)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    read_only: bool | None = Field(None, description="Force this role to non-mutating tools.")
    tools: list[str] = Field(default_factory=list, description="Extra tools granted on top of the role card.")
    deny_tools: list[str] = Field(default_factory=list)
    system_prompt_extra: str | None = None


class OrchestrationConfig(_Base):
    """Multi-worker orchestration.

    Unknown Agent is not one loop forever: a conductor inspects the work, picks a
    shape (solo / pipeline / fan-out / graph / escalate), delegates to specialised
    workers with bounded tools and budgets, merges their artefacts, verifies the
    result and escalates to deeper planning when reality disagrees.
    """

    enabled: bool = True
    mode: OrchestrationMode = "auto"
    min_complexity: int = Field(
        3,
        ge=1,
        le=10,
        description="Task complexity (1-10) at or above which orchestration is considered in auto mode.",
    )
    max_workers: int = Field(4, ge=1, le=16)
    concurrency: int = Field(2, ge=1, le=8, description="Workers executing at the same time (fan-out/graph).")
    pattern: OrchestrationPattern | None = Field(None, description="Force a shape instead of letting the router choose.")
    merge_strategy: MergeStrategy = "owner_partition"
    verify_each_worker: bool = Field(True, description="Run the relevant gates after every worker instead of only at the end.")
    max_replans: int = Field(2, ge=0, le=6)
    max_retries_per_worker: int = Field(2, ge=0, le=6)
    worker_max_steps: int = Field(24, ge=1, le=400)
    worker_token_budget: int = Field(180_000, ge=1_000)
    escalate_to_planner: bool = True
    escalate_on_repeated_failure: int = Field(2, ge=1, le=10)
    ledger: bool = Field(True, description="Append an audit record per delegation to .unknown/orchestration/.")
    roles: dict[str, RoleSettings] = Field(default_factory=dict)
    #: Roles that may run at all.  Empty means "every registered role".
    allow_roles: list[str] = Field(default_factory=list)
    deny_roles: list[str] = Field(default_factory=list)

    def role(self, name: str) -> RoleSettings:
        return self.roles.get(name, RoleSettings())


class SkillsConfig(_Base):
    """Skills, plugins and extensions.

    A *skill* is packaged expertise: instructions + tool grants + a definition of
    done + optional workflows, discovered from the package, the user dir, the
    project dir and plugins.  Skills are inert until enabled, and enabling one
    never silently widens permissions — grants are intersected with ``[policy]``.
    """

    enabled: bool = True
    #: Shipped skill pack.  ``default`` expands to the curated starter set.
    pack: list[str] = Field(default_factory=lambda: ["default"])
    enable: list[str] = Field(default_factory=list)
    disable: list[str] = Field(default_factory=list)
    extra_dirs: list[str] = Field(default_factory=list)
    auto_discover: bool = True
    allow_project_skills: bool = Field(True, description="Load skills from <repo>/.unknown/skills (prompt injection surface: reviewed on load).")
    inject_system_prompt: bool = True
    max_active: int = Field(12, ge=1, le=64)
    show_active_on_start: bool = False


class PlanningConfig(_Base):
    """Deep planning and the interactive clarification system."""

    depth: PlanningDepth = "standard"
    clarify: ClarifyMode = Field(
        "auto",
        description="auto = ask only when the request is genuinely underspecified.",
    )
    max_questions: int = Field(6, ge=0, le=24)
    batch_questions: bool = Field(True, description="Group related axes into one panel instead of one question at a time.")
    recommend_defaults: bool = True
    allow_custom_answers: bool = True
    allow_skip: bool = Field(True, description="Let the user accept every recommendation and move on.")
    require_confirmation: bool = Field(True, description="Show the assembled blueprint for approval before execution.")
    persist_blueprint: bool = Field(True, description="Write .unknown/blueprint.toml so decisions survive across runs.")
    blueprint_path: str = ".unknown/blueprint.toml"
    explore_alternatives: bool = True
    max_alternatives: int = Field(3, ge=1, le=6)
    explain_tradeoffs: bool = True
    risk_assessment: bool = True
    effort_estimation: bool = True
    remember_answers: bool = Field(True, description="Store decisions in project memory and reuse them next time.")
    interactive_ui: Literal["auto", "tui", "rich", "off"] = Field(
        "auto",
        description="auto = Textual panel when a TTY and textual are installed, else Rich prompts.",
    )


#: MCP transports the client can speak.  ``stdio`` launches a local process;
#: ``http`` is streamable HTTP (the modern remote transport); ``sse`` is the
#: legacy remote transport kept for servers that have not migrated yet.
McpTransport = Literal["stdio", "http", "sse"]

#: Trust is a ladder, not a boolean.  It decides what an MCP server's tools may
#: do *before* [policy] gets its say - policy can only narrow further.
#:   full     - tools run like builtins (still subject to [policy])
#:   trusted  - read tools run freely; mutating tools need confirmation
#:   sandboxed- every call needs confirmation; results are labelled untrusted
#:   blocked  - connected for inspection only; no tool is exposed to the agent
McpTrustLevel = Literal["full", "trusted", "sandboxed", "blocked"]


class McpServerConfig(_Base):
    """One configured MCP server.

    The key in ``[mcp.servers.<name>]`` is the server's product-facing name; tools
    it contributes are namespaced ``mcp.<name>.<tool>`` so two servers can ship a
    tool called ``search`` without colliding, and so an audit line always says
    where a capability came from.
    """

    enabled: bool = True
    transport: McpTransport = "stdio"
    #: stdio: the executable to launch (e.g. ``uvx``, ``npx``, ``python``).
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment for stdio servers. Values may be secret refs (env:VAR, store:key).",
    )
    cwd: str | None = None
    #: http / sse: where the server lives.
    url: str | None = None
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra HTTP headers (Authorization etc). Values may be secret refs.",
    )
    trust: McpTrustLevel = "trusted"
    #: Optional grouping label ("core", "web", "team-internal", ...) for bulk
    #: enable/disable and for the management UI.
    group: str = ""
    #: Only expose these tools (empty = all the server offers) ...
    allow_tools: list[str] = Field(default_factory=list)
    #: ... and never expose these, even if allowed above.
    deny_tools: list[str] = Field(default_factory=list)
    #: Cap for runaway servers: a server advertising 400 tools would drown the
    #: model's tool selection; the first N (after allow/deny) win.
    max_tools: int = Field(48, ge=1, le=256)
    startup_timeout_s: float = Field(20.0, gt=0)
    request_timeout_s: float = Field(60.0, gt=0)
    description: str = ""
    #: Free-form note shown by `ua mcp list` ("added by onboarding", "team infra").
    origin: str = ""

    @model_validator(mode="after")
    def _check_transport_fields(self) -> "McpServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio transport requires 'command'")
        if self.transport in {"http", "sse"} and not self.url:
            raise ValueError(f"{self.transport} transport requires 'url'")
        return self


class McpConfig(_Base):
    """Model Context Protocol integration.

    MCP is the product's expansion system: servers contribute tools, resources
    and prompts that join the builtin registry under the same policy engine.
    Nothing here runs unless ``enabled`` is true and the specific server is both
    enabled and non-blocked.
    """

    enabled: bool = True
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    #: Auto-connect at session start.  Off = servers connect lazily on first use,
    #: which keeps `ua run` startup fast when servers are slow to boot.
    autoconnect: bool = False
    #: Include MCP tools in the agent's registry by default.
    expose_tools: bool = True
    #: Expose MCP resources through the context builder (repo-external knowledge).
    expose_resources: bool = True
    #: Expose MCP prompts as slash-command style presets in the shell/TUI.
    expose_prompts: bool = True
    #: Fail-soft: a server that cannot connect is reported and skipped; it never
    #: aborts a run.  Turning this off makes a dead server a hard error.
    fail_soft: bool = True
    #: Ceiling across every server, after per-server caps.
    max_total_tools: int = Field(120, ge=1, le=512)
    health_check_interval_s: float = Field(0.0, ge=0.0, description="0 disables periodic pings.")
    #: Default trust for servers added without an explicit level.
    default_trust: McpTrustLevel = "trusted"


#: Where an image is allowed to come from, in the formats providers accept.
VisionSource = Literal["file", "data-url", "url"]


class VisionConfig(_Base):
    """Vision-capable workflows: screenshots, mockups, design comparison.

    Vision is a *capability-gated* feature: every entry point first asks the
    provider capability model whether the active model accepts images.  When it
    does not, the product either routes to a configured fallback vision profile
    or explains the limitation - it never sends images to a blind model.
    """

    enabled: bool = True
    #: Provider profile to route image analysis to when the active model is not
    #: vision-capable (e.g. a cheap multimodal profile).  Empty = no fallback.
    fallback_profile: str = ""
    #: Hard cap before base64 encoding; bigger files are downscaled when Pillow
    #: is available, otherwise refused with a clear message.
    max_image_bytes: int = Field(5_000_000, ge=100_000)
    max_images_per_request: int = Field(4, ge=1, le=16)
    #: Downscale target for oversized screenshots (longest edge, px).
    downscale_to_px: int = Field(1568, ge=256)
    allowed_sources: list[VisionSource] = Field(default_factory=lambda: ["file", "data-url", "url"])
    #: Remote URLs are a data-exfiltration surface in agent loops; off by default
    #: for tool-initiated fetches, on for user-initiated ones.
    allow_remote_urls_in_tools: bool = False
    #: Attach browser screenshots to the model turn after a failed browser gate.
    auto_attach_browser_evidence: bool = True
    #: Store analysed images in the run's artifacts dir for the audit trail.
    keep_copies_in_artifacts: bool = True


class ComputerUseConfig(_Base):
    """Adaptive UI interaction (computer use), distinct from scripted browser flows.

    Browser automation replays a deterministic flow and asserts on the result.
    Computer use is a *loop*: observe (screenshot) -> decide (model) -> act
    (click/type/scroll) -> observe.  It is powerful and therefore off by default,
    bounded per session, and every action is journalled.
    """

    enabled: bool = False
    #: Which backend drives the interaction.  ``browser`` uses the Playwright
    #: page as the display+input surface (works everywhere Playwright does);
    #: ``native`` is reserved for OS-level drivers (not shipped yet).
    driver: Literal["browser", "native"] = "browser"
    #: Confirmation policy for actions.  ``mutating`` confirms clicks/typing but
    #: not scrolling/reading; ``all`` confirms everything; ``none`` trusts the
    #: session policy alone (still subject to [policy] and trust).
    confirm_actions: Literal["all", "mutating", "none"] = "mutating"
    max_actions_per_task: int = Field(40, ge=1, le=400)
    max_minutes_per_task: float = Field(10.0, gt=0)
    screenshot_every_action: bool = True
    #: Providers that expose a native computer-use tool API get it; others fall
    #: back to the generic act/observe tool pair.
    prefer_native_apis: bool = True
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description="If non-empty, the driver may only navigate to these hosts.",
    )
    #: Keystrokes that are never sent regardless of what the model asks for.
    denied_keys: list[str] = Field(default_factory=lambda: ["Meta", "Cmd", "Ctrl+Alt+Delete"])


class WorkflowsConfig(_Base):
    """Contextual guided workflows (generated mini-tools)."""

    enabled: bool = True
    dir: str = ".unknown/workflows"
    auto_generate: bool = Field(True, description="Let the agent synthesise a guided workflow when a task would benefit from one.")
    max_steps: int = Field(14, ge=2, le=40)
    allow_custom: bool = True
    save_generated: bool = True
    confirm_before_run: bool = True
    library: list[str] = Field(default_factory=list, description="Extra workflow spec files/dirs to load.")


class Settings(_Base):
    """Root settings object assembled by :mod:`unknown_agent.config.loader`."""

    schema_version: int = 1
    provider: str = "default"
    providers: dict[str, ProviderConfig] = Field(default_factory=lambda: {"default": ProviderConfig()})
    agent: AgentConfig = Field(default_factory=AgentConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    appearance: AppearanceConfig = Field(default_factory=AppearanceConfig)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    planning: PlanningConfig = Field(default_factory=PlanningConfig)
    workflows: WorkflowsConfig = Field(default_factory=WorkflowsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    computer_use: ComputerUseConfig = Field(default_factory=ComputerUseConfig)

    @model_validator(mode="after")
    def _promote_legacy_theme(self) -> "Settings":
        """Keep ``ui.theme`` (legacy) and ``appearance.theme`` from disagreeing.

        ``appearance`` is the source of truth; a legacy ``ui.theme`` that was
        actually set (anything other than its ``"default"`` sentinel) is promoted
        into it, so a config written by an earlier release keeps its identity
        instead of silently reverting to ``auto``.  The legacy field is left
        untouched and simply no longer consulted for colour.
        """

        from unknown_agent.ui.theme_defs import PALETTE_ORDER

        legacy = (self.ui.theme or "").strip().lower()
        if not legacy or legacy in {"default", "auto"}:
            return self
        if self.appearance.theme != "auto":
            return self
        if legacy in {"dark", "light"} or legacy in PALETTE_ORDER:
            self.appearance.theme = legacy  # type: ignore[assignment]
        return self

    @field_validator("providers")
    @classmethod
    def _require_default_provider(cls, value: dict[str, ProviderConfig]) -> dict[str, ProviderConfig]:
        return value or {"default": ProviderConfig()}

    # -- convenience --------------------------------------------------------
    @property
    def active_provider(self) -> ProviderConfig:
        return self.providers.get(self.provider) or next(iter(self.providers.values()))


__all__ = [
    "Settings",
    "McpConfig",
    "McpServerConfig",
    "McpTransport",
    "McpTrustLevel",
    "VisionConfig",
    "ComputerUseConfig",
    "AppearanceConfig",
    "ReasoningConfig",
    "StreamingConfig",
    "OrchestrationConfig",
    "RoleSettings",
    "SkillsConfig",
    "PlanningConfig",
    "WorkflowsConfig",
    "ProviderConfig",
    "AgentConfig",
    "PolicyConfig",
    "ValidationConfig",
    "GateSetting",
    "BrowserConfig",
    "MemoryConfig",
    "LoggingConfig",
    "TelemetryConfig",
    "PluginsConfig",
    "ToolsConfig",
    "UiConfig",
    "ProjectConfig",
]
