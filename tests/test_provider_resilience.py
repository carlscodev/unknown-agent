"""Provider stability: classification, bounded retry, cooldown, and a non-blocking setup.

These tests exist because of one real failure.  A gateway (``api.unli.dev``) answered
HTTP 429 with::

    {"error":{"code":"provider_account_unavailable",
              "message":"所有回退分组均不可用:No provider capacity right now; retry."}}

and the product handled it badly in three separate ways:

* ``/setup`` froze, because the auth-mode panel ran a blocking ``subprocess.run``
  (8 s timeout) on the Textual event loop, and the key dialog disabled every button
  and the input field with no timeout and no cancel;
* the 429 was never recognised, because the classifier looked for the literal
  substring ``"rate limit"`` — which this body does not contain — so the user got the
  raw JSON plus "Re-run with --debug";
* every new message re-sent the same requests into a saturated upstream, because
  nothing remembered the provider had just said it had no capacity.

Each group below pins one of those, against the real code and the real error text.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx
import pytest

from unknown_agent.core.models import Message
from unknown_agent.errors import ProviderAuthError
from unknown_agent.providers import resilience
from unknown_agent.providers.auth import AuthMode
from unknown_agent.providers.base import ModelRequest
from unknown_agent.providers.catalog import ProviderEntry
from unknown_agent.providers.health import (
    KIND_TO_STATUS,
    ProviderStatus,
    classify_provider_error,
    run_provider_check,
)
from unknown_agent.providers.openai_compatible import OpenAICompatibleProvider
from unknown_agent.providers.resilience import (
    CooldownState,
    FailureKind,
    RetryPolicy,
    backoff_delay,
    classify,
    extract_http_status,
    parse_retry_after,
)
from unknown_agent.providers.toolkit import normalise_provider_error

# The exact payload from the bug report.
UNLI_BODY = json.dumps(
    {
        "error": {
            "code": "provider_account_unavailable",
            "message": "所有回退分组均不可用:No provider capacity right now; retry. "
            "(ID: e31d0f2f-e779-4a2b-8c3d-9f1e2a3b4c5d)",
        }
    }
)
UNLI_FLAT = f"HTTP 429: {UNLI_BODY}"


@pytest.fixture(autouse=True)
def _clean_cooldown() -> None:
    """Cooldowns are process-global; no test may inherit another's."""

    resilience.cooldown.clear()
    yield
    resilience.cooldown.clear()


# --------------------------------------------------------------------------- #
# 1. classification — provider_account_unavailable is a capacity problem
# --------------------------------------------------------------------------- #
def test_unli_capacity_error_is_not_mistaken_for_a_rate_limit_or_bad_key() -> None:
    failure = classify(Exception(UNLI_FLAT), provider="unli", model="auto")

    # The status was flattened into the text; it must still be recovered.
    assert failure.status_code == 429
    assert failure.provider_code == "provider_account_unavailable"
    assert failure.kind == FailureKind.NO_CAPACITY
    assert failure.retryable is True
    # Capacity exhaustion is not "you sent too much".
    assert failure.kind != FailureKind.RATE_LIMITED
    # ...and it is certainly not a bad key.
    assert failure.kind != FailureKind.INVALID_CREDENTIALS


def test_capacity_error_message_never_leaks_the_raw_body() -> None:
    failure = classify(Exception(UNLI_FLAT), provider="unli")

    assert "所有回退分组均不可用" not in failure.summary()
    assert "provider_account_unavailable" not in failure.summary()
    assert "capacity" in failure.summary().lower()
    assert failure.detail, "the raw body is kept as evidence, just not as the message"


def test_the_five_provider_failures_stay_distinguishable() -> None:
    cases = {
        "HTTP 401: invalid api key": FailureKind.INVALID_CREDENTIALS,
        "HTTP 403: forbidden": FailureKind.INVALID_CREDENTIALS,
        "HTTP 402: insufficient credit": FailureKind.CREDITS_EXHAUSTED,
        "HTTP 429: rate limit exceeded": FailureKind.RATE_LIMITED,
        UNLI_FLAT: FailureKind.NO_CAPACITY,
        "HTTP 503: upstream busy": FailureKind.TEMPORARILY_UNAVAILABLE,
        "HTTP 404: model not found: gpt-9": FailureKind.NOT_FOUND,
        "maximum context length is 8192 tokens": FailureKind.CONTEXT_LENGTH,
        "Could not reach unli: ConnectError: connection refused": FailureKind.OFFLINE,
    }
    for text, expected in cases.items():
        assert classify(Exception(text), provider="unli").kind == expected, text


