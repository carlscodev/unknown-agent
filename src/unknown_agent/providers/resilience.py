"""Provider resilience: honest classification, bounded retry, and cooldown.

Three failures used to live in three places and disagreed with each other:

* ``providers.toolkit.normalise_provider_error`` read ``error.status_code`` and a
  handful of literal substrings.  But the HTTP layer had already flattened the
  status into the *message* (``"HTTP 429: {...}"``), so the attribute was ``None``
  and the substring ``"rate limit"`` was absent — unli.dev's body says
  ``provider_account_unavailable`` in Chinese.  A capacity failure therefore fell
  through to the generic catch-all and the user was handed a raw JSON blob plus
  ``"Re-run with --debug"``.
* ``providers.health.classify_provider_error`` had its own digit scan and its own
  marker list, so the sidebar and the transcript could describe one failure two
  different ways.
* ``providers.openai_compatible._backoff`` was exponential and capped but had no
  jitter, and nothing remembered that the provider had just said "no capacity".
  Every new message re-sent the same four requests into a saturated upstream.

This module is now the single place that answers:

1. **what kind of failure is this?** — :func:`classify`
2. **may we retry, and when?** — :class:`RetryPolicy` + :func:`backoff_delay`
3. **should we hold off entirely for a while?** — :class:`CooldownState`

It deliberately knows nothing about the UI.  Callers map :attr:`Failure.kind`
onto their own vocabulary (``health.ProviderStatus`` for widgets, ``ProviderError``
for the CLI) so the taxonomy stays single-sourced without importing the world.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

from unknown_agent.observability.logging import get_logger

log = get_logger("providers.resilience")

__all__ = [
    "DEFAULT_POLICY",
    "SETUP_POLICY",
    "CooldownState",
    "Failure",
    "FailureKind",
    "RetryPolicy",
    "backoff_delay",
    "classify",
    "cooldown",
    "extract_http_status",
    "parse_retry_after",
    "retry_async",
]


# --------------------------------------------------------------------------- #
# taxonomy
# --------------------------------------------------------------------------- #
class FailureKind:
    """What actually went wrong, in words a product can act on.

    ``RATE_LIMITED`` and ``NO_CAPACITY`` are deliberately separate.  Both arrive as
    HTTP 429, but the first means *you* are sending too much and backing off fixes
    it; the second means the *provider's* upstream pool is empty, retrying sooner
    only makes the queue longer, and switching provider may be the only real fix.
    Treating them alike is what made the old message useless.
    """

    OK = "ok"
    INVALID_CREDENTIALS = "invalid_credentials"
    CREDITS_EXHAUSTED = "credits_exhausted"
    RATE_LIMITED = "rate_limited"
    NO_CAPACITY = "no_capacity"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INCOMPATIBLE_API = "incompatible_api"
    NOT_FOUND = "not_found"
    CONTEXT_LENGTH = "context_length"
    TIMEOUT = "timeout"
    OFFLINE = "offline"
    UNKNOWN = "unknown"

    #: Failures where sending the same request again can plausibly succeed.
    TRANSIENT = frozenset(
        {
            RATE_LIMITED,
            NO_CAPACITY,
            TEMPORARILY_UNAVAILABLE,
            TIMEOUT,
            OFFLINE,
        }
    )

    #: Failures the user must fix; retrying is noise.
    TERMINAL = frozenset(
        {
            INVALID_CREDENTIALS,
            CREDITS_EXHAUSTED,
            INCOMPATIBLE_API,
            NOT_FOUND,
            CONTEXT_LENGTH,
        }
    )


#: Provider-specific machine codes that mean "our upstream is saturated".
#: Kept as a table, not inline conditionals: gateways invent these constantly and
#: the fix is always to add a row, not to rewrite the classifier.
NO_CAPACITY_CODES = frozenset(
    {
        "provider_account_unavailable",
        "provider_unavailable",
        "no_available_provider",
        "no_provider_capacity",
        "upstream_unavailable",
        "all_fallback_groups_unavailable",
        "insufficient_provider_capacity",
        "capacity_exhausted",
        "service_overloaded",
        "overloaded",
    }
)

#: Substrings that mean the same thing when a gateway has no machine code.
NO_CAPACITY_MARKERS = (
    "no provider capacity",
    "no capacity",
    "provider capacity",
    "capacity right now",
    "all fallback groups",
    "no available provider",
    "no available channel",
    "upstream is unavailable",
    "temporarily unavailable",
    "overloaded",
    "too many requests to upstream",
    "try again later",
)

#: The remaining prose families, named so the classifier reads as a table of rules.
AUTH_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "unauthorized",
    "not authenticated",
    "incorrect api key",
)
BILLING_MARKERS = (
    "insufficient credit",
    "insufficient_credit",
    "insufficient_quota",
    "out of credit",
    "payment required",
    "quota exceeded",
    "billing",
)
API_SHAPE_MARKERS = (
    "anthropic",
    "messages",
    "method not allowed",
    "chat/completions",
    "unsupported",
    "not supported",
)
CONTEXT_MARKERS = ("context length", "too many tokens", "maximum context", "context window")
TIMEOUT_MARKERS = ("timed out", "timeout", "readtimeout", "connecttimeout")
TRANSPORT_MARKERS = (
    "could not reach",
    "connection refused",
    "name resolution",
    "unreachable",
    "network",
    "connect error",
    "nodename",
    "getaddrinfo",
    "transport error",
)

_HTTP_STATUS_RE = re.compile(r"\b(?:http|status|code)\D{0,3}([1-5]\d{2})\b", re.IGNORECASE)
_BARE_STATUS_RE = re.compile(r"\b([1-5]\d{2})\b")
_PROVIDER_CODE_RE = re.compile(r'"(?:code|type|error_code)"\s*:\s*"([a-z0-9_.\-]+)"', re.IGNORECASE)


# --------------------------------------------------------------------------- #
# status extraction
# --------------------------------------------------------------------------- #
def extract_http_status(*sources: Any) -> int | None:
    """Find the HTTP status behind a failure, wherever it ended up.

    The bug this exists to prevent: the adapter formats the status into a string
    (``f"HTTP {code}: {body}"``) and the classifier then looks for an attribute that
    was never set.  So we look in every plausible place, in a strict order:

    1. structured attributes on the exception (``status_code``, ``status``,
       ``response.status_code``, ``context["status"]``);
    2. an explicit ``HTTP 429`` / ``status: 429`` label inside the text;
    3. a bare 3-digit token, but only when it is unambiguous.

    Step 3 is guarded because request IDs, timestamps and prices also contain
    digits; a false 402 would turn a capacity problem into "you are out of money".
    """

    for source in sources:
        code = _status_from_object(source)
        if code is not None:
            return code

    text = " ".join(str(source) for source in sources if source is not None)
    match = _HTTP_STATUS_RE.search(text)
    if match:
        return int(match.group(1))
    bare = _BARE_STATUS_RE.findall(text)
    unique = {int(item) for item in bare}
    if len(unique) == 1:
        return unique.pop()
    return None


def _status_from_object(source: Any) -> int | None:
    if source is None:
        return None
    for attr in ("status_code", "status", "code"):
        value = getattr(source, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        # ``code`` is often a string like "provider_account_unavailable".
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)
    response = getattr(source, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            return code
    context = getattr(source, "context", None)
    if isinstance(context, dict):
        code = context.get("status") or context.get("status_code")
        if isinstance(code, int) and 100 <= code <= 599:
            return code
    return None


def parse_retry_after(value: Any) -> float | None:
    """Interpret a ``Retry-After`` header: delta-seconds or an HTTP-date.

    Both forms are legal (RFC 9110 §10.2.3) and gateways use both.  Returns ``None``
    when the header is missing, malformed, or in the past — callers then fall back
    to exponential backoff rather than guessing.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        from datetime import datetime

        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(0.0, seconds)


