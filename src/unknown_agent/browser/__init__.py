"""Browser automation subsystem (Playwright + Chromium by default).

Public surface:

* :class:`BrowserSessionManager` - lifecycle and app bootstrapping,
* :class:`BrowserSession` - a live, instrumented page and its flow runner,
* :class:`FlowReport` / :class:`FlowStepResult` - structured evidence,
* :func:`load_flow` / :func:`discover_flows` - repeatable verification journeys.

Importing this package never requires Playwright; only *using* it does, and the
error says exactly what to install.
"""

from unknown_agent.browser.flows import Flow, discover_flows, load_flow, smoke_steps, write_flow
from unknown_agent.browser.manager import (
    BROWSER_INSTALL_HINT,
    BrowserSessionManager,
    browsers_installed,
    install_browsers,
    playwright_available,
)
from unknown_agent.browser.session import BrowserSession, FlowReport, FlowStepResult

__all__ = [
    "BrowserSessionManager",
    "BrowserSession",
    "FlowReport",
    "FlowStepResult",
    "Flow",
    "load_flow",
    "discover_flows",
    "write_flow",
    "smoke_steps",
    "playwright_available",
    "browsers_installed",
    "install_browsers",
    "BROWSER_INSTALL_HINT",
]