def test_a_capacity_body_mentioning_a_key_is_still_not_an_auth_failure() -> None:
    """The old classifier matched the word "authentication" before the status."""

    text = (
        'HTTP 429: {"code":"provider_account_unavailable",'
        '"message":"authentication pool has no capacity"}'
    )
    failure = classify(Exception(text), provider="unli")
    assert failure.kind == FailureKind.NO_CAPACITY


def test_status_is_recovered_from_a_flattened_message() -> None:
    assert extract_http_status(Exception("HTTP 429: nope")) == 429
    assert extract_http_status(Exception("status: 503")) == 503
    assert extract_http_status(Exception("nothing numeric here")) is None


def test_status_scan_does_not_invent_a_billing_failure_from_an_id() -> None:
    """A request id containing 402 must not read as 'you are out of money'."""

    text = "HTTP 503: upstream busy (ID: 402e-abc-4021)"
    failure = classify(Exception(text), provider="unli")
    assert failure.status_code == 503
    assert failure.kind == FailureKind.TEMPORARILY_UNAVAILABLE


def test_health_vocabulary_maps_capacity_to_a_non_blocking_state() -> None:
    status = classify_provider_error(Exception(UNLI_FLAT))

    assert status == ProviderStatus.TEMPORARILY_UNAVAILABLE
    assert KIND_TO_STATUS[FailureKind.NO_CAPACITY] == ProviderStatus.TEMPORARILY_UNAVAILABLE
    # The shell must stay usable during a provider outage.
    assert status not in ProviderStatus.BLOCKING
    assert "switch provider" in resilience_failure_remedies(status)


def resilience_failure_remedies(status: str) -> tuple[str, ...]:
    from unknown_agent.providers.health import REMEDIES

    return REMEDIES.get(status, ())


# --------------------------------------------------------------------------- #
# 2. retry policy — Retry-After first, then capped exponential backoff with jitter
# --------------------------------------------------------------------------- #
def test_retry_after_is_parsed_in_both_legal_forms() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    # An HTTP-date in the future is a positive number of seconds.
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    parsed = parse_retry_after(future)
    assert parsed is not None and 25 < parsed <= 31


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(base_delay=0.8, factor=2.0, max_delay=20.0, jitter=0.0)
    delays = [backoff_delay(attempt, policy=policy) for attempt in (1, 2, 3, 4, 5, 6, 7)]
    assert delays[:4] == pytest.approx([0.8, 1.6, 3.2, 6.4])
    assert all(delay <= 20.0 for delay in delays), "the cap must hold no matter the attempt count"


def test_backoff_is_jittered_so_clients_do_not_return_in_lockstep() -> None:
    policy = RetryPolicy(jitter=0.25)
    samples = {round(backoff_delay(2, policy=policy), 6) for _ in range(25)}
    assert len(samples) > 10, "identical delays mean no jitter was applied"


def test_retry_after_wins_over_exponential_backoff_and_is_clamped() -> None:
    policy = RetryPolicy(base_delay=0.8, jitter=0.0, max_retry_after=60.0)
    assert backoff_delay(1, retry_after=7.0, policy=policy) == pytest.approx(7.0)
    # A hostile or broken header must not stall the app for an hour.
    assert backoff_delay(1, retry_after=3600.0, policy=policy) == pytest.approx(60.0)


def test_retry_after_is_read_from_real_httpx_headers() -> None:
    """``httpx.Headers`` is a Mapping but not a ``dict``; an isinstance check dropped it."""

    headers = httpx.Headers({"retry-after": "9"})
    assert classify(Exception(UNLI_FLAT), headers=headers).retry_after == pytest.approx(9.0)


def test_terminal_failures_are_never_retried() -> None:
    policy = RetryPolicy(max_retries=3)
    assert policy.should_retry(classify(Exception("HTTP 401: bad key")), attempt=1) is False
    assert policy.should_retry(classify(Exception("HTTP 402: no credit")), attempt=1) is False
    assert policy.should_retry(classify(Exception(UNLI_FLAT)), attempt=1) is True
    # The attempt count is capped.
    assert policy.should_retry(classify(Exception(UNLI_FLAT)), attempt=4) is False


