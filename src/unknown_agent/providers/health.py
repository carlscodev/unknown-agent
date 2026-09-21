"""Provider health: prove a configuration works before calling it configured.

The old flow validated a credential and stopped there.  That answers "is this key
syntactically accepted?" — not "can I actually work with this provider?".  Those come
apart constantly in practice: a key that lists models but has no credit (HTTP 402), a
subscription whose session expired, a router that answers ``/models`` but cannot call
tools, an endpoint that resolves but 404s the configured model.

:func:`run_provider_check` runs the stages that matter and returns a structured
report.  Every stage is independently reportable, so the panel can say *which* part is
broken and offer the matching fix.  Nothing here raises for provider-side problems:
a failing provider is data, not an exception, because the UI must stay alive.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from unknown_agent.observability.logging import get_logger
from unknown_agent.providers import resilience
from unknown_agent.providers.auth import AuthConflict, AuthMode, cli_session_state, detect_auth_conflicts
from unknown_agent.providers.discovery import ModelInfo, ProviderProbeError, fetch_models, validate_credential
from unknown_agent.providers.resilience import FailureKind

log = get_logger("providers.health")

__all__ = [
    "ProviderStatus",
    "CheckStage",
    "ProviderReport",
    "run_provider_check",
    "classify_provider_error",
    "classify_provider_failure",
    "KIND_TO_STATUS",
]


# --------------------------------------------------------------------------- #
# status vocabulary
# --------------------------------------------------------------------------- #
class ProviderStatus:
    """The states the UI can render.  Strings, so they survive JSON round-trips."""

    READY = "ready"                      # tested, works
    CONNECTED = "connected"              # auth good, models listed, inference unproven
    NOT_CONFIGURED = "not_configured"
    SETUP_REQUIRED = "setup_required"
    VALIDATING = "validating"
    LOGIN_REQUIRED = "login_required"    # subscription session expired / absent
    INVALID_CREDENTIALS = "invalid_credentials"
    CREDITS_EXHAUSTED = "credits_exhausted"   # HTTP 402 and friends
    RATE_LIMITED = "rate_limited"        # *we* sent too much: back off
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"  # *their* upstream is empty
    DEGRADED = "degraded"                # partially working
    OFFLINE = "offline"                  # network/endpoint unreachable
    AUTH_CONFLICT = "auth_conflict"
    RECOVERING = "recovering"

    #: Status → (label, style token) for consistent rendering everywhere.
    LABELS: dict[str, tuple[str, str]] = {
        READY: ("ready", "success"),
        CONNECTED: ("connected", "success"),
        NOT_CONFIGURED: ("not configured", "warning"),
        SETUP_REQUIRED: ("setup required", "warning"),
        VALIDATING: ("validating…", "accent"),
        LOGIN_REQUIRED: ("login required", "warning"),
        INVALID_CREDENTIALS: ("invalid credentials", "error"),
        CREDITS_EXHAUSTED: ("credits exhausted", "error"),
        RATE_LIMITED: ("rate limited", "warning"),
        TEMPORARILY_UNAVAILABLE: ("temporarily unavailable", "warning"),
        DEGRADED: ("degraded", "warning"),
        OFFLINE: ("offline", "error"),
        AUTH_CONFLICT: ("auth conflict", "warning"),
        RECOVERING: ("reconnecting…", "accent"),
    }

    #: Statuses where the product still works, just not through this provider.
    #:
    #: ``RATE_LIMITED`` and ``TEMPORARILY_UNAVAILABLE`` are deliberately *absent*:
    #: both are transient, so the shell must stay usable and the user must be able
    #: to switch model/provider or wait out the cooldown.  Locking the UI on a
    #: provider's temporary capacity problem is the bug this module exists to fix.
    BLOCKING = frozenset({NOT_CONFIGURED, SETUP_REQUIRED, INVALID_CREDENTIALS, LOGIN_REQUIRED, CREDITS_EXHAUSTED, OFFLINE})

    @classmethod
    def label(cls, status: str) -> str:
        return cls.LABELS.get(status, (status, "muted"))[0]

    @classmethod
    def style(cls, status: str) -> str:
        return cls.LABELS.get(status, (status, "muted"))[1]


#: What a user can do about each status, as concrete in-product actions.
REMEDIES: dict[str, tuple[str, ...]] = {
    ProviderStatus.NOT_CONFIGURED: ("run setup", "switch provider"),
    ProviderStatus.SETUP_REQUIRED: ("run setup", "switch provider"),
    ProviderStatus.LOGIN_REQUIRED: ("sign in", "switch auth mode", "switch provider"),
    ProviderStatus.INVALID_CREDENTIALS: ("re-enter credentials", "switch auth mode", "switch provider"),
    ProviderStatus.CREDITS_EXHAUSTED: ("switch provider", "switch model", "top up the account"),
    ProviderStatus.RATE_LIMITED: ("wait for cooldown", "retry", "switch model", "switch provider"),
    ProviderStatus.TEMPORARILY_UNAVAILABLE: ("wait for cooldown", "switch provider", "switch model", "retry"),
    ProviderStatus.OFFLINE: ("retry", "continue offline"),
    ProviderStatus.AUTH_CONFLICT: ("resolve conflict", "switch auth mode"),
    ProviderStatus.DEGRADED: ("retry", "switch model", "continue anyway"),
}


#: ``resilience.FailureKind`` → the status vocabulary the UI renders.
#:
#: This is the only place the two vocabularies meet, so the sidebar and the
#: transcript can no longer disagree about the same failure.
KIND_TO_STATUS: dict[str, str] = {
    FailureKind.OK: ProviderStatus.CONNECTED,
    FailureKind.INVALID_CREDENTIALS: ProviderStatus.INVALID_CREDENTIALS,
    FailureKind.CREDITS_EXHAUSTED: ProviderStatus.CREDITS_EXHAUSTED,
    FailureKind.RATE_LIMITED: ProviderStatus.RATE_LIMITED,
    FailureKind.NO_CAPACITY: ProviderStatus.TEMPORARILY_UNAVAILABLE,
    FailureKind.TEMPORARILY_UNAVAILABLE: ProviderStatus.TEMPORARILY_UNAVAILABLE,
    FailureKind.INCOMPATIBLE_API: ProviderStatus.DEGRADED,
    FailureKind.NOT_FOUND: ProviderStatus.DEGRADED,
    FailureKind.CONTEXT_LENGTH: ProviderStatus.DEGRADED,
    FailureKind.TIMEOUT: ProviderStatus.OFFLINE,
    FailureKind.OFFLINE: ProviderStatus.OFFLINE,
    FailureKind.UNKNOWN: ProviderStatus.DEGRADED,
}


def classify_provider_error(error: Exception | str, *, status_code: int | None = None) -> str:
    """Map a provider failure onto a :class:`ProviderStatus`.

    Thin adapter over :func:`unknown_agent.providers.resilience.classify`, kept at
    this signature because the whole TUI already calls it.  The important part is
    what it no longer does: it used to scan the text for the literal ``"rate limit"``,
    so unli.dev's ``provider_account_unavailable`` capacity error was either dumped
    raw or mislabelled.  The shared classifier reads the status code and the
    provider's own machine code first.
    """

    failure = resilience.classify(error, status_code=status_code)
    return KIND_TO_STATUS.get(failure.kind, ProviderStatus.DEGRADED)


def classify_provider_failure(error: Exception | str, *, status_code: int | None = None) -> resilience.Failure:
    """The full classified failure, for callers that need the hint and the timing."""

    return resilience.classify(error, status_code=status_code)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CheckStage:
    """One verifiable claim about the provider."""

    name: str
    state: str          # "ok" | "fail" | "warn" | "skip"
    detail: str = ""
    fix: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.state in {"ok", "skip"}

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "detail": self.detail, "fix": self.fix, "elapsed_ms": self.elapsed_ms}


@dataclass(slots=True)
class ProviderReport:
    """The normalised outcome of testing one provider configuration."""

    provider: str = ""
    provider_label: str = ""
    auth_mode: str = ""
    auth_mode_label: str = ""
    status: str = ProviderStatus.NOT_CONFIGURED
    message: str = ""
    hint: str = ""
    stages: list[CheckStage] = field(default_factory=list)
    conflicts: list[AuthConflict] = field(default_factory=list)
    models: list[ModelInfo] = field(default_factory=list)
    model_count: int = 0
    capabilities: dict[str, bool] = field(default_factory=dict)
    checked_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in {ProviderStatus.READY, ProviderStatus.CONNECTED}

    @property
    def usable(self) -> bool:
        """Can the agent plausibly run?  Degraded still counts — the user decides."""

        return self.status not in ProviderStatus.BLOCKING

    @property
    def remedies(self) -> tuple[str, ...]:
        return REMEDIES.get(self.status, ())

    def stage(self, name: str) -> CheckStage | None:
        return next((item for item in self.stages if item.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_label": self.provider_label,
            "auth_mode": self.auth_mode,
            "auth_mode_label": self.auth_mode_label,
            "status": self.status,
            "status_label": ProviderStatus.label(self.status),
            "ok": self.ok,
            "usable": self.usable,
            "message": self.message,
            "hint": self.hint,
            "stages": [item.to_dict() for item in self.stages],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "model_count": self.model_count,
            "models": [item.id for item in self.models[:50]],
            "capabilities": dict(self.capabilities),
            "remedies": list(self.remedies),
            "checked_at": self.checked_at,
        }


# --------------------------------------------------------------------------- #
# the check
# --------------------------------------------------------------------------- #
async def run_provider_check(
    entry: Any,
    mode: AuthMode,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "",
    probe_inference: bool = False,
    env: dict[str, str] | None = None,
    compatibility: str | None = None,
) -> ProviderReport:
    """Test a provider configuration end to end and report what is actually true.

    Stages, in order and each independently reported:

    1. **auth conflicts** — environment state that would defeat the chosen mode;
    2. **session / credential** — a live login for subscription modes, a validated
       credential for key modes;
    3. **model listing** — can we enumerate models in this mode?
    4. **model availability** — is the configured model actually in that list?
    5. **capabilities** — what the chosen model can do (tools, vision, context).

    Never raises for provider-side failure: the report carries the bad news.
    """

    report = ProviderReport(
        provider=getattr(entry, "key", ""),
        provider_label=getattr(entry, "label", ""),
        auth_mode=mode.key,
        auth_mode_label=mode.label,
        checked_at=time.time(),
    )

    # -- 1. conflicts ------------------------------------------------------ #
    report.conflicts = detect_auth_conflicts(entry, mode, env=env)
    blocking_conflict = next((item for item in report.conflicts if item.severity == "error"), None)
    if report.conflicts:
        worst = "fail" if blocking_conflict else "warn"
        first = blocking_conflict or report.conflicts[0]
        report.stages.append(CheckStage("auth conflicts", worst, first.title, fix=first.fix))
    else:
        report.stages.append(CheckStage("auth conflicts", "ok", "nothing in the environment overrides this mode"))

    if blocking_conflict is not None:
        report.status = ProviderStatus.SETUP_REQUIRED
        report.message = blocking_conflict.title
        report.hint = blocking_conflict.fix
        return report

    resolved_base = base_url or mode.default_base_url or getattr(entry, "base_url", "") or None

    # -- 2. session or credential ------------------------------------------ #
    started = time.monotonic()
    if mode.kind == "subscription":
        # ``cli_session_state`` shells out with an 8s timeout.  This function is
        # awaited on the Textual event loop, so calling it directly froze the whole
        # UI for the duration — the "setup hangs after a few seconds" bug.  It is
        # blocking by nature (a subprocess), so it belongs on a worker thread.
        state, detail = await asyncio.to_thread(cli_session_state, mode)
        elapsed = int((time.monotonic() - started) * 1000)
        if state == "ok":
            report.stages.append(CheckStage("subscription session", "ok", detail, elapsed_ms=elapsed))
        elif state == "missing":
            report.stages.append(
                CheckStage("subscription session", "fail", detail, fix=f"Install `{mode.cli_command}`", elapsed_ms=elapsed)
            )
            report.status = ProviderStatus.SETUP_REQUIRED
            report.message = detail
            report.hint = f"Install the `{mode.cli_command}` CLI, or switch to API-key mode."
            return report
        else:
            report.stages.append(
                CheckStage(
                    "subscription session",
                    "fail",
                    detail,
                    fix=f"{mode.cli_command} {' '.join(mode.cli_login_args or ('login',))}",
                    elapsed_ms=elapsed,
                )
            )
            report.status = ProviderStatus.LOGIN_REQUIRED
            report.message = detail
            report.hint = f"Sign in with `{mode.cli_command} {' '.join(mode.cli_login_args or ('login',))}`."
            return report
    elif mode.kind == "none":
        report.stages.append(CheckStage("credential", "skip", "this provider needs no credential"))
    else:
        if not api_key:
            report.stages.append(CheckStage("credential", "fail", "no credential supplied", fix="Run `ua setup`"))
            report.status = ProviderStatus.NOT_CONFIGURED
            report.message = f"{report.provider_label} needs a credential for {mode.label}."
            report.hint = "Run `ua setup` and paste it into the panel."
            return report
        try:
            # ``validate_credential`` is monkeypatched in tests without the new
            # ``compatibility`` kwarg — be tolerant so the health suite stays green.
            try:
                result = await validate_credential(entry, api_key=api_key, base_url=resolved_base, compatibility=compatibility)  # type: ignore[call-arg]
            except TypeError as exc:
                if "compatibility" in str(exc):
                    result = await validate_credential(entry, api_key=api_key, base_url=resolved_base)  # type: ignore[call-arg]
                else:
                    raise
        except Exception as exc:  # noqa: BLE001 - provider failure is data
            elapsed = int((time.monotonic() - started) * 1000)
            status = classify_provider_error(exc)
            report.stages.append(CheckStage("credential", "fail", str(exc)[:200], elapsed_ms=elapsed))
            report.status = status
            report.message = str(exc)[:300]
            report.hint = _hint_for(status, report.provider_label)
            return report
        elapsed = int((time.monotonic() - started) * 1000)
        if result.ok:
            report.stages.append(CheckStage("credential", "ok", result.message, elapsed_ms=elapsed))
        else:
            status = classify_provider_error(result.message)
            report.stages.append(
                CheckStage("credential", "fail", result.message, fix=result.hint or "", elapsed_ms=elapsed)
            )
            report.status = status
            report.message = result.message
            report.hint = result.hint or _hint_for(status, report.provider_label)
            return report

    # -- 3. model listing --------------------------------------------------- #
    started = time.monotonic()
    if not mode.supports_model_listing:
        report.stages.append(
            CheckStage("model listing", "skip", f"{mode.label} does not expose a model list; using the curated set")
        )
        from unknown_agent.providers.catalog import known_model_ids

        report.models = [ModelInfo(id=mid, name=mid, provider=report.provider) for mid in known_model_ids(entry)]
    else:
        try:
            try:
                report.models = list(await fetch_models(entry, api_key=api_key or None, base_url=resolved_base, compatibility=compatibility))  # type: ignore[call-arg]
            except TypeError as exc:
                if "compatibility" in str(exc):
                    report.models = list(await fetch_models(entry, api_key=api_key or None, base_url=resolved_base))  # type: ignore[call-arg]
                else:
                    raise
            report.stages.append(
                CheckStage(
                    "model listing",
                    "ok",
                    f"{len(report.models)} model(s) available",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except ProviderProbeError as exc:
            status = classify_provider_error(exc.message)
            report.stages.append(
                CheckStage("model listing", "fail", exc.message, fix=exc.hint or "", elapsed_ms=int((time.monotonic() - started) * 1000))
            )
            # A listing failure after a good credential is degraded, not dead — unless
            # it is a billing or auth problem, which the classifier already knows.
            report.status = status if status != ProviderStatus.DEGRADED else ProviderStatus.DEGRADED
            report.message = exc.message
            report.hint = exc.hint or _hint_for(report.status, report.provider_label)
            return report
        except Exception as exc:  # noqa: BLE001
            report.stages.append(CheckStage("model listing", "fail", str(exc)[:200]))
            report.status = classify_provider_error(exc)
            report.message = str(exc)[:300]
            report.hint = _hint_for(report.status, report.provider_label)
            return report

    report.model_count = len(report.models)

    # -- 4. is the configured model actually there? ------------------------- #
    if model:
        match = next((item for item in report.models if item.id == model), None)
        if match is not None:
            report.stages.append(CheckStage("model available", "ok", f"{model} is offered by this provider"))
            report.capabilities = {
                "tools": bool(match.supports_tools),
                "vision": bool(match.supports_vision),
                "reasoning": bool(match.supports_reasoning),
                "streaming": bool(getattr(entry, "supports_streaming", True)),
            }
        elif report.models:
            report.stages.append(
                CheckStage(
                    "model available",
                    "warn",
                    f"{model} is not in this provider's list",
                    fix="Pick another model with `ua models use`",
                )
            )
        else:
            report.stages.append(CheckStage("model available", "skip", "no list to check against"))
    else:
        report.stages.append(CheckStage("model available", "skip", "no model selected yet"))

    # -- 5. inference probe (opt-in: it costs a token) ---------------------- #
    if probe_inference and model:
        started = time.monotonic()
        ok, detail = await _probe_inference(entry, api_key=api_key, base_url=resolved_base, model=model, compatibility=compatibility)
        elapsed = int((time.monotonic() - started) * 1000)
        if ok:
            report.stages.append(CheckStage("inference", "ok", detail, elapsed_ms=elapsed))
            report.status = ProviderStatus.READY
            report.message = f"{report.provider_label} is ready via {mode.label}."
            return _finish(report)
        status = classify_provider_error(detail)
        report.stages.append(CheckStage("inference", "fail", detail, elapsed_ms=elapsed))
        report.status = status
        report.message = detail
        report.hint = _hint_for(status, report.provider_label)
        return report

    report.status = ProviderStatus.CONNECTED
    report.message = f"{report.provider_label} answered over {mode.label} with {report.model_count} model(s)."
    return _finish(report)


def _finish(report: ProviderReport) -> ProviderReport:
    """Downgrade to a warning status when a non-fatal stage complained."""

    if any(stage.state == "warn" for stage in report.stages) and report.status == ProviderStatus.CONNECTED:
        if any(item.severity == "warn" for item in report.conflicts):
            report.status = ProviderStatus.AUTH_CONFLICT
            first = next(item for item in report.conflicts if item.severity == "warn")
            report.message = first.title
            report.hint = first.fix
    return report


async def _probe_inference(entry: Any, *, api_key: str | None, base_url: str | None, model: str, compatibility: str | None = None) -> tuple[bool, str]:
    """One tiny completion: the only honest proof that the whole path works."""

    try:
        from unknown_agent.providers.discovery import build_client, effective_entry_for_compatibility
        from unknown_agent.providers.compatibility import normalize_compatibility

        # Respect explicit compatibility (custom anthropic gateway, etc.)
        compat_norm = normalize_compatibility(compatibility) if compatibility else None
        eff_entry = effective_entry_for_compatibility(entry, compat_norm) if compat_norm else entry
        try:
            client = build_client(eff_entry, api_key=api_key, base_url=base_url, timeout=30.0, compatibility=compat_norm)  # type: ignore[call-arg]
        except TypeError as exc:
            if "compatibility" in str(exc):
                client = build_client(eff_entry, api_key=api_key, base_url=base_url, timeout=30.0)  # type: ignore[call-arg]
            else:
                raise
        protocol = getattr(eff_entry, "protocol", "openai")
        if protocol == "anthropic" or compat_norm == "anthropic":
            payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            path = "/messages"
        else:
            payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
            path = "/chat/completions"
        try:
            response = await client.post(path, json=payload)
        finally:
            await client.aclose()
        if response.status_code < 300:
            return True, f"{model} accepted a one-token request"
        body = (response.text or "")[:200]
        return False, f"HTTP {response.status_code}: {body}"
    except Exception as exc:  # noqa: BLE001 - probing must never raise upward
        return False, f"{type(exc).__name__}: {exc}"


def _hint_for(status: str, provider_label: str) -> str:
    return {
        ProviderStatus.CREDITS_EXHAUSTED: (
            f"{provider_label} accepted the credential but the account cannot pay for the request. "
            "Top it up, or switch provider/model — Unknown Agent keeps running either way."
        ),
        ProviderStatus.INVALID_CREDENTIALS: "Re-enter the credential in `ua setup`, or switch auth mode.",
        ProviderStatus.LOGIN_REQUIRED: "Sign in again from the setup panel.",
        ProviderStatus.RATE_LIMITED: "We are sending too much for this key. The app is backing off automatically; you can also switch to a less contended model.",
        ProviderStatus.TEMPORARILY_UNAVAILABLE: (
            f"{provider_label}'s upstream has no free capacity right now. Your key is fine — "
            "wait for the cooldown, switch provider (f3), or switch model (f2)."
        ),
        ProviderStatus.OFFLINE: "Check the network or the endpoint URL, then retry.",
    }.get(status, "Open `ua setup` to reconfigure, or switch provider from the TUI.")