def _extract_retry_after(*sources: Any) -> float | None:
    """Pull ``Retry-After`` out of an exception, a response, or a headers mapping."""

    for source in sources:
        if source is None:
            continue
        # A response (``response.headers``), or the headers mapping passed directly.
        # ``httpx.Headers`` is a Mapping but *not* a ``dict``, so this has to be
        # duck-typed — an earlier ``isinstance(source, dict)`` check silently
        # dropped every real Retry-After header.
        headers = getattr(source, "headers", None)
        if headers is None and hasattr(source, "get") and not isinstance(source, (str, bytes)):
            headers = source
        if headers is not None:
            getter = getattr(headers, "get", None)
            value = None
            if callable(getter):
                try:
                    value = getter("retry-after") or getter("Retry-After")
                except Exception:
                    value = None
            parsed = parse_retry_after(value)
            if parsed is not None:
                return parsed
        for attr in ("retry_after", "retry_after_s"):
            value = getattr(source, attr, None)
            parsed = parse_retry_after(value)
            if parsed is not None:
                return parsed
    return None


# --------------------------------------------------------------------------- #
# the classified failure
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Failure:
    """One provider failure, fully interpreted.

    ``message`` is what a human should read; ``detail`` is the raw evidence, kept
    separately and truncated so a 4 KB Chinese JSON body never becomes the UI.
    """

    kind: str = FailureKind.UNKNOWN
    status_code: int | None = None
    retryable: bool = False
    retry_after: float | None = None
    provider_code: str = ""
    message: str = ""
    detail: str = ""
    hint: str = ""
    provider: str = ""
    model: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == FailureKind.OK

    @property
    def transient(self) -> bool:
        return self.kind in FailureKind.TRANSIENT

    @property
    def terminal(self) -> bool:
        return self.kind in FailureKind.TERMINAL

    @property
    def capacity(self) -> bool:
        """True for the "the provider's upstream is empty" family."""

        return self.kind == FailureKind.NO_CAPACITY

    def summary(self, *, limit: int = 160) -> str:
        """One line that explains the failure without leaking the raw body."""

        base = self.message or self.detail or "provider failed"
        if len(base) <= limit:
            return base
        return base[: limit - 1].rstrip() + "…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "provider_code": self.provider_code,
            "message": self.message,
            "hint": self.hint,
            "provider": self.provider,
            "model": self.model,
        }