# --------------------------------------------------------------------------- #
# 3. cooldown — stop hammering a provider that already said no
# --------------------------------------------------------------------------- #
def test_cooldown_tracks_remaining_time_and_clears() -> None:
    state = CooldownState()
    assert state.active("unli:auto") is False
    state.trip("unli:auto", 5, reason="test")
    assert state.active("unli:auto") is True
    assert 4.0 < state.remaining("unli:auto") <= 5.0
    state.clear("unli:auto")
    assert state.active("unli:auto") is False


def test_a_newer_shorter_cooldown_never_shortens_an_existing_one() -> None:
    state = CooldownState()
    state.trip("unli:auto", 30)
    state.trip("unli:auto", 2)
    assert state.remaining("unli:auto") > 25, "the provider's longest word stands"


def test_cooldown_is_capped_so_a_user_is_never_locked_out() -> None:
    state = CooldownState()
    state.note("unli:auto", classify(Exception(UNLI_FLAT)), default_seconds=15.0, cap=300.0)
    assert 0 < state.remaining("unli:auto") <= 300.0


# --------------------------------------------------------------------------- #
# 4. the adapter, against a provider that behaves exactly like the bug report
# --------------------------------------------------------------------------- #
def _provider(handler, *, max_retries: int = 3) -> tuple[OpenAICompatibleProvider, dict[str, Any]]:
    provider = OpenAICompatibleProvider(
        model="auto", base_url="https://api.unli.dev/v1", api_key="sk-test", max_retries=max_retries
    )
    calls: dict[str, Any] = {"n": 0, "times": []}

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["times"].append(time.monotonic())
        return handler(request)

    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(wrapped), base_url=provider.base_url
    )
    return provider, calls


def _request() -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content="hey")])


async def test_capacity_429_retries_a_bounded_number_of_times_with_backoff() -> None:
    provider, calls = _provider(lambda *_: httpx.Response(429, text=UNLI_BODY), max_retries=2)
    response = await provider.complete(_request())
    await provider.aclose()

    assert calls["n"] == 3, "max_retries=2 means exactly 3 requests, never a loop"
    gaps = [calls["times"][i + 1] - calls["times"][i] for i in range(len(calls["times"]) - 1)]
    assert all(gap > 0.3 for gap in gaps), "429s must never be retried immediately"
    assert gaps[1] > gaps[0], "the backoff must grow"
    assert response.error and "capacity" in response.error.lower()
    assert response.raw.get("kind") == FailureKind.NO_CAPACITY


async def test_retry_after_header_is_actually_waited_out() -> None:
    provider, calls = _provider(
        lambda *_: httpx.Response(429, headers={"retry-after": "1"}, text=UNLI_BODY),
        max_retries=1,
    )
    await provider.complete(_request())
    await provider.aclose()

    gap = calls["times"][1] - calls["times"][0]
    assert 0.9 <= gap <= 2.0, f"expected to honour Retry-After: 1, waited {gap:.2f}s"


async def test_a_second_message_does_not_re_hammer_a_saturated_provider() -> None:
    provider, calls = _provider(lambda *_: httpx.Response(429, text=UNLI_BODY), max_retries=1)
    provider.COOLDOWN_FAIL_FAST_S = 0.0  # force the fail-fast branch so we can measure it

    await provider.complete(_request())
    after_first = calls["n"]
    resilience.cooldown.trip(provider.cooldown_key, 30, reason="test")
    second = await provider.complete(_request())
    await provider.aclose()

    assert calls["n"] == after_first, "no further requests may reach a cooling-down provider"
    assert "cooling down" in (second.error or "").lower()


async def test_invalid_credentials_fail_fast_without_retry_or_cooldown() -> None:
    provider, calls = _provider(
        lambda *_: httpx.Response(401, text='{"error":{"message":"invalid api key"}}'),
        max_retries=3,
    )
    with pytest.raises(ProviderAuthError):
        await provider.complete(_request())
    await provider.aclose()

    assert calls["n"] == 1, "a bad key must not be retried"
    assert resilience.cooldown.active(provider.cooldown_key) is False


async def test_a_successful_request_clears_any_stale_cooldown() -> None:
    ok = httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
            ]
        },
    )
    provider, calls = _provider(lambda *_: ok)
    # Short enough to sit under the fail-fast threshold, so the request still goes
    # out; the point of this test is that success wipes the cooldown.
    assert provider.COOLDOWN_FAIL_FAST_S > 5
    resilience.cooldown.trip(provider.cooldown_key, 5, reason="stale")

    response = await provider.complete(_request())
    await provider.aclose()

    assert calls["n"] == 1
    assert response.content == "hi"
    assert resilience.cooldown.active(provider.cooldown_key) is False


