"""The Textual interface: the wizard and the workspace.

Two applications share one widget set and one theme source:

:mod:`~unknown_agent.tui.themes`
    Every palette in :mod:`unknown_agent.ui.theme_defs` becomes a Textual
    ``Theme``, so ``ua theme use paper`` recolours the CLI and the TUI together.
    The mapping pins the anchors Textual derives everything else from, plus the
    product tokens (``$ua-muted``, ``$ua-border-dim`` …) as CSS variables.

:mod:`~unknown_agent.tui.widgets`
    The premium widget set — step rail, masked API-key capture with an offline
    shape check, capability badges, an option grid that shows the consequences of
    the highlighted choice, the planning decision panel, the model table, palette
    swatches, permission switches with risk labels, the reasoning ladder and the
    live orchestration board.  Design rule: never a bare control — every widget
    carries its own explanation.

:mod:`~unknown_agent.tui.quicksetup`
    The default setup experience: a provider selector, a modal API-key dialog that
    validates and fetches the model catalog on Enter, a model selector and a save
    step.  Four panels, no commands.

:mod:`~unknown_agent.tui.onboarding`
    The twelve-step first-run wizard.  Live re-theming, per-step gating, network
    only where it earns its place (validate, list models) and an offline fallback
    everywhere, nothing written to disk until the review step.

:mod:`~unknown_agent.tui.app`
    The persistent shell: transcript, composer, status rail and a side panel with
    plan / workers / gates / tools.  It drives the core loop through
    :class:`~unknown_agent.core.loop.LoopHooks` and an injected runner callable,
    so the same screen serves the real agent, an orchestrated run, or a stub in
    tests.

A note on CSS: widget ``DEFAULT_CSS`` may only use Textual's built-in variables,
because it is parsed at class-definition time — before any app has registered our
palettes.  The ``$ua-*`` tokens are used in app-level CSS, which is parsed after.
"""

from __future__ import annotations

from unknown_agent.tui.app import AgentShell, ComposerMessage, ShellBridge, StatusSnapshot, run_shell
from unknown_agent.tui.onboarding import STEP_LABELS, STEP_ORDER, OnboardingApp, WizardState, build_wizard, run_onboarding_tui
from unknown_agent.tui.quicksetup import (
    ApiKeyScreen,
    ModelScreen,
    ProviderScreen,
    QuickSetupResult,
    ReviewScreen,
    SetupApp,
    run_setup_panel,
    save_provider_choice,
)
from unknown_agent.tui.themes import apply_to_app, available_theme_names, register_all, theme_name, to_textual_theme
from unknown_agent.tui.widgets import (
    ApiKeyInput,
    CapabilityBadges,
    DecisionPanel,
    GridChoice,
    KeyHints,
    ModelTable,
    OptionGrid,
    OrchestrationBoard,
    PanelCard,
    ReasoningLadder,
    StepRail,
    SwatchRow,
    SwitchGrid,
    SwitchItem,
    rank_models,
)

__all__ = [
    # app
    "AgentShell",
    "ComposerMessage",
    "ShellBridge",
    "StatusSnapshot",
    "run_shell",
    # onboarding
    "OnboardingApp",
    "STEP_LABELS",
    "STEP_ORDER",
    "WizardState",
    "build_wizard",
    "run_onboarding_tui",
    # guided setup panel
    "ApiKeyScreen",
    "ModelScreen",
    "ProviderScreen",
    "QuickSetupResult",
    "ReviewScreen",
    "SetupApp",
    "run_setup_panel",
    "save_provider_choice",
    # themes
    "apply_to_app",
    "available_theme_names",
    "register_all",
    "theme_name",
    "to_textual_theme",
    # widgets
    "ApiKeyInput",
    "CapabilityBadges",
    "DecisionPanel",
    "GridChoice",
    "KeyHints",
    "ModelTable",
    "OptionGrid",
    "OrchestrationBoard",
    "PanelCard",
    "ReasoningLadder",
    "StepRail",
    "SwatchRow",
    "SwitchGrid",
    "SwitchItem",
    "rank_models",
]