def classify(
    error: Exception | str | None,
    *,
    status_code: int | None = None,
    headers: Any = None,
    provider: str = "",
    model: str = "",
) -> Failure:
    """Turn any provider failure into a :class:`Failure`.

    Order matters and is the heart of the fix.  The old code checked ``401/403``
    markers before the status code, so a body containing the word "authentication"
    inside a *capacity* error was reported as bad credentials.  We now read the
    status first, then the provider's own machine code, then prose — and the
    capacity family is matched before the generic rate-limit branch so
    ``provider_account_unavailable`` never degrades into "you sent too much".
    """

    if error is None:
        return Failure(kind=FailureKind.OK, message="", provider=provider, model=model)

    detail = str(error)
    code = status_code if status_code is not None else extract_http_status(error, status_code)
    lowered = detail.lower()
    provider_code = _provider_code(detail)
    retry_after = _extract_retry_after(error, headers)
    who = provider or "the provider"

    def build(kind: str, message: str, hint: str, *, retryable: bool | None = None) -> Failure:
        return Failure(
            kind=kind,
            status_code=code,
            retryable=kind in FailureKind.TRANSIENT if retryable is None else retryable,
            retry_after=retry_after if kind in FailureKind.TRANSIENT else None,
            provider_code=provider_code,
            message=message,
            detail=detail[:400],
            hint=hint,
            provider=provider,
            model=model,
        )

    # -- 1. auth / billing: the two states a user must fix ------------------ #
    # Checked first only when the status is unambiguous, so a capacity body that
    # happens to mention a key cannot masquerade as an auth failure.
    if code in {401, 403}:
        return build(
            FailureKind.INVALID_CREDENTIALS,
            f"{who} rejected the credentials (HTTP {code}).",
            "Re-enter the key in `ua setup`, or switch auth mode/provider.",
        )
    if code == 402:
        return build(
            FailureKind.CREDITS_EXHAUSTED,
            f"{who} says the account is out of credit and cannot pay for this request (HTTP 402).",
            "Top up the account, or switch model/provider.",
        )

    # -- 2. capacity: the provider's upstream pool is empty ----------------- #
    # This is NOT rate limiting by quota and NOT bad credentials.  unli.dev sends
    # exactly this as a 429 with code ``provider_account_unavailable``.
    if provider_code in NO_CAPACITY_CODES or _has_marker(lowered, NO_CAPACITY_MARKERS):
        wait = f" Retry in about {retry_after:.0f}s." if retry_after else ""
        return build(
            FailureKind.NO_CAPACITY,
            f"{who} has no upstream capacity right now — this is temporary, "
            "not a problem with your key."
            + wait,
            "Wait for the cooldown to lift, switch model, or switch provider. Retrying immediately "
            "only lengthens the provider's queue.",
        )

    # -- 3. rate limiting by quota ------------------------------------------ #
    if code == 429 or "rate limit" in lowered or "rate-limit" in lowered or "ratelimit" in lowered:
        wait = f" Retry in about {retry_after:.0f}s." if retry_after else ""
        return build(
            FailureKind.RATE_LIMITED,
            f"{who} rate-limited the request (HTTP {code or 429}).{wait}",
            (
                f"Wait about {retry_after:.0f}s and retry, or use a different model."
                if retry_after
                else "Wait a few seconds and retry, or use a different model."
            ),
        )

    # -- 4. auth/billing by prose, only now that status is exhausted -------- #
    if _has_marker(lowered, AUTH_MARKERS):
        return build(
            FailureKind.INVALID_CREDENTIALS,
            f"{who} rejected the credentials.",
            "Re-enter the key in `ua setup`, or check the environment variable it references.",
        )
    if _has_marker(lowered, BILLING_MARKERS):
        return build(
            FailureKind.CREDITS_EXHAUSTED,
            f"{who} says the account is out of credit.",
            "Top up the account, or switch model/provider.",
        )

    # -- 5. shape mismatches and missing things ----------------------------- #
    if code in {400, 404, 405} and _has_marker(lowered, API_SHAPE_MARKERS):
        return build(
            FailureKind.INCOMPATIBLE_API,
            f"{who} does not speak the API shape we sent (HTTP {code}).",
            "Switch compatibility to `anthropic` or `auto` in setup, or check the base URL.",
        )
    if code == 404 or _has_marker(lowered, ("model not found", "does not exist", "no such model")):
        return build(
            FailureKind.NOT_FOUND,
            f"{who} does not know the model {model or '(unset)'!r}.",
            "Run `ua models list` to see what this key can actually call.",
        )
    if _has_marker(lowered, CONTEXT_MARKERS):
        return build(
            FailureKind.CONTEXT_LENGTH,
            f"The conversation exceeds {model or 'the model'}'s context window.",
            "Lower the context budget, or switch to a model with a larger window.",
        )

    # -- 6. transport ------------------------------------------------------- #
    if isinstance(error, TimeoutError) or _has_marker(lowered, TIMEOUT_MARKERS):
        return build(
            FailureKind.TIMEOUT,
            f"{who} timed out.",
            "Increase the request timeout or retry; local and contended endpoints "
            "are often just slow.",
        )
    if _has_marker(lowered, TRANSPORT_MARKERS):
        return build(
            FailureKind.OFFLINE,
            f"{who} could not be reached.",
            "Check the network, proxy settings, and the base URL, then retry.",
        )

    # -- 7. upstream trouble ------------------------------------------------ #
    if code is not None and 500 <= code <= 599:
        return build(
            FailureKind.TEMPORARILY_UNAVAILABLE,
            f"{who} is having trouble upstream (HTTP {code}).",
            "This is on the provider's side; retry shortly or switch provider.",
        )
    if code is not None and code >= 400:
        return build(
            FailureKind.UNKNOWN,
            f"{who} returned HTTP {code}.",
            "Re-run with --debug for the full response.",
        )
    return build(
        FailureKind.UNKNOWN,
        f"{who} failed: {detail[:160]}",
        "Re-run with --debug for the full response.",
    )