async def test_a_long_cooldown_fails_fast_rather_than_sleeping_inside_a_request() -> None:
    """The user must be told how long to wait, not left staring at a hung call."""

    provider, calls = _provider(
        lambda *_: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    )
    resilience.cooldown.trip(
        provider.cooldown_key, provider.COOLDOWN_FAIL_FAST_S + 20, reason="no capacity"
    )

    response = await provider.complete(_request())
    await provider.aclose()

    assert calls["n"] == 0, "nothing may be sent while the provider is cooling down"
    assert "cooling down" in (response.error or "").lower()
    assert response.raw.get("cooldown_s", 0) > 0


async def test_streaming_retries_the_initial_rejection_then_reports_cleanly() -> None:
    """``stream`` used to have no retry path at all."""

    provider, calls = _provider(lambda *_: httpx.Response(429, text=UNLI_BODY), max_retries=1)
    chunks = [chunk async for chunk in provider.stream(_request())]
    await provider.aclose()

    assert calls["n"] == 2, "the stream must retry reaching the endpoint"
    errors = [chunk.error for chunk in chunks if chunk.error]
    assert errors and "capacity" in errors[0].lower()
    assert "所有回退分组均不可用" not in errors[0]


async def test_streaming_still_delivers_content_when_the_provider_is_healthy() -> None:
    body = (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    provider, _calls = _provider(
        lambda *_: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    )
    chunks = [chunk async for chunk in provider.stream(_request())]
    await provider.aclose()

    assert "".join(chunk.delta for chunk in chunks if chunk.delta) == "Hello"
    assert not [chunk for chunk in chunks if chunk.error]


# --------------------------------------------------------------------------- #
# 5. the agent-loop message the user actually reads
# --------------------------------------------------------------------------- #
def test_agent_loop_error_is_explained_instead_of_dumping_the_body() -> None:
    error = normalise_provider_error(Exception(UNLI_FLAT), provider="unli", model="auto")

    assert "所有回退分组均不可用" not in error.message
    assert "Re-run with --debug" not in (error.hint or ""), "the old useless hint must be gone"
    assert "capacity" in error.message.lower()
    assert error.context.get("kind") == FailureKind.NO_CAPACITY
    assert error.context.get("status") == 429


def test_agent_loop_error_still_covers_the_classic_failures() -> None:
    cases = {
        "HTTP 401: invalid api key": "credentials",
        "HTTP 402: insufficient credits": "credit",
        "HTTP 429: rate limit exceeded": "rate-limited",
        "model not found: gpt-9": "does not know the model",
        "maximum context length is 8192 tokens": "context window",
    }
    for raw, needle in cases.items():
        error = normalise_provider_error(Exception(raw), provider="openrouter", model="gpt")
        message = error.message
        assert needle.lower() in message.lower(), f"{raw!r} -> {message!r}"


# --------------------------------------------------------------------------- #
# 6. /setup must never freeze — the regression that started all of this
# --------------------------------------------------------------------------- #
def _offline_entry() -> ProviderEntry:
    """A provider entry that needs no network for the model-listing stage."""

    return ProviderEntry(
        key="probe",
        label="Probe",
        protocol="openai",
        base_url="http://127.0.0.1:1/v1",
        env_var="PROBE_API_KEY",
        docs_url="",
        api_key_required=False,
        model_listing="none",
        known_models=("probe-model",),
    )


async def test_subscription_session_probe_does_not_block_the_event_loop() -> None:
    """The freeze: ``cli_session_state`` shells out for up to 8 s on the UI loop.

    A heartbeat task proves the loop stayed alive while the subprocess ran.  Before
    the fix this assertion failed outright, because ``run_provider_check`` called the
    blocking helper directly from an ``async def``.
    """

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    mode = AuthMode(
        key="subscription-probe",
        label="Subscription",
        kind="subscription",
        summary="probe",
        cli_command="sleep",  # real, on PATH, and slow — exactly the shape of `codex`
        cli_status_args=("1",),
    )
    task = asyncio.create_task(heartbeat())
    try:
        check = run_provider_check(_offline_entry(), mode, env={})
        report = await asyncio.wait_for(check, timeout=20)
    finally:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task

    assert ticks > 15, f"event loop blocked during the probe ({ticks} ticks in ~1s)"
    assert report.stages, "the check still produced a real report"


def test_auth_mode_screen_describes_without_calling_the_blocking_probe() -> None:
    """Guard the structure, not just the timing: no subprocess on the render path."""

    import inspect

    from unknown_agent.tui import quicksetup

    source = inspect.getsource(quicksetup.AuthModeScreen._describe)
    # _describe runs on the UI thread and must not shell out.
    assert "cli_session_state(" not in source

    probe_source = inspect.getsource(quicksetup.AuthModeScreen._probe_session)
    # The probe must be offloaded to a worker thread.
    assert "asyncio.to_thread" in probe_source


def test_key_dialog_has_an_escape_hatch() -> None:
    """A disabled input with no timeout and no cancel is what made setup unkillable."""

    import inspect

    from unknown_agent.tui import quicksetup

    source = inspect.getsource(quicksetup.ApiKeyScreen)
    assert "_cancel_validation" in source
    assert "asyncio.wait_for" in inspect.getsource(quicksetup.ApiKeyScreen._validate_and_fetch)
    assert "key-cancel" in source
    assert "key-save-anyway" in source, "a capacity failure must still let the user save"


# --------------------------------------------------------------------------- #
# 7. provider state as the UI sees it
# --------------------------------------------------------------------------- #
def test_runtime_error_becomes_recoverable_state_with_a_cooldown() -> None:
    from unknown_agent.tui.provider_status import ProviderStatusController

    controller = ProviderStatusController(settings=None)
    controller.state.provider_label = "unli"
    controller.state.model = "auto"

    state = controller.note_runtime_error(Exception(UNLI_FLAT))

    assert state.status == ProviderStatus.TEMPORARILY_UNAVAILABLE
    assert "所有回退分组均不可用" not in state.message
    assert state.hint, "the user must be told what to do next"
    assert state.cooldown_remaining > 0
    assert state.provider_code == "provider_account_unavailable"
    assert state.can_send, "a capacity outage must not disable the shell"


def test_recovery_clears_the_cooldown_and_the_warning() -> None:
    from unknown_agent.tui.provider_status import ProviderStatusController

    controller = ProviderStatusController(settings=None)
    controller.state.provider_label = "unli"
    controller.state.model = "auto"
    controller.note_runtime_error(Exception(UNLI_FLAT))

    state = controller.note_success()

    assert state.status == ProviderStatus.CONNECTED
    assert state.cooldown_remaining == 0
    assert resilience.cooldown.active(controller._cooldown_key()) is False


# --------------------------------------------------------------------------- #
# 8. the real setup panel, driven headlessly through Textual
# --------------------------------------------------------------------------- #
textual = pytest.importorskip("textual")

from unknown_agent.providers.discovery import ValidationResult  # noqa: E402
from unknown_agent.tui.quicksetup import (  # noqa: E402
    ApiKeyScreen,
    AuthModeScreen,
    ModelScreen,
    SetupApp,
)


class _Runtime:
    """Just enough runtime for the panel to open and save."""

    def __init__(self, tmp_path: Path) -> None:
        from unknown_agent.config.models import Settings

        self._settings = Settings()
        self.path = tmp_path / "config.toml"
        self.non_interactive = False
        self.saved = 0

    def load(self, *, force: bool = False) -> Any:
        settings = self._settings

        class Loaded:
            pass

        Loaded.settings = settings
        return Loaded()

    def save_settings(self, settings: Any) -> Path:
        self._settings = settings
        self.saved += 1
        self.path.write_text("# written by the setup panel\n", encoding="utf-8")
        return self.path


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "credentials.toml"
    monkeypatch.setenv("UNKNOWN_AGENT_CREDENTIALS", str(target))
    return target


async def _to_key_screen(app: SetupApp, pilot: Any) -> None:
    await pilot.pause()
    await pilot.press("enter")  # first provider in the list
    await pilot.pause()
    if isinstance(app.screen, AuthModeScreen):
        await pilot.press("enter")  # first auth mode
        await pilot.pause()
    assert isinstance(app.screen, ApiKeyScreen)


async def test_a_hung_validation_can_be_cancelled_and_releases_the_panel(
    tmp_path: Path, isolated_store: Path
) -> None:
    """The original trap: buttons and input disabled forever, no timeout, no cancel."""

    async def hang(*args: Any, **kwargs: Any) -> ValidationResult:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    import unknown_agent.tui.quicksetup as qs

    app = SetupApp(_Runtime(tmp_path))
    original = qs._safe_validate_credential
    qs._safe_validate_credential = hang
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await _to_key_screen(app, pilot)
            screen = app.screen
            screen.query_one("#key-field").value = "sk-" + "a" * 40
            await pilot.press("enter")
            await pilot.pause()
            assert screen.busy is True
            assert screen.query_one("#key-field").disabled is True
            assert screen.query_one("#key-cancel").disabled is False

            # Escape is the escape hatch: it must cancel, not do nothing.
            await pilot.press("escape")
            await pilot.pause()
            assert screen.busy is False, "escape must abort the in-flight request"
            assert screen.query_one("#key-field").disabled is False
            assert "cancelled" in str(screen.query_one("#key-status").render()).lower()
    finally:
        qs._safe_validate_credential = original


async def test_a_validation_that_never_answers_times_out_with_an_explanation(
    tmp_path: Path, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import unknown_agent.tui.quicksetup as qs

    async def hang(*args: Any, **kwargs: Any) -> ValidationResult:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    monkeypatch.setattr(qs, "_safe_validate_credential", hang)
    monkeypatch.setattr(qs, "SETUP_VALIDATE_TIMEOUT_S", 0.4)

    app = SetupApp(_Runtime(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _to_key_screen(app, pilot)
        screen = app.screen
        screen.query_one("#key-field").value = "sk-" + "a" * 40
        await pilot.press("enter")
        await pilot.pause(0.8)
        await pilot.pause()

        status = str(screen.query_one("#key-status").render()).lower()
        assert screen.busy is False
        assert "did not answer" in status, status
        assert screen.query_one("#key-field").disabled is False


async def test_capacity_failure_offers_to_save_unverified_with_manual_models(
    tmp_path: Path, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable provider must not destroy setup: save + type the model by hand."""

    import unknown_agent.tui.quicksetup as qs

    async def no_capacity(entry: Any, **kwargs: Any) -> ValidationResult:
        return ValidationResult(False, UNLI_FLAT, hint="")

    async def no_models(entry: Any, **kwargs: Any) -> list[Any]:
        raise qs.ProviderProbeError(UNLI_FLAT)

    monkeypatch.setattr(qs, "_safe_validate_credential", no_capacity)
    monkeypatch.setattr(qs, "_safe_fetch_models", no_models)

    app = SetupApp(_Runtime(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await _to_key_screen(app, pilot)
        screen = app.screen
        screen.query_one("#key-field").value = "sk-" + "a" * 40
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause(0.1)
        await pilot.pause()

        # Still in the dialog: the failure is the provider's, not the key's.
        assert isinstance(app.screen, ApiKeyScreen)
        status = str(app.screen.query_one("#key-status").render()).lower()
        assert "capacity" in status, status
        # It must not read as an auth failure — that was the misclassification.
        bad_phrases = (
            "401", "unauthorized", "rejected the credentials",
            "invalid api key", "invalid credentials",
        )
        for bad in bad_phrases:
            assert bad not in status, f"{bad!r} must not appear in a capacity failure: {status}"
        assert "visible" in app.screen.query_one("#key-recovery").classes

        # The recovery path keeps the config and falls back to manual model entry.
        app.screen.query_one("#key-save-anyway").press()
        await pilot.pause()
        await pilot.pause()
        # Saving unverified must continue setup rather than dead-end.
        assert isinstance(app.screen, ModelScreen)
        assert app.models_source == "unverified"


async def test_the_auth_mode_panel_stays_responsive_while_the_session_probe_runs(
    tmp_path: Path, isolated_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The freeze, reproduced through the real UI.

    ``cli_session_state`` blocks for 1.5 s here.  Before the fix that ran on the
    Textual event loop and the panel ignored every keystroke for its duration.
    """

    import unknown_agent.tui.quicksetup as qs

    def blocking_probe(mode: Any) -> tuple[str, str]:
        time.sleep(1.5)
        return "ok", "session is active"

    monkeypatch.setattr(qs, "cli_session_state", blocking_probe)

    app = SetupApp(_Runtime(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        if not isinstance(app.screen, AuthModeScreen):
            pytest.skip("first provider exposes no auth-mode screen")

        listing = app.screen.query_one("#auth-list")
        before = listing.highlighted
        started = time.monotonic()
        await pilot.press("down")
        await pilot.pause(0.05)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"the panel was unresponsive for {elapsed:.2f}s during the probe"
        if before is not None and before + 1 < len(app.screen.modes):
            # Arrow keys must still move the selection.
            assert listing.highlighted != before
