"""Presentation layer (Rich): consoles, theme, renderers, prompts, errors."""

from unknown_agent.ui.console import OutputManager, configure_output, get_console, get_diag_console, output
from unknown_agent.ui.errors import render_error, render_warnings
from unknown_agent.ui.prompts import Prompter, confirm_destructive
from unknown_agent.ui.theme import UNKNOWN_AGENT_THEME, glyph

__all__ = [
    "output",
    "OutputManager",
    "configure_output",
    "get_console",
    "get_diag_console",
    "render_error",
    "render_warnings",
    "Prompter",
    "confirm_destructive",
    "UNKNOWN_AGENT_THEME",
    "glyph",
]