def _provider_code(text: str) -> str:
    match = _PROVIDER_CODE_RE.search(text)
    return match.group(1).lower() if match else ""


def _has_marker(lowered: str, markers: tuple[str, ...] | frozenset[str]) -> bool:
    return any(marker in lowered for marker in markers)


# --------------------------------------------------------------------------- #
# retry policy
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RetryPolicy:
    """Bounded, jittered backoff — the only retry shape this product uses.

    ``max_retries`` is a *count of extra attempts*, so the total number of requests
    is ``max_retries + 1``.  Both the per-attempt delay and ``Retry-After`` are
    clamped, because a misconfigured gateway that answers ``Retry-After: 3600``
    must not hang a session for an hour.
    """

    max_retries: int = 3
    base_delay: float = 0.8
    factor: float = 2.0
    max_delay: float = 20.0
    #: Fraction of the computed delay that is randomised, ``[d*(1-j), d*(1+j)]``.
    #: Zero would synchronise every client against a recovering provider.
    jitter: float = 0.25
    respect_retry_after: bool = True
    max_retry_after: float = 60.0
    #: Kinds we will retry at all.  Terminal failures fail fast by design.
    retryable_kinds: frozenset[str] = FailureKind.TRANSIENT

    def attempts(self) -> int:
        return max(0, self.max_retries) + 1

    def should_retry(self, failure: Failure, *, attempt: int) -> bool:
        """Is another attempt allowed for this failure at this attempt number?"""

        if attempt > self.max_retries:
            return False
        return failure.kind in self.retryable_kinds

    def delay(
        self,
        attempt: int,
        failure: Failure | None = None,
        *,
        rng: Callable[[], float] | None = None,
    ) -> float:
        """Seconds to wait before ``attempt + 1``.  Honours ``Retry-After`` first."""

        retry_after = failure.retry_after if failure is not None else None
        return backoff_delay(attempt, retry_after=retry_after, policy=self, rng=rng)


