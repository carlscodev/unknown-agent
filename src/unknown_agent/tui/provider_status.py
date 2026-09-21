"""Provider status as *UI state*, resolved after the TUI is already on screen.

This module exists because of a specific bug: running the TUI against an account with
no credit produced an HTTP 402 during startup, and the TUI never appeared.  The user
was locked out of a local application by a remote billing condition.

The architectural mistake was ordering.  Startup did:

    load settings → build provider → validate credential / list models → draw UI

so every remote failure was a launch failure.  There is no reason for that coupling:
the shell, the transcript, the config, the history and the command palette are all
local.  The provider is only needed when the user actually sends a task.

The order is now:

    draw UI (immediately) → publish "validating" → resolve health off the UI thread
    → publish a concrete status → offer in-UI recovery

:class:`ProviderStatusController` owns that lifecycle.  It never raises: every failure
path ends as a :class:`ProviderState` the sidebar can render and the user can act on.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.auth import AuthConflict, resolve_auth_mode
from unknown_agent.providers.health import ProviderReport, ProviderStatus, classify_provider_error, run_provider_check

log = get_logger("tui.provider_status")

__all__ = ["ProviderState", "ProviderStatusController", "StartupHealthResolver", "active_profile_name"]


def active_profile_name(settings: Any) -> str:
    """Name of the profile in use.

    ``Settings.provider`` is the selector; ``agent.provider`` is an older override that
    some configs still carry. Checking both means a switch made by either path is
    honoured rather than silently ignored.
    """

    for candidate in (getattr(settings, "provider", ""), getattr(getattr(settings, "agent", None), "provider", "")):
        if candidate:
            return str(candidate)
    profiles = getattr(settings, "providers", {}) or {}
    return next(iter(profiles), "") if isinstance(profiles, dict) else ""


@dataclass(slots=True)
class ProviderState:
    """Everything the UI needs to render provider health, and nothing more."""

    status: str = ProviderStatus.NOT_CONFIGURED
    provider: str = "—"
    provider_label: str = ""
    model: str = "—"
    auth_mode: str = ""
    auth_mode_label: str = ""
    message: str = ""
    hint: str = ""
    model_count: int = 0
    conflicts: tuple[AuthConflict, ...] = ()
    capabilities: dict[str, bool] = field(default_factory=dict)
    context_window: int = 0
    checked_at: float = 0.0
    #: How many recovery attempts have been made since the last good state.
    attempts: int = 0

    @property
    def label(self) -> str:
        return ProviderStatus.label(self.status)

    @property
    def style(self) -> str:
        return ProviderStatus.style(self.status)

    @property
    def ok(self) -> bool:
        return self.status in {ProviderStatus.READY, ProviderStatus.CONNECTED}

    @property
    def can_send(self) -> bool:
        """May the user send a task?

        Deliberately permissive.  We only hard-stop when there is provably nothing to
        call; for everything else the user may try, because our health picture can be
        stale and blocking them on a guess is exactly the bug being fixed.
        """

        return self.status not in {ProviderStatus.NOT_CONFIGURED, ProviderStatus.SETUP_REQUIRED}

    @property
    def remedies(self) -> tuple[str, ...]:
        from unknown_agent.providers.health import REMEDIES

        return REMEDIES.get(self.status, ())

    def summary(self) -> str:
        """One line for the status bar."""

        bits = [f"{self.provider_label or self.provider}", self.model or "no model"]
        if self.auth_mode_label:
            bits.append(self.auth_mode_label.lower())
        return " · ".join(bit for bit in bits if bit and bit != "—")


class ProviderStatusController:
    """Owns provider health for the lifetime of a TUI session.

    Subscribers (the sidebar, the status bar, the banner) register a callback and get
    pushed every transition.  Callbacks are invoked defensively: a broken widget must
    not take down health resolution, and a failing provider must not take down a widget.
    """

    def __init__(self, settings: Any = None, *, on_change: Callable[[ProviderState], None] | None = None) -> None:
        self.settings = settings
        self._listeners: list[Callable[[ProviderState], None]] = []
        if on_change is not None:
            self._listeners.append(on_change)
        self.state = self._initial_state(settings)
        self.last_report: ProviderReport | None = None

    # -- subscription ------------------------------------------------------- #
    def subscribe(self, listener: Callable[[ProviderState], None]) -> None:
        self._listeners.append(listener)
        self._emit()

    def _emit(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self.state)
            except Exception:  # noqa: BLE001 - a bad listener must not break health
                log.debug("provider status listener failed", exc_info=True)

    def _set(self, **changes: Any) -> ProviderState:
        """Replace the state with a *new* object rather than mutating in place.

        Textual reactives compare by identity before firing a watcher, so mutating the
        existing instance leaves the sidebar frozen on the first status it ever saw.
        Immutable transitions also make the state safe to hand across threads.
        """

        self.state = dataclasses.replace(self.state, **changes)
        self._emit()
        return self.state

    # -- local-only bootstrap ----------------------------------------------- #
    @staticmethod
    def _initial_state(settings: Any) -> ProviderState:
        """Derive a state from *local config alone* — no network, no exceptions.

        This is what the UI renders on its first frame.  It is allowed to be optimistic
        about nothing: it reports what is written down, and marks it unverified.
        """

        state = ProviderState()
        try:
            profile_name = active_profile_name(settings)
            profiles = getattr(settings, "providers", {}) or {}
            profile = profiles.get(profile_name) if isinstance(profiles, dict) else getattr(profiles, profile_name, None)
            provider_type = getattr(profile, "type", "") or profile_name
            state.provider = profile_name or "—"
            state.model = getattr(profile, "model", "") or "—"
            state.context_window = int(getattr(profile, "context_window", 0) or 0)

            from unknown_agent.providers.catalog import resolve_entry

            entry = resolve_entry(provider_type)
            if entry is not None:
                state.provider_label = entry.label
                mode = resolve_auth_mode(entry, getattr(profile, "auth_mode", "") or None)
                state.auth_mode = mode.key
                state.auth_mode_label = mode.label
            if not profile_name or state.model == "—":
                state.status = ProviderStatus.NOT_CONFIGURED
                state.message = "No provider configured yet."
                state.hint = "Press ctrl+p → 'setup', or run `ua setup`."
            else:
                state.status = ProviderStatus.VALIDATING
                state.message = "Checking the provider in the background…"
        except Exception as exc:  # noqa: BLE001 - config problems must not block startup
            log.debug("could not derive initial provider state: %s", exc)
            state.status = ProviderStatus.NOT_CONFIGURED
            state.message = "Could not read the provider configuration."
            state.hint = "Run `ua doctor`, or press ctrl+p → 'setup'."
        return state

    # -- background resolution ---------------------------------------------- #
    async def resolve(self, *, probe_inference: bool = False, timeout: float = 20.0) -> ProviderState:
        """Resolve real provider health.  Safe to call any time, never raises.

        Called once after mount and again on every retry.  A timeout is applied so a
        black-holed endpoint degrades to "offline" instead of leaving the UI showing
        "validating…" forever.
        """

        if self.state.status == ProviderStatus.NOT_CONFIGURED and not self.state.provider_label:
            return self.state

        self._set(
            status=ProviderStatus.VALIDATING if self.state.attempts == 0 else ProviderStatus.RECOVERING,
            message="Checking the provider…",
            hint="",
        )

        try:
            report = await asyncio.wait_for(self._check(probe_inference=probe_inference), timeout=timeout)
        except asyncio.TimeoutError:
            return self._set(
                status=ProviderStatus.OFFLINE,
                message="The provider did not answer in time.",
                hint="Check the network or endpoint, then press r to retry. The shell keeps working.",
                checked_at=time.time(),
                attempts=self.state.attempts + 1,
            )
        except Exception as exc:  # noqa: BLE001 - the whole point: nothing escapes
            log.debug("provider health check raised", exc_info=True)
            return self._set(
                status=classify_provider_error(exc),
                message=str(exc)[:240],
                hint="Press r to retry, or ctrl+p → 'switch provider'.",
                checked_at=time.time(),
                attempts=self.state.attempts + 1,
            )

        self.last_report = report
        return self._apply(report)

    async def _check(self, *, probe_inference: bool) -> ProviderReport:
        """Resolve config → catalog entry → credential, then run the real check."""

        from unknown_agent.providers.catalog import resolve_entry

        settings = self.settings
        profile_name = active_profile_name(settings)
        profiles = getattr(settings, "providers", {}) or {}
        profile = profiles.get(profile_name) if isinstance(profiles, dict) else getattr(profiles, profile_name, None)
        if profile is None:
            report = ProviderReport(provider=profile_name, status=ProviderStatus.NOT_CONFIGURED)
            report.message = "No provider profile is selected."
            report.hint = "Press ctrl+p → 'setup'."
            return report

        entry = resolve_entry(getattr(profile, "type", "") or profile_name)
        if entry is None:
            report = ProviderReport(provider=profile_name, status=ProviderStatus.SETUP_REQUIRED)
            report.message = f"Unknown provider type {getattr(profile, 'type', '')!r}."
            report.hint = "Press ctrl+p → 'switch provider'."
            return report

        mode = resolve_auth_mode(entry, getattr(profile, "auth_mode", "") or None)
        api_key = await asyncio.to_thread(self._resolve_key, profile, entry)
        return await run_provider_check(
            entry,
            mode,
            api_key=api_key,
            base_url=getattr(profile, "base_url", "") or None,
            model=getattr(profile, "model", "") or "",
            probe_inference=probe_inference,
        )

    @staticmethod
    def _resolve_key(profile: Any, entry: Any) -> str | None:
        """Read the credential without ever logging or returning it to the UI layer."""

        try:
            from unknown_agent.config.secrets import resolve_secret

            reference = getattr(profile, "api_key", "") or entry.credential_hint or ""
            if reference:
                return resolve_secret(reference)
        except Exception:  # noqa: BLE001
            log.debug("credential resolution failed", exc_info=True)
        import os

        return os.environ.get(getattr(entry, "env_var", "") or "") or None

    def _apply(self, report: ProviderReport) -> ProviderState:
        return self._set(
            status=report.status,
            provider_label=report.provider_label or self.state.provider_label,
            auth_mode=report.auth_mode or self.state.auth_mode,
            auth_mode_label=report.auth_mode_label or self.state.auth_mode_label,
            message=report.message,
            hint=report.hint,
            model_count=report.model_count,
            conflicts=tuple(report.conflicts),
            capabilities=dict(report.capabilities),
            checked_at=report.checked_at or time.time(),
            attempts=0 if report.ok else self.state.attempts + 1,
        )

    # -- reacting to live failures ------------------------------------------ #
    def note_runtime_error(self, error: Exception | str) -> ProviderState:
        """Fold a mid-session provider failure into the status.

        A 402 raised by an actual run is the same condition the startup check looks
        for, so it lands in the same state machine and offers the same remedies —
        instead of surfacing as a traceback that kills the session.
        """

        status = classify_provider_error(error)
        from unknown_agent.providers.health import _hint_for

        return self._set(
            status=status,
            message=str(error)[:240],
            hint=_hint_for(status, self.state.provider_label or self.state.provider),
            checked_at=time.time(),
            attempts=self.state.attempts + 1,
        )

    def note_switch(self, *, provider: str = "", model: str = "", auth_mode: str = "") -> ProviderState:
        """The user changed something; reflect it at once and mark it unverified."""

        changes: dict[str, Any] = {"status": ProviderStatus.VALIDATING, "message": "Checking the new selection…", "attempts": 0}
        if provider:
            changes["provider"] = provider
        if model:
            changes["model"] = model
        if auth_mode:
            changes["auth_mode"] = auth_mode
        return self._set(**changes)


class StartupHealthResolver:
    """Runs the first health check *after* the UI is interactive.

    Kept separate from the controller so the rule is enforced by structure: this is the
    only code that runs at startup, it is scheduled from ``on_mount``, and it cannot
    block the first frame.  ``delay`` lets the UI paint and accept input first.
    """

    def __init__(self, controller: ProviderStatusController, *, delay: float = 0.15, probe_inference: bool = False) -> None:
        self.controller = controller
        self.delay = delay
        self.probe_inference = probe_inference
        self.done = False

    async def run(self) -> ProviderState:
        """Resolve health.  Any failure is captured as state, never propagated."""

        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            state = await self.controller.resolve(probe_inference=self.probe_inference)
        except Exception as exc:  # noqa: BLE001 - startup must survive anything
            log.debug("startup health resolver failed", exc_info=True)
            state = self.controller.note_runtime_error(exc)
        self.done = True
        return state
