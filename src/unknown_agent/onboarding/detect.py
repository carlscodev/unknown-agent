"""Decide whether Unknown Agent is ready to work, and what is missing.

The product must never fail with "no provider configured" and leave the user to
guess.  Every entry point (``ua``, ``ua run``, ``ua doctor``, ``ua shell``) asks
this module one question - :func:`detect_setup_state` - and routes accordingly:
straight into work, into onboarding, or into a repair path that names the exact
problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from unknown_agent.config.credentials import CredentialStore
from unknown_agent.config.secrets import resolve_secret
from unknown_agent.errors import ConfigError, UnknownAgentError
from unknown_agent.observability.logging import get_logger

log = get_logger("onboarding.detect")

#: Reasons that mean "run onboarding", not "something is broken".
NEEDS_SETUP = "needs_setup"
NEEDS_REPAIR = "needs_repair"
READY = "ready"


@dataclass(slots=True)
class SetupState:
    """Result of the readiness check."""

    status: str
    provider: str = ""
    model: str = ""
    reason: str = ""
    hint: str = ""
    profiles: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == READY

    @property
    def needs_onboarding(self) -> bool:
        return self.status == NEEDS_SETUP

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "hint": self.hint,
            "profiles": self.profiles,
            "problems": self.problems,
        }


def detect_setup_state(settings: Any, *, store: CredentialStore | None = None) -> SetupState:
    """Inspect configuration *without* making network calls.

    Fast by design: this runs on every CLI start, so it only looks at config,
    credential references and the environment.  Live checks live in
    :mod:`unknown_agent.providers.discovery` and are used by onboarding and
    ``ua doctor``.
    """

    store = store or CredentialStore()
    stored = set(store.names())
    profiles = sorted(getattr(settings, "providers", {}) or {})
    active = getattr(settings, "provider", "") or "default"
    problems: list[str] = []

    configured = [
        name
        for name in profiles
        if not getattr(getattr(settings, "providers", {})[name], "is_placeholder", False)
    ]
    if not profiles or not configured:
        return SetupState(
            status=NEEDS_SETUP,
            reason="no provider has been configured yet",
            hint="Run `ua setup` to pick a provider, paste its API key and choose a model.",
            profiles=configured,
        )

    profile = settings.providers.get(active)
    if profile is None:
        return SetupState(
            status=NEEDS_SETUP,
            provider=active,
            profiles=configured,
            reason=f"the configured default provider {active!r} no longer exists",
            hint="Run `ua setup` or `ua providers use <name>` to choose an existing profile.",
        )

    # The deterministic offline provider needs no credentials at all.
    if profile.type == "offline":
        return SetupState(status=READY, provider=active, model=profile.model, profiles=configured)

    if not profile.api_key:
        problems.append(f"providers.{active}.api_key is not set")
    else:
        try:
            secret = resolve_secret(profile.api_key, key=f"providers.{active}.api_key")
        except UnknownAgentError as exc:
            return SetupState(
                status=NEEDS_REPAIR,
                provider=active,
                model=profile.model,
                profiles=configured,
                reason=f"the credential for {active!r} cannot be resolved: {exc.message}",
                hint=exc.hint or f"Re-enter it with `ua setup`, or export {profile.api_key.split(':', 1)[-1]}.",
                problems=[str(exc.message)],
            )
        if not secret:
            problems.append(f"credential {profile.api_key!r} resolved to an empty value")
        elif profile.api_key.startswith("store:") and profile.api_key.split(":", 1)[1] not in stored:
            problems.append(f"{profile.api_key!r} is not present in {store.path}")

    if not profile.model:
        problems.append(f"providers.{active}.model is empty")

    if properties_broken(profile):
        problems.append(f"providers.{active}.supports_tools is false but the agent needs native tool calling")

    if problems:
        return SetupState(
            status=NEEDS_REPAIR,
            provider=active,
            model=profile.model,
            profiles=configured,
            reason="the active provider profile is incomplete",
            hint="Run `ua setup` (it will reuse what it can) or `ua providers test` for a live check.",
            problems=problems,
        )

    return SetupState(status=READY, provider=active, model=profile.model, profiles=configured)


def properties_broken(profile: Any) -> bool:
    """A profile that cannot call tools cannot drive the agent loop.

    This is checked here (cheaply, from config) so the product refuses early with a
    clear message instead of half-working later.  Providers whose models genuinely
    cannot call tools are allowed only in ``--read-only``-ish consultative modes,
    which set ``supports_tools`` explicitly.
    """

    from unknown_agent.providers.catalog import resolve_entry

    entry = resolve_entry(getattr(profile, "type", ""))
    if entry is not None and not entry.supports_tools:
        return False  # the vendor is honestly not tool-capable; the caller warns instead
    return not bool(getattr(profile, "supports_tools", True))


def raise_if_not_ready(state: SetupState) -> None:
    """Turn a non-ready state into the right exception for non-interactive runs."""

    if state.ready:
        return
    raise ConfigError(
        f"Unknown Agent is not configured: {state.reason}.",
        hint=state.hint or "Run `ua setup` to configure a provider and model.",
        context=state.to_dict(),
    )


__all__ = ["NEEDS_REPAIR", "NEEDS_SETUP", "READY", "SetupState", "detect_setup_state", "raise_if_not_ready"]
