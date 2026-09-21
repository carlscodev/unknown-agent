"""Computer use: adaptive UI interaction, distinct from scripted browser flows.

Browser automation (``unknown_agent.browser``) replays deterministic flows and
asserts on the result - it is a *verification* instrument.  Computer use is an
*interaction loop*: observe (screenshot + UI state) -> decide (model) -> act
(click/type/scroll/key) -> observe again.  The two share the Playwright
runtime but nothing else, on purpose.

Layers:

``actions``  the closed vocabulary of actions and observations (typed, auditable)
``driver``   the Driver protocol + the Playwright-backed BrowserDriver;
             native OS drivers plug in here later without touching callers
``session``  budgets, confirmation policy, host allowlist, the action journal
``tools``    computer_observe / computer_act - the agent-facing pair

Safety model: off by default ([computer_use] enabled = false), bounded per
task (actions + wall clock), confirmation for mutating actions by default,
denied-keys list, host allowlist, and every action journalled with its
before/after screenshots.
"""

from unknown_agent.computer_use.actions import Action, ActionResult, Observation
from unknown_agent.computer_use.session import ComputerUseSession, ComputerUseError

__all__ = ["Action", "ActionResult", "Observation", "ComputerUseSession", "ComputerUseError"]
