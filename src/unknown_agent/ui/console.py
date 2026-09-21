"""Console factory and global output policy.

Guarantees the product depends on:

* **stdout is data, stderr is diagnostics.**  Payloads go to the data console;
  logs, spinners and progress go to the diagnostics console.  That is what makes
  ``ua inspect --json > repo.json`` safe.
* **Colour respects the environment** - ``NO_COLOR``, ``FORCE_COLOR``,
  ``CLICOLOR_FORCE``, ``TERM=dumb``, non-TTY pipes.
* **One width contract** - rendered against the real terminal width, capped at
  120 columns so code stays readable on ultrawide terminals.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator, TextIO

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from unknown_agent.ui.theme import UNKNOWN_AGENT_THEME

MAX_WIDTH = 120


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def detect_color(stream: TextIO) -> bool | None:
    """Return True/False to force colour, or None to let Rich decide."""

    if os.environ.get("NO_COLOR"):
        return False
    if _env_truthy("FORCE_COLOR") or _env_truthy("CLICOLOR_FORCE"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return None


def target_width(stream: TextIO | None = None) -> int:
    try:
        columns = os.get_terminal_size().columns
    except OSError:
        columns = 100
    return max(60, min(columns, MAX_WIDTH))


def make_console(
    *,
    stream: TextIO | None = None,
    stderr: bool = False,
    theme: Theme | None = None,
    force_color: bool | None = None,
    width: int | None = None,
    quiet: bool = False,
) -> Console:
    target = stream or (sys.stderr if stderr else sys.stdout)
    color = detect_color(target) if force_color is None else force_color
    return Console(
        file=target,
        theme=theme or UNKNOWN_AGENT_THEME,
        color_system="truecolor" if color else None,
        force_terminal=bool(color),
        no_color=color is False,
        width=width or target_width(target),
        soft_wrap=False,
        highlight=False,
        emoji=False,
        log_path=False,
        quiet=quiet,
        markup=True,
    )


class OutputManager:
    """Owns both consoles plus the human/machine output mode."""

    def __init__(self) -> None:
        self.mode: str = "human"
        self.quiet: bool = False
        self.verbose: int = 0
        self.unicode: bool = True
        self.animations: bool = True
        self.show_timings: bool = True
        self._data: Console | None = None
        self._diag: Console | None = None
        self._bound_streams: tuple[Any, Any] | None = None

    def configure(
        self,
        *,
        mode: str = "human",
        quiet: bool = False,
        verbose: int = 0,
        unicode_ok: bool = True,
        animations: bool = True,
        show_timings: bool = True,
        force_color: bool | None = None,
        width: int | None = None,
    ) -> OutputManager:
        self.mode = mode
        self.quiet = quiet
        self.verbose = verbose
        self.unicode = unicode_ok
        self.animations = animations
        self.show_timings = show_timings
        # The data console is deliberately never created with Rich's own ``quiet``
        # flag: Rich's quiet suppresses even forced output, and ``--json`` payloads
        # must always reach stdout. Suppression is handled here instead, where an
        # explicit ``force=True`` (structured output) can override it.
        self._data = make_console(force_color=force_color, width=width, quiet=False)
        self._diag = make_console(stderr=True, force_color=force_color, width=width, quiet=False)
        self._bound_streams = (sys.stdout, sys.stderr)
        return self

    def _sync_streams(self) -> None:
        """Rebuild the consoles when ``sys.stdout``/``sys.stderr`` have changed.

        Console objects capture the stream they were built with.  Anything that
        replaces the process streams - a test runner, a notebook, an embedding
        application - would otherwise leave this module writing into a closed file
        ("I/O operation on closed file").  Rebuilding is cheap and keeps output
        correct no matter who owns the streams.
        """

        if self._data is None or self._diag is None:
            return
        if self._bound_streams == (sys.stdout, sys.stderr):
            return
        width = getattr(self._data, "width", None)
        self.configure(
            mode=self.mode,
            quiet=self.quiet,
            verbose=self.verbose,
            unicode_ok=self.unicode,
            animations=self.animations,
            show_timings=self.show_timings,
            width=width,
        )

    @property
    def data(self) -> Console:
        if self._data is None:
            self.configure()
        else:
            self._sync_streams()
        assert self._data is not None
        return self._data

    @property
    def diag(self) -> Console:
        if self._diag is None:
            self.configure()
        else:
            self._sync_streams()
        assert self._diag is not None
        return self._diag

    @property
    def is_machine(self) -> bool:
        return self.mode in {"json", "ndjson"}

    @property
    def interactive(self) -> bool:
        return bool(self.data.is_terminal) and not self.is_machine and not self.quiet

    # -- primitives --------------------------------------------------------
    def print(self, *objects: Any, **kwargs: Any) -> None:
        force = bool(kwargs.pop("force", False))
        if self.quiet and not force:
            return
        self.data.print(*objects, **kwargs)

    def diag_print(self, *objects: Any, **kwargs: Any) -> None:
        self.diag.print(*objects, **kwargs)

    def raw(self, text: str, *, stream: Any = None) -> None:
        """Write pre-formatted text (JSON, ndjson) straight to the stream.

        Machine output must never pass through Rich's renderer: wrapping would
        insert newlines inside JSON strings and break every consumer.
        """

        target = stream or sys.stdout
        if not text.endswith("\n"):
            text += "\n"
        target.write(text)
        target.flush()

    def rule(self, title: str = "", **kwargs: Any) -> None:
        if self.quiet:
            return
        self.data.rule(f"[ua.muted]{title}[/ua.muted]" if title else "", **kwargs)

    def blank(self) -> None:
        if not self.quiet:
            self.data.print()

    @contextmanager
    def status(self, message: str, *, spinner: str = "dots", transient: bool = True) -> Iterator[Any]:
        """Live spinner that is a no-op in quiet / machine / non-TTY mode."""

        if not self.animations or self.quiet or self.is_machine or not self.diag.is_terminal:
            yield None
            return
        with self.diag.status(f"[ua.muted]{message}[/ua.muted]", spinner=spinner, transient=transient) as handle:
            yield handle

    def error_panel(self, title: str, body: Any, *, hint: str | None = None) -> None:
        """Render a failure with an actionable hint.

        The hint is inserted as plain :class:`~rich.text.Text`, never as markup:
        hints routinely contain bracketed configuration paths such as
        ``[project.commands]`` which Rich would otherwise swallow as a style tag.
        """

        parts: list[Any] = [body]
        if hint:
            prefix = Text("Try: ", style="ua.muted")
            prefix.append_text(Text(hint, style="ua.info"))
            parts.extend(["", prefix])
        self.diag_print(Panel(Group(*parts), title=f"[ua.err]{title}[/ua.err]", border_style="ua.error.border", padding=(1, 2)))

    def success_panel(self, title: str, body: Any) -> None:
        self.print(Panel(body, title=f"[ua.ok]{title}[/ua.ok]", border_style="ua.success.border", padding=(1, 2)))


#: Process-wide manager, created lazily so import order never matters.
output = OutputManager()


def get_console() -> Console:
    return output.data


def get_diag_console() -> Console:
    return output.diag


def configure_output(**kwargs: Any) -> OutputManager:
    return output.configure(**kwargs)
