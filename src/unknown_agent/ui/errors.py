"""Error rendering with actionable hints.

A failure message must answer three questions: what failed, why, and what to do
next.  ``UnknownAgentError.hint`` carries the third part; this module guarantees
the format is identical everywhere and that exit codes stay stable.
"""

from __future__ import annotations

import json
import traceback

from rich.console import Group
from rich.text import Text

from unknown_agent.errors import UnknownAgentError
from unknown_agent.ui.console import output

INTERNAL_ERROR_EXIT = 70


def render_error(error: BaseException, *, debug: bool = False, context: str | None = None) -> int:
    """Print ``error`` and return the process exit code."""

    if isinstance(error, UnknownAgentError):
        code = int(error.exit_code)
        if output.is_machine:
            output.raw(json.dumps(error.to_dict(), ensure_ascii=False, indent=2))
            return code
        parts: list[object] = [Text(error.message, style="ua.obs")]
        if context:
            parts.extend(["", Text(context, style="ua.muted")])
        details = None
        if error.context:
            details = error.context.get("problems") or error.context.get("detail")
        if details:
            parts.extend(["", Text(_format_details(details), style="ua.muted")])
        output.error_panel(error.code.replace("_", " ").upper(), Group(*parts), hint=error.hint)
        return code

    if output.is_machine:
        output.raw(json.dumps({"error": "internal_error", "message": str(error), "exit_code": INTERNAL_ERROR_EXIT}, indent=2))
        return INTERNAL_ERROR_EXIT
    detail: object = Text(str(error), style="ua.obs")
    if debug:
        detail = Text("".join(traceback.format_exception(type(error), error, error.__traceback__)), style="ua.dim")
    output.error_panel(
        "INTERNAL ERROR",
        detail,
        hint="Re-run with --debug and report it at https://github.com/unknown-agent/unknown-agent/issues",
    )
    return INTERNAL_ERROR_EXIT


def _format_details(details: object) -> str:
    if isinstance(details, (list, tuple)):
        return "\n".join(f"  - {item}" for item in details)
    return str(details)


def render_warnings(warnings: list[str], *, max_shown: int = 5) -> None:
    if not warnings or output.quiet:
        return
    text = Text()
    for warning in warnings[:max_shown]:
        text.append("  ! ", style="ua.warn")
        text.append(f"{warning}\n", style="ua.obs")
    if len(warnings) > max_shown:
        text.append(f"  ... and {len(warnings) - max_shown} more\n", style="ua.muted")
    output.diag_print(text)
