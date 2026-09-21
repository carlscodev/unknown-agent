"""The first-run experience: a wizard, not a questionnaire.

Twelve steps, one screen, a rail on the left that always tells you where you are
and lets you jump back.  Nothing is written to disk until the review step, so an
abandoned setup leaves the previous configuration untouched.

The steps are deliberately ordered by *cost of getting it wrong*:

1. **welcome**   what this is, and what will be asked of you
2. **theme**     the cheapest choice, and the one that makes the rest pleasant
3. **provider**  where the intelligence comes from
4. **endpoint**  only when the provider needs one (custom / OpenAI-compatible)
5. **api key**   captured masked, validated against the vendor's key shape, then
                 live-checked against the real API before anything depends on it
6. **model**     fetched live, ranked by capability and cost, with the metadata
                 that makes the choice informed
7. **reasoning** how hard it thinks, and how the answer streams back
8. **browser**   optional, and only offered when Playwright can actually run
9. **skills**    the built-in pack, and how to add your own
10. **safety**   permissions, with a risk label on every row
11. **project**  what to work on first (skipped when there is no project)
12. **review**   everything on one screen, then write

Design rules:

* Every step states its consequence before asking for a decision.
* No step can be completed by accident: Enter confirms, Escape goes back.
* Network calls happen only in steps 5 and 6, both with a visible retry path and
  an offline fallback — a broken network must not brick setup.
* The wizard is testable headless: every step is a widget, and the app exposes
  ``goto``/``complete_current`` so a test can drive it with a Pilot.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from dataclasses import dataclass, field
from typing import Any, Sequence

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, LoadingIndicator, OptionList, Static, Switch
from textual.widgets.option_list import Option

from unknown_agent.config.credentials import CredentialStore
from unknown_agent.config.models import Settings
from unknown_agent.observability.logging import get_logger
from unknown_agent.onboarding.detect import NEEDS_SETUP, SetupState, detect_setup_state
from unknown_agent.providers.catalog import CATALOG, ProviderEntry, profile_name_for, resolve_entry
from unknown_agent.providers.discovery import ModelInfo, ProviderProbeError, fetch_models, validate_credential
from unknown_agent.reasoning import MODES, MODE_ORDER
from unknown_agent.skills.loader import SKILL_FILE_TEMPLATE, packs_payload
from unknown_agent.tui.themes import apply_to_app, theme_name
from unknown_agent.tui.widgets import (
    ApiKeyInput,
    CapabilityBadges,
    GridChoice,
    KeyHints,
    ModelTable,
    OptionGrid,
    PanelCard,
    ReasoningLadder,
    StepRail,
    SwatchRow,
    SwitchGrid,
    SwitchItem,
    rank_models,
)
from unknown_agent.ui.theme_defs import PALETTE_ORDER, get_palette

log = get_logger("tui.onboarding")

__all__ = [
    "WizardState",
    "OnboardingApp",
    "run_onboarding_tui",
    "STEP_ORDER",
]


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
@dataclass
class WizardState:
    """Everything the wizard collects.  Nothing here touches disk until ``apply``."""

    settings: Settings = field(default_factory=Settings)
    #: Setup detection that routed us here (first run vs. repair).
    state: SetupState | None = None
    repair: bool = False

    theme_key: str = "auto"
    glyphs: str = "unicode"
    motion: str = "full"

    entry: ProviderEntry | None = None
    custom_entry: bool = False
    base_url: str = ""
    profile_name: str = ""
    api_key: str = ""
    credential_ref: str = ""
    key_source: str = ""  # "pasted" | "env" | "existing" | "keyless"
    validation_message: str = ""
    models: list[ModelInfo] = field(default_factory=list)
    models_source: str = ""  # "live" | "catalog" | "typed"
    model: str = ""

    reasoning_mode: str = "high"
    planning_depth: str = "deep"
    streaming: bool = True
    stream_style: str = "live"

    browser_enabled: bool = False
    browser_headless: bool = True
    browser_channel: str = "chromium"
    browser_available: bool = False
    browser_reason: str = ""

    skills_pack: str = "default"
    skills_extra_dir: str = ""

    allow_network: bool = True
    allow_destructive: bool = False
    confirm_destructive: bool = True
    confirm_writes: bool = False
    allow_outside_workspace: bool = False

    first_task: str = ""
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    def fail(self, text: str) -> None:
        if text and text not in self.errors:
            self.errors.append(text)

    @property
    def provider_key(self) -> str:
        return self.entry.key if self.entry else ""

    def summary(self) -> list[tuple[str, str]]:
        entry = self.entry
        rows = [
            ("theme", get_palette(None if self.theme_key == "auto" else self.theme_key).label
             + (" (auto)" if self.theme_key == "auto" else "")),
            ("provider", entry.label if entry else "—"),
            ("model", self.model or "—"),
            ("credential", self.credential_ref or ("none required" if entry and not entry.api_key_required else "—")),
            ("reasoning", MODES[self.reasoning_mode].label if self.reasoning_mode in MODES else self.reasoning_mode),
            ("planning", self.planning_depth),
            ("streaming", self.stream_style if self.streaming else "off"),
            ("browser", f"{self.browser_channel}{' headless' if self.browser_headless else ''}" if self.browser_enabled else "disabled"),
            ("skills", self.skills_pack),
            (
                "permissions",
                "network " + ("on" if self.allow_network else "off")
                + " · destructive " + ("allowed" if self.allow_destructive else "blocked")
                + (" (confirmed)" if self.allow_destructive and self.confirm_destructive else ""),
            ),
        ]
        if self.base_url:
            rows.insert(2, ("endpoint", self.base_url))
        return rows


#: Step keys in order.  ``required=False`` steps can be skipped by the flow.
STEP_ORDER: tuple[tuple[str, str, bool], ...] = (
    ("welcome", "Welcome", True),
    ("theme", "Theme", True),
    ("provider", "Provider", True),
    ("endpoint", "Endpoint", False),
    ("apikey", "API key", True),
    ("model", "Model", True),
    ("reasoning", "Reasoning", True),
    ("browser", "Browser", False),
    ("skills", "Skills", True),
    ("safety", "Safety", True),
    ("project", "First task", False),
    ("review", "Review", True),
)
STEP_KEYS = tuple(key for key, _, _ in STEP_ORDER)
STEP_LABELS = tuple(label for _, label, _ in STEP_ORDER)


# --------------------------------------------------------------------------- #
# base step
# --------------------------------------------------------------------------- #
class WizardStep(VerticalScroll):
    """A single step of the wizard.

    Subclasses override :meth:`build` to compose their content and set
    :attr:`can_continue` / :attr:`blocking_message` to gate the continue button.
    The app drives navigation; a step only ever reports what it collected.
    """

    DEFAULT_CSS = """
    WizardStep {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    WizardStep .step-title {
        color: $primary;
        text-style: bold;
        content-align: left middle;
        height: 1;
        width: 100%;
    }
    WizardStep .step-subtitle {
        color: $text-muted;
        height: auto;
        width: 100%;
        margin-bottom: 1;
    }
    """

    #: Overridden by subclasses that must do work before continuing.
    requires_confirmation = False

    def __init__(self, app_state: WizardState, *, id: str | None = None) -> None:
        # The id must be unique per step: a step is mounted before its predecessor
        # is removed, so two "wizard-step" nodes would collide.
        super().__init__(id=id or f"step-{type(self).__name__}")
        self.wizard = app_state

    def build(self) -> ComposeResult:  # pragma: no cover - abstract
        yield Static("")

    def compose(self) -> ComposeResult:
        yield from self.build()

    def header(self, title: str, subtitle: str = "") -> ComposeResult:
        yield Static(title, classes="step-title")
        if subtitle:
            yield Static(subtitle, classes="step-subtitle")

    # -- gating ------------------------------------------------------------ #
    #: Set once the step's own widgets exist.  ``_update_nav`` runs right after a
    #: mount is scheduled, so gating logic must be able to say "not ready yet"
    #: instead of raising on a query that cannot match.
    ready = False

    def on_mount(self) -> None:
        self.ready = True

    def _find(self, selector: str, expected: type | None = None) -> Any:
        """``query_one`` that returns ``None`` while the step is still composing."""

        try:
            if expected is None:
                return self.query_one(selector)
            return self.query_one(selector, expected)
        except Exception:
            return None

    def can_continue(self) -> bool:
        return True

    def blocking_message(self) -> str:
        return ""

    def collect(self) -> None:
        """Pull values out of the widgets into :attr:`wizard`."""

    def on_continue(self) -> bool:
        """Called when the user presses continue.  Return False to stay."""

        self.collect()
        return True

    def on_leave(self) -> None:
        """Called when the step loses focus (back or continue)."""

    async def refresh_dynamic(self) -> None:
        """Re-evaluate labels that depend on other steps."""


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #
class WelcomeStep(WizardStep):
    def build(self) -> ComposeResult:
        yield from self.header("Unknown Agent", "A hyperagent for building software. Eleven choices, then it works.")
        state = self.wizard.state
        if self.wizard.repair and state is not None:
            yield PanelCard(
                "repair mode",
                "Something in your setup stopped working. We will fix only what is broken and keep everything else.",
                Static("\n".join(f"• {problem}" for problem in (state.problems or [state.reason])), id="repair-problems"),
            )
        else:
            yield PanelCard(
                "what happens next",
                "Nothing is written until the review step. Escape goes back at any point; quitting changes nothing.",
                Static(
                    "\n".join(
                        f"[bold]{index + 1:>2}[/]  {label}{'' if required else '  [dim](optional)[/]'}"
                        for index, (label, required) in enumerate(
                            [(label, required) for _, label, required in STEP_ORDER[1:]]
                        )
                    ),
                    id="step-preview",
                ),
            )
        yield PanelCard(
            "your machine",
            Static(
                f"{platform.system()} {platform.release()} · python {platform.python_version()} · "
                f"terminal {os.environ.get('TERM_PROGRAM') or os.environ.get('TERM') or 'unknown'} · "
                f"colour {'truecolor' if os.environ.get('COLORTERM') == 'truecolor' else '256'}\n"
                f"config will be written to [bold]~/.config/unknown-agent/config.toml[/]",
                id="env-line",
            ),
        )

    def on_continue(self) -> bool:
        self.wizard.note("welcome acknowledged")
        return True


class ThemeStep(WizardStep):
    """Pick the palette — applied live, so you choose with your eyes."""

    def build(self) -> ComposeResult:
        yield from self.header(
            "Theme",
            "Both surfaces share one palette, so the CLI and this interface always look like the same product.",
        )
        choices = [GridChoice("auto", "Auto", "Follow the terminal: dark palettes at night, light in daylight.",
                              badge="bold green]recommended", selected=self.wizard.theme_key == "auto")]
        for key in PALETTE_ORDER:
            palette = get_palette(key)
            choices.append(
                GridChoice(
                    key,
                    palette.label,
                    palette.tagline,
                    badge=f"{'dark' if palette.is_dark else 'light'}",
                    selected=self.wizard.theme_key == key,
                )
            )
        yield OptionGrid(choices, title="palette", id="theme-grid")
        yield SwatchRow(self.wizard.theme_key, id="theme-swatch")
        yield OptionGrid(
            [
                GridChoice("unicode", "Unicode glyphs", "✓ › ● — the default. Needs any modern terminal."),
                GridChoice("ascii", "ASCII glyphs", "Plain markers. For terminals and SSH clients that mangle Unicode."),
                GridChoice("nerd", "Nerd Font glyphs", "Richer icons. Only if your terminal font is a Nerd Font."),
            ],
            title="glyphs",
            id="glyph-grid",
        )

    def on_mount(self) -> None:
        super().on_mount()
        self._preview()

    def _preview(self) -> None:
        key = self._find("#theme-grid", OptionGrid).value
        resolved = get_palette(None if key == "auto" else key)
        self._find("#theme-swatch", SwatchRow).set_palette(resolved.key)
        self.app.theme = theme_name(resolved.key)

    @on(OptionGrid.Highlighted, "#theme-grid")
    def _hover(self, event: OptionGrid.Highlighted) -> None:
        choice = event.choice
        if choice is None:
            return
        key = "auto" if choice.value == "auto" else choice.value
        resolved = get_palette(None if key == "auto" else key)
        self._find("#theme-swatch", SwatchRow).set_palette(resolved.key)
        # Live theming: the decision is visible before it is committed.
        self.app.theme = theme_name(resolved.key)

    def collect(self) -> None:
        theme = self._find("#theme-grid", OptionGrid)
        glyphs = self._find("#glyph-grid", OptionGrid)
        if theme is not None:
            self.wizard.theme_key = theme.value or "auto"
        if glyphs is not None:
            self.wizard.glyphs = glyphs.value or "unicode"


class ProviderStep(WizardStep):
    def build(self) -> ComposeResult:
        yield from self.header(
            "Provider",
            "Where the intelligence comes from. Every entry is a real adapter — protocol, auth, model listing and "
            "reasoning controls included.",
        )
        existing = {name: profile.type for name, profile in (self.wizard.settings.providers or {}).items()}
        choices: list[GridChoice] = []
        for entry in CATALOG:
            badge = ""
            if entry.local:
                badge = "bold green]local · no key"
            elif not entry.api_key_required:
                badge = "green]keyless"
            elif entry.key in existing.values():
                badge = "yellow]configured"
            implications = [
                f"protocol: {entry.protocol}",
                f"models: {entry.model_listing}",
                f"endpoint: {entry.base_url or 'you provide it'}",
            ]
            if entry.notes:
                implications.append(entry.notes)
            choices.append(
                GridChoice(
                    entry.key,
                    entry.label,
                    f"{entry.credential_hint or 'API key from the vendor dashboard.'}  ·  {entry.docs_url}",
                    badge=badge,
                    implies=implications,
                    selected=self.wizard.provider_key == entry.key,
                )
            )
        choices.append(
            GridChoice(
                "__custom__",
                "Custom OpenAI-compatible endpoint",
                "vLLM, LiteLLM, TGI, Ollama's OpenAI port, a company gateway, a proxy — anything that speaks the "
                "OpenAI chat-completions dialect.",
                badge="bold magenta]bring your own",
                implies=["you supply the base URL and auth header", "capability flags are yours to set"],
            )
        )
        yield OptionGrid(choices, title="choose a provider", id="provider-grid")
        yield Static("", id="provider-detail")

    @on(OptionGrid.Highlighted, "#provider-grid")
    def _hover(self, event: OptionGrid.Highlighted) -> None:
        detail = self._find("#provider-detail", Static)
        choice = event.choice
        if choice is None:
            detail.update("")
            return
        entry = resolve_entry(choice.value)
        detail.update(CapabilityBadges.from_entry(entry) if entry else "flags are set on the next screen")

    def collect(self) -> None:
        grid = self._find("#provider-grid", OptionGrid)
        if grid is None:
            return
        value = grid.value
        self.wizard.custom_entry = value == "__custom__"
        self.wizard.entry = None if self.wizard.custom_entry else resolve_entry(value)


class EndpointStep(WizardStep):
    """Custom provider configuration: endpoint, auth and capability flags."""

    def build(self) -> ComposeResult:
        yield from self.header(
            "Custom endpoint",
            "This is a first-class provider, not a workaround: it goes through the same adapter registry, credential "
            "store and model discovery as the catalog vendors.",
        )
        yield PanelCard(
            "endpoint",
            "The base URL of an OpenAI-compatible server. Include the version path if your server expects it.",
            Input(value=self.wizard.base_url, placeholder="https://llm.internal.example.com/v1", id="base-url"),
            Static("[dim]examples: http://localhost:8000/v1 (vLLM) · http://localhost:11434/v1 (Ollama) · https://litellm.internal/v1[/]", id="endpoint-hint"),
        )
        yield PanelCard(
            "display name",
            "How this provider is named in your config and in the model selector.",
            Input(value=self.wizard.profile_name, placeholder="internal-llm", id="custom-name"),
        )
        yield PanelCard(
            "capabilities",
            "Tell the agent what your endpoint can do. Wrong flags mean silent degradation — tools off, no streaming, "
            "no reasoning — so it is worth thirty seconds.",
            SwitchGrid(
                [
                    SwitchItem("tools", "Tool calling", "Required for the agent to edit files and run commands.", value=True),
                    SwitchItem("streaming", "Streaming", "Tokens arrive as they are produced.", value=True),
                    SwitchItem("reasoning", "Reasoning control", "Accepts an effort/thinking-budget parameter.", value=False),
                    SwitchItem("vision", "Vision", "Accepts image inputs.", value=False),
                    SwitchItem("json", "JSON mode", "Supports response_format=json_object.", value=True),
                    SwitchItem("keyless", "No API key", "Local servers that do not authenticate.", value=False, risk="unauthenticated"),
                ],
                id="custom-flags",
            ),
        )
        yield PanelCard(
            "auth header",
            "Most gateways want ``Authorization: Bearer <key>``. Override only if yours differs.",
            Input(value="", placeholder="X-Api-Key  (leave empty for Bearer)", id="auth-header"),
        )

    def can_continue(self) -> bool:
        field = self._find("#base-url", Input)
        if field is None:
            return False
        url = field.value.strip()
        return url.startswith(("http://", "https://")) or url.startswith("unix:")

    def blocking_message(self) -> str:
        return "a base URL is required — it must start with http:// or https://"

    def collect(self) -> None:
        from unknown_agent.providers.catalog import ProviderEntry

        base_field = self._find("#base-url", Input)
        if base_field is None:
            return
        base = base_field.value.strip()
        name = (self._find("#custom-name", Input) or _Blank()).value.strip() or "custom"
        flags = (self._find("#custom-flags", SwitchGrid) or _BlankSwitches()).values()
        header = (self._find("#auth-header", Input) or _Blank()).value.strip()
        extra_headers = {header.split(":")[0].strip(): ""} if header and ":" in header else {}
        self.wizard.base_url = base
        self.wizard.profile_name = name
        self.wizard.entry = ProviderEntry(
            key=_safe_profile_name(name),
            label=name,
            protocol="openai-compatible",
            base_url=base,
            env_var=f"{name.upper().replace('-', '_')}_API_KEY",
            docs_url=base,
            credential_hint="key from your gateway" if not flags.get("keyless") else "no key required",
            api_key_required=not flags.get("keyless", False),
            model_listing="openai-compatible",
            models_path="/models",
            default_model="",
            supports_tools=bool(flags.get("tools", True)),
            supports_streaming=bool(flags.get("streaming", True)),
            supports_json_mode=bool(flags.get("json", True)),
            supports_reasoning=bool(flags.get("reasoning", False)),
            supports_vision=bool(flags.get("vision", False)),
            local=base.startswith(("http://localhost", "http://127.0.0.1", "http://0.0.0.0")),
            requires_base_url=True,
            extra_headers=extra_headers,
            notes="custom provider configured during setup",
        )
        self.wizard.custom_entry = True
        self.wizard.note(f"custom endpoint {base}")


class ApiKeyStep(WizardStep):
    """Capture, then live-validate — with an honest offline path."""

    def __init__(self, app_state: WizardState, *, id: str | None = None) -> None:
        super().__init__(app_state, id=id)
        self.checking = False

    def build(self) -> ComposeResult:
        entry = self.wizard.entry
        env_value = ""
        if entry and entry.env_var:
            env_value = os.environ.get(entry.env_var, "")
        existing_ref = ""
        if self.wizard.repair and self.wizard.state is not None:
            existing_ref = self.wizard.state.reason

        yield from self.header(
            "API key",
            "Masked as you type. The value goes to the credential store with 0600 permissions — never to config.toml, "
            "never to a log.",
        )
        if entry is not None and not entry.api_key_required:
            yield PanelCard(
                "no key needed",
                Static(
                    f"[bold]{entry.label}[/] does not require a credential. Continue to fetch its model list.",
                    id="keyless-note",
                ),
                Button("continue", id="keyless-continue", variant="primary"),
            )
            return

        yield ApiKeyInput(
            vendor=entry.key.split(":")[0] if entry else "custom",
            env_var=entry.env_var if entry else "",
            existing_hint=(
                f"found ${entry.env_var} in your environment — press [bold]use env[/] to keep it out of the store"
                if env_value and entry
                else (existing_ref or "masked — only a fingerprint is ever shown")
            ),
            id="key-input-widget",
        )
        with Horizontal(id="key-actions"):
            if env_value:
                yield Button(f"use ${entry.env_var if entry else 'ENV'}", id="use-env", variant="success")
            yield Button("validate now", id="validate-key", variant="primary")
            yield Button("skip validation", id="skip-validate")
        yield Static("", id="key-status")
        if env_value:
            yield Static(
                f"[dim]${entry.env_var if entry else 'ENV'} is set in this shell "
                f"({_fingerprint(env_value)}). Using it means the secret stays in your environment instead of our store.[/]",
                id="env-note",
            )

    def can_continue(self) -> bool:
        entry = self.wizard.entry
        if entry is not None and not entry.api_key_required:
            return True
        try:
            widget = self._find("#key-input-widget", ApiKeyInput)
        except Exception:  # pragma: no cover - not mounted yet
            return bool(self.wizard.credential_ref)
        return bool(widget.value) or bool(self.wizard.credential_ref)

    def blocking_message(self) -> str:
        return "paste an API key, use the one in your environment, or go back and pick a keyless provider"

    @on(ApiKeyInput.Changed, "#key-input-widget")
    def _changed(self, event: ApiKeyInput.Changed) -> None:
        status = self._find("#key-status", Static)
        if event.verdict == "error":
            status.update(f"[bold red]✕ {event.detail}[/]")
        elif event.verdict == "warn":
            status.update(f"[yellow]! {event.detail}[/]")
        elif event.verdict == "ok":
            status.update(f"[green]✓ {event.detail}[/] — press [bold]validate now[/] to check it against the API")
        else:
            status.update("")

    @on(Button.Pressed, "#use-env")
    def _use_env(self, event: Button.Pressed) -> None:
        entry = self.wizard.entry
        if not entry:
            return
        self.wizard.api_key = os.environ.get(entry.env_var, "")
        self.wizard.credential_ref = f"env:{entry.env_var}"
        self.wizard.key_source = "env"
        self._find("#key-status", Static).update(
            f"[green]✓ using ${entry.env_var} — the secret stays in your environment[/]"
        )

    @on(Button.Pressed, "#keyless-continue")
    def _keyless(self, event: Button.Pressed) -> None:
        self.wizard.credential_ref = ""
        self.wizard.key_source = "keyless"
        self.app.action_continue()

    @on(Button.Pressed, "#skip-validate")
    def _skip(self, event: Button.Pressed) -> None:
        self.collect()
        self.wizard.note("credential validation skipped by the user")
        self._find("#key-status", Static).update(
            "[yellow]! not validated — if the key is wrong, the first request will fail with a clear message[/]"
        )

    @on(Button.Pressed, "#validate-key")
    def _validate(self, event: Button.Pressed) -> None:
        self.collect()
        if not self.wizard.api_key and not self.wizard.credential_ref:
            self._find("#key-status", Static).update("[red]✕ nothing to validate yet[/]")
            return
        self._run_validation()

    @work(thread=False)
    async def _run_validation(self) -> None:
        entry = self.wizard.entry
        if entry is None:
            return
        status = self._find("#key-status", Static)
        self.checking = True
        status.update("[dim]⟳ talking to " + entry.label + " ...[/]")
        try:
            result = await validate_credential(entry, api_key=self.wizard.api_key or None, base_url=self.wizard.base_url or None)
        except Exception as exc:  # pragma: no cover - defensive
            result = None
            self.wizard.fail(f"credential validation crashed: {exc}")
        self.checking = False
        if result is None:
            status.update("[red]✕ validation could not run — see the log[/]")
            return
        if result.ok:
            self.wizard.validation_message = result.message
            self.wizard.note(f"credential validated against {entry.label}")
            status.update(f"[bold green]✓ {result.message}[/]")
        else:
            self.wizard.fail(result.message)
            hint = f"\n[dim]{result.hint}[/]" if result.hint else ""
            status.update(f"[bold red]✕ {result.message}[/]{hint}")

    def collect(self) -> None:
        try:
            widget = self._find("#key-input-widget", ApiKeyInput)
        except Exception:
            # Keyless provider: the step renders no input at all.
            self.wizard.credential_ref = self.wizard.credential_ref or ""
            self.wizard.key_source = self.wizard.key_source or "keyless"
            return
        value = widget.value
        if value:
            self.wizard.api_key = value
            self.wizard.key_source = "pasted"
            entry = self.wizard.entry
            self.wizard.credential_ref = f"store:{_safe_profile_name(entry.key) if entry else 'default'}"


class ModelStep(WizardStep):
    """Fetch the real model list, rank it, show the metadata that matters."""

    def build(self) -> ComposeResult:
        yield from self.header(
            "Model",
            "Fetched from the provider, ranked tool-capable first, then cheap and large-context. The metadata is the "
            "point: context window decides whether your repo fits, tool support decides whether the agent can act.",
        )
        with Horizontal(id="model-toolbar"):
            yield Input(placeholder="filter models …", id="model-filter")
            yield Button("refresh list", id="refresh-models")
        yield Static("", id="model-status")
        yield ModelTable(id="model-table")
        yield Static("", id="model-detail")
        yield Input(placeholder="…or type a model id / deployment name", id="model-typed")

    def on_mount(self) -> None:
        super().on_mount()
        self._load(self.wizard.models)
        if not self.wizard.models:
            self._fetch_models()

    def _load(self, models: Sequence[ModelInfo]) -> None:
        table = self._find("#model-table", ModelTable)
        query = self._find("#model-filter", Input).value.strip()
        ranked = rank_models(models, query=query)
        table.load(ranked, selected=self.wizard.model)
        self._find("#model-status", Static).update(
            f"[dim]{len(ranked)} of {len(models)} models"
            + (f" · source: {self.wizard.models_source}" if self.wizard.models_source else "")
            + "[/]"
        )
        if self.wizard.model:
            self._find("#model-typed", Input).value = self.wizard.model

    @on(Input.Changed, "#model-filter")
    def _filter(self, event: Input.Changed) -> None:
        self._load(self.wizard.models)

    @on(Button.Pressed, "#refresh-models")
    def _refresh(self, event: Button.Pressed) -> None:
        self._fetch_models()

    @work(thread=False)
    async def _fetch_models(self) -> None:
        entry = self.wizard.entry
        if entry is None:
            return
        status = self._find("#model-status", Static)
        status.update(f"[dim]⟳ fetching models from {entry.label} ...[/]")
        api_key = self.wizard.api_key or None
        try:
            models = await fetch_models(entry, api_key=api_key, base_url=self.wizard.base_url or None)
        except ProviderProbeError as exc:
            models = []
            self.wizard.note(f"live listing failed ({exc.message}); using the curated catalog list")
        except Exception as exc:  # pragma: no cover - defensive
            models = []
            self.wizard.note(f"live listing failed ({exc}); using the curated catalog list")
        if models:
            self.wizard.models = list(models)
            self.wizard.models_source = "live"
        else:
            from unknown_agent.providers.catalog import known_model_ids

            self.wizard.models = [
                ModelInfo(id=model_id, name=model_id, provider=entry.key) for model_id in known_model_ids(entry)
            ]
            self.wizard.models_source = "catalog"
        self._load(self.wizard.models)

    @on(ModelTable.ModelChosen, "#model-table")
    def _chosen(self, event: ModelTable.ModelChosen) -> None:
        self.wizard.model = event.model_id
        self._find("#model-typed", Input).value = event.model_id
        info = next((item for item in self.wizard.models if item.id == event.model_id), None)
        detail = self._find("#model-detail", Static)
        if info is None:
            detail.update("")
            return
        lines = [CapabilityBadges.from_model(info)]
        if info.name and info.name != info.id:
            lines.append(f"[dim]{info.name}[/]")
        if info.max_output_tokens:
            lines.append(f"[dim]max output: {info.max_output_tokens:,} tokens[/]")
        detail.update("\n".join(line for line in lines if line))

    @on(Input.Submitted, "#model-typed")
    def _typed(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.wizard.model = event.value.strip()
            self.wizard.models_source = "typed"

    def can_continue(self) -> bool:
        table = self._find("#model-table", ModelTable)
        typed = self._find("#model-typed", Input)
        return bool(self.wizard.model or (table is not None and table.value) or (typed is not None and typed.value.strip()))

    def blocking_message(self) -> str:
        return "select a model from the table or type its id"

    def collect(self) -> None:
        typed_field = self._find("#model-typed", Input)
        table = self._find("#model-table", ModelTable)
        typed = typed_field.value.strip() if typed_field is not None else ""
        self.wizard.model = self.wizard.model or typed or (table.value if table is not None else "")


class ReasoningStep(WizardStep):
    def build(self) -> ComposeResult:
        yield from self.header(
            "Reasoning",
            "How hard the agent thinks, per surface. Models without native reasoning controls degrade gracefully — "
            "the depth still changes planning, verification and self-healing, it just cannot buy more thinking tokens.",
        )
        yield ReasoningLadder([MODES[mode] for mode in MODE_ORDER], selected=self.wizard.reasoning_mode, id="ladder")
        yield PanelCard(
            "per-surface depths",
            "One global default is wrong: a question needs medium, a refactor needs super. These are the defaults "
            "written to config — you can change them any time with ``ua reasoning``.",
            Static(
                "\n".join(
                    f"  [bold]{surface:<20}[/] {MODES[mode].label}"
                    for surface, mode in (
                        ("planning", "super"),
                        ("coding", "high"),
                        ("reasoning-intensive", "ultra" if "ultra" in MODES else "max"),
                        ("review", "high"),
                        ("quick answers", "medium"),
                    )
                ),
                id="surface-depths",
            ),
        )
        yield PanelCard(
            "planning depth",
            "How far the planner looks before it writes a line: how many approaches it compares and how detailed the "
            "milestones are.",
            OptionGrid(
                [
                    GridChoice("quick", "Quick", "One approach, minimal milestones. For small fixes.", badge="dim]fastest"),
                    GridChoice("standard", "Standard", "Three approaches, standard milestones."),
                    GridChoice("deep", "Deep", "Five approaches scored, milestones, risks, definition of done.",
                               badge="bold green]recommended", selected=self.wizard.planning_depth == "deep"),
                    GridChoice("exhaustive", "Exhaustive", "Everything in deep, plus alternatives and an adversarial critique.",
                               badge="yellow]slow"),
                ],
                id="planning-grid",
            ),
        )
        yield PanelCard(
            "streaming",
            "How the answer arrives. Live is the default; buffered keeps the screen calm on noisy models.",
            SwitchGrid(
                [
                    SwitchItem("streaming", "Stream responses", "Tokens appear as they are produced.", value=self.wizard.streaming),
                ],
                id="stream-flags",
            ),
            OptionGrid(
                [
                    GridChoice("live", "Live", "Stream straight to the transcript as it arrives.", selected=True),
                    GridChoice("buffered", "Buffered", "Stream, but render in complete lines."),
                    GridChoice("structured", "Structured", "Show tool calls and reasoning as structured blocks."),
                    GridChoice("silent", "Silent", "Only the final answer."),
                ],
                title="style",
                id="stream-grid",
            ),
        )

    def collect(self) -> None:
        ladder = self._find("#ladder", ReasoningLadder)
        planning = self._find("#planning-grid", OptionGrid)
        stream_flags = self._find("#stream-flags", SwitchGrid)
        stream_grid = self._find("#stream-grid", OptionGrid)
        if ladder is not None:
            self.wizard.reasoning_mode = ladder.value
        if planning is not None:
            self.wizard.planning_depth = planning.value
        if stream_flags is not None:
            self.wizard.streaming = bool(stream_flags.values().get("streaming", True))
        if stream_grid is not None:
            self.wizard.stream_style = stream_grid.value


def detect_browser() -> tuple[bool, str, str]:
    """Is a real browser available?  Returns ``(available, channel, reason)``."""

    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "chromium", "the 'browser' extra is not installed — pip install 'unknown-agent[browser]'"
    executable = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    driver = os.path.join(os.path.dirname(os.path.dirname(playwright.__file__)), "driver")  # type: ignore[attr-defined]
    if executable:
        return True, "chromium", f"system browser at {executable}"
    if os.path.isdir(driver):
        return True, "chromium", "Playwright is installed — browsers download on first use (ua browser install)"
    return False, "chromium", "Playwright is installed but no browser was found — run ua browser install"


class BrowserStep(WizardStep):
    def build(self) -> ComposeResult:
        yield from self.header(
            "Browser",
            "A browser is what turns 'I wrote the CSS' into 'I saw the page'. The validator role screenshots, clicks "
            "and reads the console, then reports what it actually observed.",
        )
        available, channel, reason = self.wizard.browser_available, self.wizard.browser_channel, self.wizard.browser_reason
        yield PanelCard(
            "status",
            Static(
                (f"[bold green]✓ available[/] — {reason}" if available else f"[yellow]! not ready[/] — {reason}"),
                id="browser-status",
            ),
            Static(
                "[dim]Browser validation is a gate, not a guess: with it, UI work ends in evidence. Without it, the "
                "browser-validator role is dropped from every plan and UI claims are marked unverified.[/]",
                id="browser-consequence",
            ),
        )
        yield SwitchGrid(
            [
                SwitchItem(
                    "browser",
                    "Enable browser validation",
                    "Adds the browser-validator role and the browser gate to UI work.",
                    value=self.wizard.browser_enabled and available,
                    risk="" if available else "unavailable",
                ),
                SwitchItem(
                    "headless",
                    "Run headless",
                    "No visible window. Turn off to watch the agent drive the browser.",
                    value=self.wizard.browser_headless,
                ),
            ],
            id="browser-flags",
        )
        yield OptionGrid(
            [
                GridChoice("chromium", "Chromium", "The default. Best tool-call fidelity with Playwright.", selected=True),
                GridChoice("firefox", "Firefox", "Useful when you need a second engine's rendering."),
                GridChoice("webkit", "WebKit", "Safari's engine — for web-specific checks."),
            ],
            title="engine",
            id="browser-engine",
        )
        if not available:
            yield PanelCard(
                "how to enable it later",
                Static(
                    "pip install 'unknown-agent[browser]'   then   ua browser install\n"
                    "[dim]Nothing else changes: the role appears in plans automatically once the browser answers.[/]",
                    id="browser-install-hint",
                ),
            )

    def collect(self) -> None:
        flags_widget = self._find("#browser-flags", SwitchGrid)
        engine = self._find("#browser-engine", OptionGrid)
        if flags_widget is None:
            return
        flags = flags_widget.values()
        self.wizard.browser_enabled = bool(flags.get("browser", False))
        self.wizard.browser_headless = bool(flags.get("headless", True))
        self.wizard.browser_channel = (engine.value if engine is not None else "chromium") or "chromium"


class SkillsStep(WizardStep):
    def build(self) -> ComposeResult:
        yield from self.header(
            "Skills",
            "A skill is a named bundle of instructions, tool grants and gates. Packs are how you turn a set of them on "
            "at once; you can add your own as TOML or YAML files later.",
        )
        payload = packs_payload()
        choices: list[GridChoice] = []
        for pack in payload:
            badge = "bold green]default" if pack.get("default") else ""
            implications = list(pack.get("skills", []))[:6]
            if len(pack.get("skills", [])) > 6:
                implications.append(f"+{len(pack['skills']) - 6} more")
            choices.append(
                GridChoice(
                    str(pack["key"]),
                    str(pack["label"]),
                    str(pack.get("description", "")),
                    badge=badge,
                    implies=implications,
                    selected=self.wizard.skills_pack == pack["key"],
                )
            )
        yield OptionGrid(choices, title="built-in pack", id="pack-grid")
        yield PanelCard(
            "your own skills",
            "Drop a file in ``~/.config/unknown-agent/skills/`` (yours, everywhere) or ``.unknown/skills/`` (this "
            "project only). Project skills are parsed but not executed as tools without your approval.",
            Static(SKILL_FILE_TEMPLATE.strip(), id="skill-template"),
        )

    def collect(self) -> None:
        grid = self._find("#pack-grid", OptionGrid)
        self.wizard.skills_pack = (grid.value if grid is not None else "") or "default"


class SafetyStep(WizardStep):
    def build(self) -> ComposeResult:
        yield from self.header(
            "Safety",
            "What the agent is allowed to do without asking. Every row states its risk; the defaults are the ones a "
            "careful engineer would pick on a machine they care about.",
        )
        yield SwitchGrid(
            [
                SwitchItem(
                    "network",
                    "Network access",
                    "Model calls always need it. This flag governs the agent's own fetches (docs, APIs, packages).",
                    value=self.wizard.allow_network,
                ),
                SwitchItem(
                    "destructive",
                    "Allow destructive commands",
                    "git reset --hard, rm -rf inside the workspace, force-push.",
                    value=self.wizard.allow_destructive,
                    risk="dangerous",
                ),
                SwitchItem(
                    "confirm-destructive",
                    "Confirm destructive commands",
                    "Ask before running anything irreversible. Ignored when destructive is off.",
                    value=self.wizard.confirm_destructive,
                ),
                SwitchItem(
                    "confirm-writes",
                    "Confirm every file write",
                    "Safe, and slow: the agent pauses on each edit.",
                    value=self.wizard.confirm_writes,
                    risk="slow",
                ),
                SwitchItem(
                    "outside",
                    "Allow writes outside the workspace",
                    "The agent stays inside the project directory unless you allow this.",
                    value=self.wizard.allow_outside_workspace,
                    risk="dangerous",
                ),
            ],
            id="safety-grid",
        )
        yield PanelCard(
            "always denied",
            "These paths are refused no matter what the flags say — credentials, VCS internals and dependency trees.",
            Static(
                "\n".join(f"  [dim]{pattern}[/]" for pattern in self.wizard.settings.policy.denied_paths[:8]),
                id="denied-list",
            ),
        )

    def collect(self) -> None:
        grid = self._find("#safety-grid", SwitchGrid)
        if grid is None:
            return
        flags = grid.values()
        self.wizard.allow_network = bool(flags.get("network", True))
        self.wizard.allow_destructive = bool(flags.get("destructive", False))
        self.wizard.confirm_destructive = bool(flags.get("confirm-destructive", True))
        self.wizard.confirm_writes = bool(flags.get("confirm-writes", False))
        self.wizard.allow_outside_workspace = bool(flags.get("outside", False))


class ProjectStep(WizardStep):
    def build(self) -> ComposeResult:
        yield from self.header(
            "First task",
            "Optional, and the fastest way to find out whether the setup works. Leave it empty to drop into the shell.",
        )
        cwd = os.getcwd()
        yield PanelCard(
            "working directory",
            Static(f"[bold]{cwd}[/]\n[dim]The agent reads and writes inside this directory.[/]", id="cwd-line"),
        )
        yield PanelCard(
            "what should it build first?",
            "Be concrete. 'Build me a site' gets you eleven questions; 'a FastAPI service that serves /healthz and "
            "stores tasks in SQLite' gets you a plan.",
            Input(value=self.wizard.first_task, placeholder="describe the first task, or leave empty", id="first-task"),
            Static("[dim]Enter to continue — this does not start anything yet.[/]", id="first-task-hint"),
        )

    @on(Input.Submitted, "#first-task")
    def _submitted(self, event: Input.Submitted) -> None:
        self.collect()
        self.app.action_continue()

    def collect(self) -> None:
        field = self._find("#first-task", Input)
        if field is not None:
            self.wizard.first_task = field.value.strip()


class ReviewStep(WizardStep):
    """Everything on one screen, then write."""

    def build(self) -> ComposeResult:
        yield from self.header(
            "Review",
            "This is exactly what will be written. Nothing is on disk yet.",
        )
        rows = "\n".join(f"  [bold]{label:<14}[/] {value}" for label, value in self.wizard.summary())
        yield PanelCard("settings", Static(rows, id="review-rows"))
        if self.wizard.errors:
            yield PanelCard(
                "unresolved problems",
                Static("\n".join(f"  [red]✕ {error}[/]" for error in self.wizard.errors), id="review-errors"),
                Static(
                    "[dim]None of these block setup — they are recorded so the repair flow knows where to start.[/]",
                    id="review-errors-note",
                ),
            )
        yield PanelCard(
            "files",
            Static(
                "  [bold]config.toml[/]      ~/.config/unknown-agent/config.toml   [dim](no secrets)[/]\n"
                "  [bold]credentials[/]      ~/.config/unknown-agent/credentials/    [dim](mode 0600)[/]\n"
                "  [bold]blueprint[/]        .unknown/blueprint.toml                 [dim](per project)[/]",
                id="review-files",
            ),
        )
        with Horizontal(id="review-actions"):
            yield Button("write it", id="write-config", variant="primary")
            yield Button("back", id="review-back")
        yield Static("", id="review-status")

    @on(Button.Pressed, "#review-back")
    def _back(self, event: Button.Pressed) -> None:
        self.app.action_back()

    @on(Button.Pressed, "#write-config")
    def _write(self, event: Button.Pressed) -> None:
        self.collect()
        self.app.action_finish()

    def collect(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #
class OnboardingApp(App[WizardState]):
    """The setup wizard."""

    TITLE = "Unknown Agent · setup"
    CSS = """
    Screen {
        layout: horizontal;
    }
    #wizard-body {
        width: 1fr;
        height: 1fr;
    }
    #wizard-nav {
        dock: bottom;
        height: 3;
        width: 100%;
        padding: 0 2;
        align: left middle;
        background: $surface;
        border-top: round $panel-lighten-2 60%;
    }
    #wizard-nav Button {
        margin-right: 1;
    }
    #nav-status {
        width: 1fr;
        height: 1;
        color: $text-muted;
        content-align: left middle;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        Binding("escape", "back", "Back", priority=False),
        Binding("f1", "continue_step", "Continue", priority=False),
        Binding("question_mark", "help", "Help", priority=False, show=False),
    ]

    def __init__(self, wizard: WizardState, *, runtime: Any = None) -> None:
        super().__init__()
        self.wizard = wizard
        self.runtime = runtime
        self.step_index = 0
        self._steps: dict[str, type[WizardStep]] = {
            "welcome": WelcomeStep,
            "theme": ThemeStep,
            "provider": ProviderStep,
            "endpoint": EndpointStep,
            "apikey": ApiKeyStep,
            "model": ModelStep,
            "reasoning": ReasoningStep,
            "browser": BrowserStep,
            "skills": SkillsStep,
            "safety": SafetyStep,
            "project": ProjectStep,
            "review": ReviewStep,
        }
        self.finished_with: str = ""

    # -- lifecycle --------------------------------------------------------- #
    def on_mount(self) -> None:
        apply_to_app(self, None if self.wizard.theme_key == "auto" else self.wizard.theme_key)
        self._prepare_environment()
        self._show_step(0)

    def _prepare_environment(self) -> None:
        available, channel, reason = detect_browser()
        self.wizard.browser_available = available
        self.wizard.browser_channel = channel
        self.wizard.browser_reason = reason
        if not available:
            self.wizard.browser_enabled = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield StepRail(list(STEP_LABELS), current=0, id="rail")
            with Vertical(id="wizard-body"):
                with Horizontal(id="wizard-nav"):
                    yield Button("← back", id="nav-back")
                    yield Button("continue →", id="nav-continue", variant="primary")
                    yield Static("", id="nav-status")
        yield Footer()

    # -- navigation -------------------------------------------------------- #
    @property
    def step_key(self) -> str:
        return STEP_KEYS[self.step_index]

    def _visible_steps(self) -> list[int]:
        """Indices of steps that apply to this run."""

        visible: list[int] = []
        for index, (key, _, required) in enumerate(STEP_ORDER):
            if self._step_applies(key):
                visible.append(index)
        return visible or list(range(len(STEP_ORDER)))

    def _step_applies(self, key: str) -> bool:
        if key == "endpoint":
            entry = self.wizard.entry
            return bool(entry and entry.requires_base_url) or self.wizard.custom_entry
        if key == "apikey":
            entry = self.wizard.entry
            return entry is None or entry.api_key_required
        if key == "project":
            return bool(self.wizard.first_task) or not self.wizard.repair
        return True

    def _show_step(self, index: int) -> Any:
        index = max(0, min(index, len(STEP_ORDER) - 1))
        self.step_index = index
        rail = self.query_one("#rail", StepRail)
        rail.set_current(index)
        for position, (key, _, _) in enumerate(STEP_ORDER):
            rail.mark(position, done=position < index, skipped=not self._step_applies(key))
        step_cls = self._steps[STEP_KEYS[index]]
        step = step_cls(self.wizard)
        body = self.query_one("#wizard-body", Vertical)
        nav = self.query_one("#wizard-nav", Horizontal)
        # One step on screen at a time: the outgoing step is removed only after
        # the incoming one is mounted, so focus never lands on nothing.
        previous = [widget for widget in body.children if isinstance(widget, WizardStep)]
        for widget in previous:
            widget.remove()
        # The nav gate depends on the step being queryable, and mounting is async:
        # refresh once the compositor has caught up, not before.
        self.call_after_refresh(self._update_nav)
        step.focus(scroll_visible=False)
        return body.mount(step, before=nav)

    def _update_nav(self) -> None:
        step = self._current_step()
        try:
            back = self.query_one("#nav-back", Button)
            continue_button = self.query_one("#nav-continue", Button)
            status = self.query_one("#nav-status", Static)
        except Exception:  # pragma: no cover - during remount
            return
        back.disabled = self.step_index == 0
        # A step that is still composing cannot answer the gate question yet; it
        # refreshes itself once mounted rather than being treated as "blocked".
        gateable = step is not None
        continue_button.disabled = gateable and not step.can_continue()
        label = STEP_LABELS[self.step_index]
        blocked = step.blocking_message() if gateable and not step.can_continue() else ""
        status.update(
            f"[bold]{label}[/]  ·  step {self.step_index + 1} of {len(STEP_ORDER)}"
            + (f"  [yellow]{blocked}[/]" if blocked else "")
            + ("  [dim](last step)[/]" if self.step_index == len(STEP_ORDER) - 1 else "")
        )

    def _current_step(self) -> WizardStep | None:
        try:
            return self.query_one(WizardStep)
        except Exception:  # pragma: no cover - during remount
            return None

    def action_refresh(self) -> None:
        """Re-evaluate the continue gate (bound to any change inside a step)."""

        self._update_nav()

    def action_back(self) -> None:
        if self.step_index == 0:
            self.exit(self.wizard)
            return
        step = self._current_step()
        if step is not None:
            step.collect()
            step.on_leave()
        visible = self._visible_steps()
        position = visible.index(self.step_index) if self.step_index in visible else self.step_index
        target = visible[max(0, position - 1)] if position > 0 else max(0, self.step_index - 1)
        self._show_step(target)

    def action_continue(self) -> None:
        step = self._current_step()
        if step is None:
            return
        if not step.can_continue():
            self.notify(step.blocking_message() or "this step needs one more answer", severity="warning")
            self._update_nav()
            return
        if not step.on_continue():
            self._update_nav()
            return
        step.on_leave()
        visible = self._visible_steps()
        position = visible.index(self.step_index) if self.step_index in visible else -1
        if position >= 0 and position + 1 < len(visible):
            self._show_step(visible[position + 1])
        elif self.step_index + 1 < len(STEP_ORDER):
            self._show_step(self.step_index + 1)
        else:
            self.action_finish()

    action_continue_step = action_continue

    def action_finish(self) -> None:
        step = self._current_step()
        if step is not None:
            step.collect()
        result = self.apply()
        self.finished_with = result
        self.exit(self.wizard)

    def action_help(self) -> None:
        self.notify(
            "Enter/F1 continue · Escape back · ↑↓ move · Ctrl+C quit without writing anything",
            title="keys",
            timeout=8,
        )

    @on(StepRail.Jumped, "#rail")
    def _jumped(self, event: StepRail.Jumped) -> None:
        # Jumping forward past unanswered steps would let you write a half-config,
        # so forward jumps stop at the first incomplete step.
        step = self._current_step()
        if step is not None:
            step.collect()
        # A forward jump is allowed only to a step whose predecessors are all
        # answerable; otherwise the rail becomes a way to skip the API key.
        target = min(event.index, len(STEP_ORDER) - 1)
        for index in range(self.step_index, target):
            key = STEP_KEYS[index]
            if not self._step_applies(key):
                continue
            probe = self._steps[key](self.wizard)
            if not _static_gate(probe):
                target = index
                self.notify(f"{STEP_LABELS[index]} still needs an answer", severity="warning")
                break
        self._show_step(target)

    @on(Button.Pressed, "#nav-back")
    def _nav_back(self, event: Button.Pressed) -> None:
        self.action_back()

    @on(Button.Pressed, "#nav-continue")
    def _nav_continue(self, event: Button.Pressed) -> None:
        self.action_continue()

    @on(OptionGrid.Chosen)
    def _any_choice(self, event: OptionGrid.Chosen) -> None:
        # Choosing an option is a decision: refresh the gate state so a newly
        # answerable step can be continued immediately.
        step = self._current_step()
        if step is not None:
            step.collect()
        self._update_nav()

    @on(Input.Changed)
    def _any_input(self, event: Input.Changed) -> None:
        self._update_nav()

    @on(Switch.Changed)
    def _any_switch(self, event: Switch.Changed) -> None:
        step = self._current_step()
        if step is not None:
            step.collect()
        self._update_nav()

    # -- persistence ------------------------------------------------------- #
    def apply(self) -> str:
        """Write config and credentials.  Returns a human-readable result line."""

        wizard = self.wizard
        settings = wizard.settings
        entry = wizard.entry
        if entry is None:
            wizard.fail("no provider selected")
            return "nothing written: no provider selected"

        from unknown_agent.config.models import ProviderConfig

        profile_name = wizard.profile_name or profile_name_for(entry, settings.providers or {})
        if wizard.custom_entry:
            # A custom endpoint is a real profile, not a special case: it uses the
            # openai-compatible dialect with the flags the user declared, and goes
            # through the same adapter registry, credential store and discovery.
            profile_values = {
                "type": "openai-compatible",
                "model": wizard.model,
                "base_url": wizard.base_url,
                "supports_tools": entry.supports_tools,
                "supports_streaming": entry.supports_streaming,
                "json_mode": entry.supports_json_mode,
                "supports_reasoning": entry.supports_reasoning,
                "supports_vision": entry.supports_vision,
            }
            if entry.extra_headers:
                profile_values["headers"] = dict(entry.extra_headers)
        else:
            profile_values = dict(getattr(entry, "profile_defaults", {}) or {})
            profile_values["model"] = wizard.model or profile_values.get("model") or ""
            if wizard.base_url:
                profile_values["base_url"] = wizard.base_url
        info = next((item for item in wizard.models if item.id == wizard.model), None)
        if info is not None:
            if info.context_window:
                profile_values["context_window"] = info.context_window
            if info.max_output_tokens:
                profile_values["max_output_tokens"] = info.max_output_tokens
            if info.supports_tools is not None:
                profile_values["supports_tools"] = info.supports_tools
            if info.input_cost_per_mtok is not None:
                profile_values["input_cost_per_mtok"] = info.input_cost_per_mtok
            if info.output_cost_per_mtok is not None:
                profile_values["output_cost_per_mtok"] = info.output_cost_per_mtok
            profile_values["supports_reasoning"] = info.supports_reasoning
            profile_values["supports_vision"] = info.supports_vision

        # credentials ------------------------------------------------------------
        store = CredentialStore()
        credential_ref = wizard.credential_ref
        if wizard.api_key and wizard.key_source == "pasted":
            secret_name = _safe_profile_name(profile_name or entry.key)
            try:
                store.set(secret_name, wizard.api_key)
                credential_ref = f"store:{secret_name}"
                wizard.note(f"secret stored as {credential_ref} (0600)")
            except Exception as exc:  # pragma: no cover - filesystem dependent
                wizard.fail(f"could not write the credential store: {exc}")
        if credential_ref:
            profile_values["api_key"] = credential_ref

        try:
            profile = ProviderConfig(**profile_values)
        except Exception as exc:
            wizard.fail(f"the provider profile was rejected by the schema: {exc}")
            return f"nothing written: {exc}"

        providers = dict(settings.providers or {})
        providers[profile_name] = profile
        settings.providers = providers
        # ``settings.provider`` names the active profile; the placeholder profile
        # injected before setup is dropped so the config does not carry a dead entry.
        placeholder = providers.get("default")
        if placeholder is not None and placeholder.is_placeholder and profile_name != "default":
            providers.pop("default", None)
            settings.providers = providers
        if settings.provider not in providers:
            settings.provider = profile_name

        # appearance / reasoning / streaming ------------------------------------
        settings.appearance.theme = wizard.theme_key  # type: ignore[assignment]
        settings.appearance.glyphs = wizard.glyphs  # type: ignore[assignment]
        settings.appearance.motion = wizard.motion  # type: ignore[assignment]
        settings.reasoning.default_mode = wizard.reasoning_mode  # type: ignore[assignment]
        settings.streaming.enabled = wizard.streaming
        settings.streaming.style = wizard.stream_style  # type: ignore[assignment]
        settings.planning.depth = wizard.planning_depth  # type: ignore[assignment]
        # ``[skills].pack`` is a list so several packs can be combined; the wizard
        # picks one at a time, which is the common case.
        settings.skills.pack = [wizard.skills_pack] if wizard.skills_pack else []
        settings.policy.allow_network = wizard.allow_network
        settings.policy.allow_destructive = wizard.allow_destructive
        settings.policy.confirm_destructive = wizard.confirm_destructive
        settings.policy.confirm_writes = wizard.confirm_writes
        settings.policy.allow_outside_workspace = wizard.allow_outside_workspace
        settings.browser.enabled = wizard.browser_enabled
        settings.browser.headless = wizard.browser_headless
        settings.browser.browser = wizard.browser_channel  # type: ignore[assignment]

        # write ------------------------------------------------------------------
        try:
            from unknown_agent.config.loader import save_settings

            path = save_settings(settings)
        except ImportError:  # pragma: no cover - older loader
            path = _save_settings_fallback(settings)
        except Exception as exc:
            wizard.fail(f"could not write config: {exc}")
            return f"nothing written: {exc}"
        return f"written: {path} · provider {profile_name} · model {wizard.model or '—'}"


