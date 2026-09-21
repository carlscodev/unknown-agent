"""CLI runtime: global options, settings assembly, run orchestration.

Every command receives a :class:`Runtime` through ``ctx.obj``.  It owns the
cross-cutting concerns so command functions stay short and testable:

* loading and caching :class:`~unknown_agent.config.loader.LoadedSettings` with
  CLI overrides recorded as provenance (``ua config show --explain``),
* configuring logging (file + console) once per process,
* creating a :class:`~unknown_agent.core.runner.Runner` wired to CLI-friendly
  hooks (live event rendering, plan approval, Ctrl-C),
* translating run outcomes into exit codes and structured output.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.cli.render import RunRenderer
from unknown_agent.config.loader import LoadedSettings, load_settings
from unknown_agent.config.paths import AppPaths
from unknown_agent.errors import ConfigError, ExitCode
from unknown_agent.observability.logging import setup_logging
from unknown_agent.ui.console import output
from unknown_agent.ui.prompts import Prompter

#: CLI flag -> dotted config path, used for provenance reporting.
FLAG_TO_SETTING = {
    "mode": "agent.mode",
    "model": "providers.default.model",
    "max_steps": "agent.max_steps",
    "definition_of_done": "agent.definition_of_done",
    "read_only": "policy.read_only",
    "dry_run": "policy.dry_run",
    "browser_url": "browser.base_url",
}


@dataclass
class Runtime:
    config_file: Path | None = None
    cwd: Path = field(default_factory=Path.cwd)
    project: Path | None = None
    mode: str | None = None
    model: str | None = None
    provider: str | None = None
    max_steps: int | None = None
    definition_of_done: str | None = None
    read_only: bool = False
    dry_run: bool = False
    assume_yes: bool = False
    non_interactive: bool = False
    debug: bool = False
    verbosity: int = 0
    #: Appearance flags.  ``None`` means "the config file decides": an explicit
    #: ``--no-color`` / ``--ascii`` / ``--no-animations`` on the command line is a
    #: more recent and more specific statement of intent than ``[appearance]``, so
    #: it wins — but a flag the user never typed must not override their config.
    force_color: bool | None = None
    ascii_only: bool = False
    animations: bool | None = None
    _loaded: LoadedSettings | None = None
    _logging_ready: bool = False
    _renderer: Any = None
    #: The palette actually in force after :meth:`_apply_appearance` (may be None
    #: before the first load); commands report it via ``ua config show``.
    palette: Any = None

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    def load(self, *, force: bool = False) -> LoadedSettings:
        if self._loaded is not None and not force:
            return self._loaded
        cwd = self.project or self.cwd
        overrides: dict[str, Any] = {}
        flag_sources: dict[str, str] = {}

        def set_override(dotted: str, value: Any, flag: str) -> None:
            if value is None:
                return
            node = overrides
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            flag_sources[dotted] = flag

        set_override("agent.mode", self.mode, "--mode")
        set_override("agent.max_steps", self.max_steps, "--max-steps")
        set_override("agent.definition_of_done", self.definition_of_done, "--dod")
        if self.provider:
            set_override("provider", self.provider, "--provider")
        if self.read_only:
            set_override("policy.read_only", True, "--read-only")
        if self.dry_run:
            set_override("policy.dry_run", True, "--dry-run")

        loaded = load_settings(
            cwd=cwd,
            config_file=self.config_file,
            overrides=overrides,
            flag_sources=flag_sources,
        )
        # `--provider <type>` must work even before the user has written a config:
        # the type is materialised into a profile, then `--model` lands on whichever
        # profile ended up being effective.
        from unknown_agent.providers.registry import ensure_provider_profile

        if self.provider and not ensure_provider_profile(loaded.settings, self.provider):
            raise ConfigError(
                f"No provider profile named {self.provider!r}.",
                hint=(
                    "Configured profiles: "
                    f"{', '.join(sorted(loaded.settings.providers)) or 'none'}. "
                    "Pass a provider type instead (offline, ollama, anthropic, openai-compatible) "
                    "or add [providers.<name>] to your config."
                ),
            )
        if self.model:
            effective = loaded.settings.provider or "default"
            profile = loaded.settings.providers.get(effective)
            if profile is not None:
                profile.model = self.model
            flag_sources[f"providers.{effective}.model"] = "--model"
        self._loaded = loaded
        self._apply_appearance(loaded.settings)
        return loaded

    def _apply_appearance(self, settings: Any) -> None:
        """Make ``[appearance]`` real: theme, glyphs, colour depth, motion.

        Called from :meth:`load` rather than from the CLI callback because the
        callback runs before any config is read.  Flags beat config; a broken theme
        name is a config error with the valid list, not a traceback — a typo in a
        config file should never make every ``ua`` command unusable.
        """

        from unknown_agent.ui.console import output
        from unknown_agent.ui.theme import apply_appearance, resolve_theme_key

        appearance = getattr(settings, "appearance", None)
        if appearance is None:
            return
        force_depth = None if self.force_color is None else ("truecolor" if self.force_color else "none")
        try:
            palette = apply_appearance(
                appearance,
                force_glyphs="ascii" if self.ascii_only else None,
                force_depth=force_depth,
            )
        except KeyError:
            from unknown_agent.ui.theme import PALETTE_ORDER

            raise ConfigError(
                f"Unknown theme {getattr(appearance, 'theme', None)!r} in [appearance].",
                hint=(
                    f"Valid themes: {', '.join(PALETTE_ORDER)}. "
                    'Or use theme = "auto" with mode = "dark" / "light" to follow your terminal.'
                ),
            ) from None
        # Motion and timings are rendering policy, not theme: they belong on the
        # output manager so spinners, live panels and cost lines all agree.
        ascii_requested = self.ascii_only or str(getattr(appearance, "glyphs", "unicode")) == "ascii"
        from unknown_agent.ui.console import target_width

        # ``max_width`` is a *cap*, not a target: forcing Rich to 120 columns on an
        # 80-column terminal would emit lines the terminal then re-wraps.  Honour
        # the cap by taking the narrower of the two.
        cap = int(getattr(appearance, "max_width", 0) or 0)
        width = min(target_width(), cap) if cap else None
        output.configure(
            mode=output.mode,
            quiet=output.quiet,
            verbose=output.verbose,
            # Derived from the flag and the config alone, never from ``output``'s
            # current value: reading global state here made one ``--ascii``
            # invocation stick for the rest of the process (a second ``Runtime``
            # could never get its box drawing back).
            unicode_ok=not ascii_requested,
            animations=(self.animations if self.animations is not None else bool(appearance.wants_motion)),
            show_timings=bool(getattr(appearance, "show_timings", True)),
            force_color=self.force_color,
            width=width,
        )
        self.palette = palette

    @property
    def settings(self) -> Any:
        return self.load().settings

    @property
    def paths(self) -> AppPaths:
        return self.load().paths

    # ------------------------------------------------------------------ #
    # logging / prompting
    # ------------------------------------------------------------------ #
    def save_settings(self, settings: Any) -> Path:
        """Persist settings to the user config file (0600), and refresh the cache."""

        from unknown_agent.config.loader import dump_settings

        target = self.config_file or self.paths.user_config_file
        dump_settings(settings, target)
        self._loaded = None  # force a reload so later commands see the new values
        log_ready = self._logging_ready
        self._logging_ready = log_ready
        return target

    def setup_state(self) -> Any:
        """Is this installation ready to work?  See onboarding.detect."""

        from unknown_agent.onboarding.detect import detect_setup_state

        return detect_setup_state(self.load().settings)

    def start_logging(self) -> None:
        if self._logging_ready:
            return
        settings = self.settings
        paths = self.paths
        level = "debug" if self.debug else ("debug" if self.verbosity > 1 else settings.logging.level)
        setup_logging(
            level=level,
            fmt=settings.logging.format,
            console=output.diag,
            log_file=paths.log_file if settings.logging.file_enabled else None,
            file_enabled=settings.logging.file_enabled,
            include_timestamps=True,
            max_bytes=settings.logging.file_max_bytes,
            backup_count=settings.logging.file_backup_count,
            verbosity=self.verbosity + (1 if self.debug else 0),
            force=True,
        )
        self._logging_ready = True

    @property
    def prompter(self) -> Prompter:
        return Prompter(assume_yes=self.assume_yes, non_interactive=self.non_interactive)

    # ------------------------------------------------------------------ #
    # running the agent
    # ------------------------------------------------------------------ #
    def make_runner(self, request: Any, *, hooks: Any = None, event_sink: Any = None) -> Any:
        from unknown_agent.core.loop import LoopHooks
        from unknown_agent.core.runner import Runner

        loaded = self.load()
        resolved_hooks = hooks or LoopHooks()
        if resolved_hooks.on_event is None and not output.is_machine:
            renderer = RunRenderer(stream_json=output.mode == "ndjson")
            resolved_hooks.on_event = renderer.handle_event
            resolved_hooks.on_thought = renderer.thought
            resolved_hooks.on_tool_start = renderer.tool_start
            resolved_hooks.on_tool_end = renderer.tool_end
            resolved_hooks.on_verification = renderer.verification
            self._renderer = renderer
        return Runner(loaded, request=request, hooks=resolved_hooks, event_sink=event_sink)

    def execute_run(self, request: Any, *, on_result: Any = None) -> Any:
        """Run a request synchronously, returning a :class:`RunResult`."""

        from unknown_agent.core.runner import RunResult

        runner = None

        async def _run() -> RunResult:
            nonlocal runner
            runner = self.make_runner(request)
            await runner.setup()
            if self._renderer is not None:
                self._renderer.start(request.task)
            return await runner.execute()

        try:
            result = asyncio.run(_run())
        except KeyboardInterrupt:  # pragma: no cover - second Ctrl-C
            output.diag_print("[ua.warn]interrupted[/ua.warn]")
            raise SystemExit(ExitCode.INTERRUPTED) from None
        finally:
            if runner is not None and self._renderer is not None:
                self._renderer.stop()
        if on_result is not None:
            on_result(result)
        return result

    # ------------------------------------------------------------------ #
    # output helpers
    # ------------------------------------------------------------------ #
    def emit_json(self, payload: Any) -> None:
        """Write a JSON document to stdout without any terminal formatting."""

        output.raw(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default))

    @property
    def is_machine(self) -> bool:
        return output.is_machine

    @property
    def quiet(self) -> bool:
        return output.quiet

    def debug_enabled(self) -> bool:
        return self.debug or self.verbosity > 0

    def panel_if_human(self, *args: Any, **kwargs: Any) -> None:
        if not self.is_machine:
            output.print(*args, **kwargs)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def run_async(coro: Any) -> Any:
    """Run a coroutine from a sync command, preserving Ctrl-C semantics."""

    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit(ExitCode.INTERRUPTED) from None


@contextlib.contextmanager
def timed(label: str) -> Any:
    started = time.perf_counter()
    try:
        yield
    finally:
        if output.verbose > 0:
            output.diag_print(f"[ua.muted]{label}: {time.perf_counter() - started:.2f}s[/ua.muted]")


__all__ = ["Runtime", "run_async", "timed", "FLAG_TO_SETTING"]
