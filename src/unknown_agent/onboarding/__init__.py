"""Onboarding: the first-run experience.

Why this is a package rather than a command function: onboarding is a *state
machine* with retries, recovery paths, persistence and a real UI.  Splitting it
keeps the CLI thin and makes each piece testable:

* :mod:`unknown_agent.onboarding.detect` - is setup needed, and why?
* :mod:`unknown_agent.onboarding.screens` - every visual element (Rich only, no logic)
* :mod:`unknown_agent.onboarding.flow`   - the steps, retries and persistence
* :mod:`unknown_agent.onboarding.panel`  - the single entry point: interactive
  panel when a terminal allows it, guided prompts otherwise
"""

from __future__ import annotations

from unknown_agent.onboarding.detect import SetupState, detect_setup_state
from unknown_agent.onboarding.flow import OnboardingResult, run_onboarding
from unknown_agent.onboarding.panel import SetupOutcome, launch_setup, panel_available

__all__ = [
    "OnboardingResult",
    "SetupOutcome",
    "SetupState",
    "detect_setup_state",
    "launch_setup",
    "panel_available",
    "run_onboarding",
]