def _save_settings_fallback(settings: Settings) -> str:
    """Minimal TOML writer for the case the loader cannot round-trip."""

    from pathlib import Path

    import tomli_w

    path = Path(os.environ.get("UNKNOWN_AGENT_CONFIG", "") or Path.home() / ".config" / "unknown-agent" / "config.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json", exclude_none=False)
    with path.open("wb") as handle:
        tomli_w.dump(payload, handle)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    return str(path)


def _static_gate(step: WizardStep) -> bool:
    """Whether a step can be continued using only state already collected.

    Used by the rail so a forward jump cannot skip a step that still needs an
    answer (an API key, a model).  It deliberately ignores widget-level gates,
    which only exist once the step is mounted.
    """

    wizard = step.wizard
    key = step.__class__.__name__
    if key == "ProviderStep":
        return wizard.entry is not None
    if key == "EndpointStep":
        return bool(wizard.base_url)
    if key == "ApiKeyStep":
        entry = wizard.entry
        return bool(entry and not entry.api_key_required) or bool(wizard.api_key or wizard.credential_ref)
    if key == "ModelStep":
        return bool(wizard.model)
    return True


class _Blank:
    """Stand-in for a widget that is not mounted yet."""

    value = ""


class _BlankSwitches:
    @staticmethod
    def values() -> dict[str, bool]:
        return {}