#: Runtime requests: generous, because a task in flight is worth waiting for.
DEFAULT_POLICY = RetryPolicy()

#: Setup/validation probes: short.  A user staring at a panel must not wait 20s to
#: learn the provider is saturated; better to say so and offer a manual retry.
SETUP_POLICY = RetryPolicy(
    max_retries=1,
    base_delay=0.5,
    max_delay=4.0,
    max_retry_after=10.0,
)


def backoff_delay(
    attempt: int,
    *,
    retry_after: float | None = None,
    policy: RetryPolicy = DEFAULT_POLICY,
    rng: Callable[[], float] | None = None,
) -> float:
    """Capped exponential backoff with jitter, or ``Retry-After`` when the provider sent one.

    ``Retry-After`` wins when present and sane — the provider knows its own queue —
    but is still clamped to ``policy.max_retry_after`` so a hostile or broken header
    cannot stall the app.  Otherwise: ``base * factor**(attempt-1)``, capped at
    ``max_delay``, with ``jitter`` of that delay randomised.
    """

    rand = rng or random.random
    if policy.respect_retry_after and retry_after is not None and retry_after > 0:
        capped = min(retry_after, policy.max_retry_after)
        # Jitter is additive only: Retry-After is a server-mandated minimum, not
        # an estimate clients may shorten. Positive spread still prevents herds.
        spread = capped * policy.jitter
        return max(0.05, capped + rand() * spread)

    index = max(1, attempt) - 1
    exponential = min(policy.max_delay, policy.base_delay * (policy.factor**index))
    spread = exponential * policy.jitter
    return max(0.05, exponential + (rand() * 2 - 1) * spread)


