"""Interactive prompts with a correct non-interactive story.

* destructive actions always confirm unless ``--yes`` is passed;
* confirmation is skipped when piped or when ``--json`` is active, in which case
  the *documented* default applies and is logged;
* ``--non-interactive`` makes every prompt resolve to its default so CI never hangs.
"""

from __future__ import annotations

import sys
from typing import Any

from rich.prompt import Confirm, Prompt
from rich.text import Text

from unknown_agent.errors import UsageError
from unknown_agent.ui.console import output


class Prompter:
    def __init__(self, *, assume_yes: bool = False, non_interactive: bool = False) -> None:
        self.assume_yes = assume_yes
        self.non_interactive = non_interactive or not sys.stdin.isatty()

    def confirm(
        self,
        question: str,
        *,
        preview: str | None = None,
        default: bool = False,
        dangerous: bool = False,
    ) -> bool:
        """Ask a yes/no question, honouring ``--yes`` and non-interactive sessions.

        ``preview`` shows exactly what is about to happen (the concrete command or
        file operation) so a "yes" is an informed one.
        """

        if self.assume_yes:
            output.diag_print(Text(f"--yes: approving {question}", style="ua.muted"))
            return True
        if self.non_interactive or not output.interactive:
            if dangerous and not default:
                output.diag_print(
                    Text(
                        f"non-interactive: refusing {question!r} (default: no). Pass --yes to allow.",
                        style="ua.warn",
                    )
                )
            return default
        if preview:
            output.diag_print(Text(preview, style="ua.muted"))
        return Confirm.ask(
            Text(question, style="ua.err" if dangerous else "ua.warn"),
            default=default,
            console=output.data,
        )

    def text(self, question: str, *, default: str | None = None, secret: bool = False, allow_empty: bool = False) -> str:
        if self.non_interactive or not output.interactive:
            if default is not None:
                return default
            if allow_empty:
                return ""
            raise UsageError(f"Missing required input: {question}", hint="Pass the value as a flag for non-interactive use.")
        return Prompt.ask(Text(question, style="ua.info"), default=default or "", password=secret, console=output.data, show_default=default is not None)

    def choose(self, question: str, options: list[tuple[str, str]], *, default: str | None = None) -> str:
        if self.non_interactive or not output.interactive:
            if default is None:
                raise UsageError(f"Missing required choice: {question}", hint="Pass the value as a flag for non-interactive use.")
            return default
        listing = Text()
        for key, description in options:
            listing.append(f"  {key}", style="ua.brand")
            listing.append(f"  {description}\n", style="ua.muted")
        output.print(listing)
        return self.text(question, default=default)


def confirm_destructive(prompter: Prompter, what: str, *, preview: str | None = None) -> bool:
    output.diag_print(Text(f"This will {what}.", style="ua.warn"))
    if preview:
        output.diag_print(Text(preview, style="ua.muted"))
    return prompter.confirm("Proceed?", default=False, dangerous=True)


def require_threshold(value: Any, name: str, kind: type = float, minimum: float = 0) -> Any:
    try:
        parsed = kind(value)
    except (TypeError, ValueError) as exc:
        raise UsageError(f"{name} must be a {kind.__name__}, got {value!r}.") from exc
    if parsed < minimum:
        raise UsageError(f"{name} must be >= {minimum}, got {parsed}.")
    return parsed