def _safe_profile_name(value: str) -> str:
    """A profile name usable as a TOML table key and a credential filename."""

    cleaned = "".join(char if (char.isalnum() or char in "-_") else "-" for char in (value or "").strip().lower())
    cleaned = cleaned.strip("-") or "custom"
    return cleaned[:48]


def _fingerprint(value: str) -> str:
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}{'•' * 8}{value[-4:]}"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_wizard(runtime: Any = None, *, settings: Settings | None = None, repair: bool = False) -> WizardState:
    """Seed the wizard from the current runtime state."""

    resolved = settings or (runtime.load(force=True).settings if runtime is not None else Settings())
    state = detect_setup_state(resolved) if runtime is not None else None
    wizard = WizardState(settings=resolved, state=state, repair=repair or (state is not None and state.status != NEEDS_SETUP))
    wizard.theme_key = resolved.appearance.theme or "auto"
    wizard.glyphs = resolved.appearance.glyphs or "unicode"
    wizard.motion = resolved.appearance.motion or "full"
    wizard.reasoning_mode = resolved.reasoning.default_mode or "high"
    wizard.planning_depth = resolved.planning.depth or "deep"
    wizard.streaming = bool(resolved.streaming.enabled)
    wizard.stream_style = resolved.streaming.style or "live"
    wizard.skills_pack = (resolved.skills.pack or ["default"])[0] if isinstance(resolved.skills.pack, list) else (resolved.skills.pack or "default")
    wizard.allow_network = bool(resolved.policy.allow_network)
    wizard.allow_destructive = bool(resolved.policy.allow_destructive)
    wizard.confirm_destructive = bool(resolved.policy.confirm_destructive)
    wizard.confirm_writes = bool(resolved.policy.confirm_writes)
    wizard.allow_outside_workspace = bool(resolved.policy.allow_outside_workspace)
    wizard.browser_enabled = bool(getattr(resolved.browser, "enabled", False))
    wizard.browser_headless = bool(getattr(resolved.browser, "headless", True))
    wizard.browser_channel = getattr(resolved.browser, "browser", "chromium") or "chromium"
    if resolved.provider and resolved.providers:
        profile = resolved.providers.get(resolved.provider)
        if profile is not None and not profile.is_placeholder:
            entry = resolve_entry(profile.type)
            if entry is not None:
                wizard.entry = entry
                wizard.profile_name = resolved.provider
                wizard.model = profile.model
                wizard.base_url = profile.base_url or ""
                wizard.credential_ref = profile.api_key or ""
    return wizard


async def run_onboarding_tui(runtime: Any = None, *, repair: bool = False, headless: bool = False) -> WizardState:
    """Run the wizard and return its state (with settings written on success)."""

    wizard = build_wizard(runtime, repair=repair)
    app = OnboardingApp(wizard, runtime=runtime)
    if headless:
        async with app.run_test(size=(128, 40)) as pilot:
            await pilot.pause()
        return wizard
    await app.run_async()
    return app.wizard