async def retry_async(
    operation: Callable[[], Awaitable[Any]],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    classify_error: Callable[[Exception], Failure] = classify,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    on_retry: Callable[[int, Failure, float], None] | None = None,
    rng: Callable[[], float] | None = None,
) -> Any:
    """Run ``operation`` under ``policy``; return its result or re-raise the last error.

    ``operation`` is a zero-arg coroutine factory so each attempt gets a fresh call.
    ``on_retry`` is the observability hook: the UI uses it to say "retrying in 4s"
    instead of silently freezing, which is the difference between a slow app and a
    dead one.
    """

    waiter = sleep or _async_sleep
    attempt = 0
    last: Exception | None = None
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            failure = classify_error(exc)
            last = exc
            if not policy.should_retry(failure, attempt=attempt):
                raise
            delay = policy.delay(attempt, failure, rng=rng)
            if on_retry is not None:
                try:
                    on_retry(attempt, failure, delay)
                except Exception:
                    log.debug("on_retry hook failed", exc_info=True)
            await waiter(delay)
            if attempt >= policy.attempts():  # pragma: no cover - defensive
                raise last from None


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# --------------------------------------------------------------------------- #
# cooldown
# --------------------------------------------------------------------------- #
class CooldownState:
    """Remembers that a provider told us to go away, and for how long.

    Without this, every new user message re-runs the full retry ladder against a
    provider that already said "no capacity" — which is precisely the hammering
    that keeps the upstream saturated.  Keyed by provider (optionally
    ``provider/model``) so one saturated route does not block the others.
    """

    __slots__ = ("_kind", "_reason", "_until")

    def __init__(self) -> None:
        self._until: dict[str, float] = {}
        self._reason: dict[str, str] = {}
        self._kind: dict[str, str] = {}

    def trip(
        self,
        key: str,
        seconds: float,
        *,
        reason: str = "",
        kind: str = FailureKind.RATE_LIMITED,
    ) -> float:
        """Start (or extend) a cooldown.  Returns the new remaining time."""

        if seconds <= 0:
            return self.remaining(key)
        deadline = time.monotonic() + seconds
        # Never shorten an existing cooldown: the provider's latest word is the
        # longest one it has given us.
        self._until[key] = max(deadline, self._until.get(key, 0.0))
        self._reason[key] = reason
        self._kind[key] = kind
        return self.remaining(key)

    def note(
        self,
        key: str,
        failure: Failure,
        *,
        default_seconds: float = 15.0,
        cap: float = 300.0,
    ) -> float:
        """Trip a cooldown sized from the failure itself."""

        seconds = failure.retry_after if failure.retry_after else default_seconds
        # Capacity outages are the provider's problem and can last a while; a
        # quota 429 is usually shorter.  Both are capped so we never lock a user
        # out of their own tool.
        if failure.kind == FailureKind.NO_CAPACITY:
            seconds = max(seconds, default_seconds)
        return self.trip(
            key, min(cap, max(0.0, seconds)), reason=failure.summary(), kind=failure.kind
        )

    def remaining(self, key: str) -> float:
        deadline = self._until.get(key, 0.0)
        return max(0.0, deadline - time.monotonic())

    def active(self, key: str) -> bool:
        return self.remaining(key) > 0.0

    def reason(self, key: str) -> str:
        return self._reason.get(key, "")

    def kind(self, key: str) -> str:
        return self._kind.get(key, "")

    def clear(self, key: str | None = None) -> None:
        """End a cooldown.  ``None`` clears everything (a provider switch does this)."""

        if key is None:
            self._until.clear()
            self._reason.clear()
            self._kind.clear()
            return
        self._until.pop(key, None)
        self._reason.pop(key, None)
        self._kind.pop(key, None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        return {
            key: {
                "remaining_s": round(max(0.0, deadline - now), 2),
                "reason": self._reason.get(key, ""),
                "kind": self._kind.get(key, ""),
            }
            for key, deadline in self._until.items()
        }


#: Process-wide default.  Adapters that do not own one share this, which is what
#: makes the cooldown actually prevent hammering across calls.
cooldown = CooldownState()
