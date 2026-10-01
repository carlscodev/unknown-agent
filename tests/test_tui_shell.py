"""The TUI must open and stay usable whatever the provider is doing.

These tests encode the requirement directly: for every way a provider can fail, the
shell launches, renders, accepts typing, and offers a way out.  If any of them start
failing, the 402 bug has come back.
"""

from __future__ import annotations

import asyncio

import pytest

from unknown_agent.providers.health import CheckStage, ProviderReport, ProviderStatus
from unknown_agent.tui.commands import Command, CommandRegistry, fuzzy_score
from unknown_agent.tui.provider_status import ProviderStatusController, StartupHealthResolver
from unknown_agent.tui.shell_app import ErrorBoundary, TUIAppShell
from unknown_agent.tui.usage import UsageTracker, estimate_tokens


def _plain(renderable) -> str:
    """Render a Rich renderable to plain text so assertions can read it."""

    from rich.console import Console

    console = Console(width=60, file=None, record=True, no_color=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _failing_controller(settings, error: Exception) -> ProviderStatusController:
    class Failing(ProviderStatusController):
        async def _check(self, *, probe_inference: bool = False):  # type: ignore[override]
            raise error

    return Failing(settings)


# --------------------------------------------------------------------------- #
# the launch guarantee
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("HTTP 402: insufficient credits"), ProviderStatus.CREDITS_EXHAUSTED),
        (RuntimeError("401 invalid api key"), ProviderStatus.INVALID_CREDENTIALS),
        (ConnectionError("Temporary failure in name resolution"), ProviderStatus.OFFLINE),
        (TimeoutError("request timed out"), ProviderStatus.OFFLINE),
        (RuntimeError("429 rate limit"), ProviderStatus.RATE_LIMITED),
        (ValueError("an error nobody anticipated"), ProviderStatus.DEGRADED),
    ],
)
async def test_tui_launches_through_every_provider_failure(settings, error, expected) -> None:
    app = TUIAppShell(settings=settings, controller=_failing_controller(settings, error))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause(0.4)
        await pilot.pause()

        assert app.is_running, "the shell must be running despite the provider failure"
        assert app.provider_status.state.status == expected

        # …and it must still accept input.
        await pilot.click("#composer")
        await pilot.press("h", "i")
        assert app.query_one("#composer").value == "hi"


async def test_402_surfaces_a_banner_with_remedies(settings) -> None:
    app = TUIAppShell(
        settings=settings,
        controller=_failing_controller(settings, RuntimeError("HTTP 402 insufficient credits")),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause(0.4)
        alert = app.query_one("#notice")
        assert alert.has_class("visible"), "a 402 must be announced in the UI"
        rendered = str(alert.render())
        assert "credits exhausted" in rendered
        state = app.provider_status.state
        assert "switch provider" in state.remedies and "switch model" in state.remedies


async def test_a_hung_provider_does_not_delay_the_first_frame(settings) -> None:
    """The UI must be interactive while the health check is still outstanding."""

    class Hanging(ProviderStatusController):
        async def _check(self, *, probe_inference: bool = False):  # type: ignore[override]
            await asyncio.sleep(60)

    app = TUIAppShell(settings=settings, controller=Hanging(settings))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.is_running
        await pilot.click("#composer")
        await pilot.press("o", "k")
        assert app.query_one("#composer").value == "ok"
        assert app.provider_status.state.status in {ProviderStatus.VALIDATING, ProviderStatus.RECOVERING}


async def test_unreadable_settings_still_produce_a_shell() -> None:
    """Even with no settings at all the shell opens and says what to do."""

    app = TUIAppShell(settings=None, resolve_on_mount=False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.is_running
        assert app.provider_status.state.status == ProviderStatus.NOT_CONFIGURED
        assert app.provider_status.state.hint


async def test_mount_performs_no_provider_io(settings, monkeypatch) -> None:
    """Structural guarantee: nothing network-shaped is touched before the first frame."""

    called: list[str] = []
    import unknown_agent.providers.discovery as discovery

    async def _boom(*args, **kwargs):
        called.append("fetch")
        raise AssertionError("startup must not contact the provider")

    monkeypatch.setattr(discovery, "fetch_models", _boom)
    monkeypatch.setattr(discovery, "validate_credential", _boom)

    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert called == []
        assert app.is_running


# --------------------------------------------------------------------------- #
# recovery
# --------------------------------------------------------------------------- #
async def test_retry_re_runs_the_health_check(settings) -> None:
    attempts: list[int] = []

    class Counting(ProviderStatusController):
        async def _check(self, *, probe_inference: bool = False):  # type: ignore[override]
            attempts.append(1)
            raise RuntimeError("402 insufficient credits")

    app = TUIAppShell(settings=settings, controller=Counting(settings))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause(0.4)
        first = len(attempts)
        app.action_retry_provider()
        await pilot.pause()
        await pilot.pause(0.4)
        assert len(attempts) > first


async def test_runtime_402_becomes_status_not_a_crash(settings) -> None:
    """A provider that dies mid-run leaves the shell alive and informative."""

    async def exploding_runner(task: str, hooks) -> str:
        raise RuntimeError("HTTP 402: insufficient credits mid-stream")

    app = TUIAppShell(settings=settings, runner=exploding_runner, resolve_on_mount=False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.provider_status._set(status=ProviderStatus.CONNECTED, message="ok")
        await pilot.click("#composer")
        await pilot.press(*"do the thing")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause(0.4)
        assert app.is_running
        assert app.provider_status.state.status == ProviderStatus.CREDITS_EXHAUSTED


async def test_error_boundary_swallows_and_reports(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        with ErrorBoundary(app, region="unit test"):
            raise RuntimeError("402 no credit")
        assert app.is_running
        assert app.provider_status.state.status == ProviderStatus.CREDITS_EXHAUSTED


async def test_startup_resolver_never_raises(settings) -> None:
    controller = _failing_controller(settings, RuntimeError("boom"))
    state = await StartupHealthResolver(controller, delay=0).run()
    assert state is not None
    assert state.status != ProviderStatus.READY


# --------------------------------------------------------------------------- #
# state transitions are observable
# --------------------------------------------------------------------------- #
async def test_state_transitions_are_pushed_to_subscribers(settings) -> None:
    seen: list[str] = []
    controller = ProviderStatusController(settings)
    controller.subscribe(lambda state: seen.append(state.status))
    controller.note_runtime_error(RuntimeError("402 insufficient credits"))
    assert seen[-1] == ProviderStatus.CREDITS_EXHAUSTED


async def test_state_objects_are_replaced_not_mutated(settings) -> None:
    """Reactives compare by identity: a mutated state would never redraw."""

    controller = ProviderStatusController(settings)
    before = controller.state
    controller.note_runtime_error(RuntimeError("offline: connection refused"))
    assert controller.state is not before


async def test_a_broken_listener_does_not_break_health(settings) -> None:
    controller = ProviderStatusController(settings)
    controller.subscribe(lambda state: (_ for _ in ()).throw(RuntimeError("bad widget")))
    controller.note_runtime_error(RuntimeError("402"))
    assert controller.state.status == ProviderStatus.CREDITS_EXHAUSTED


# --------------------------------------------------------------------------- #
# dashboard content
# --------------------------------------------------------------------------- #
async def test_sidebar_shows_usage_and_context(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.usage.set_pricing("m", input_per_mtok=3.0, output_per_mtok=15.0)
        app.usage.set_context_window(200_000)
        app.usage.record(model="m", input_tokens=10_000, output_tokens=2_000)
        app.usage.set_context_tokens(12_000)
        app._refresh_sidebar()
        await pilot.pause()
        # Render through a console: Static.render() returns a wrapper, not text.
        rendered = _plain(app.query_one("#card-usage").render_usage(app.usage.snapshot()))
        assert "10,000" in rendered and "2,000" in rendered
        assert "$" in rendered
        assert "6%" in _plain(app.query_one("#card-context").render_usage(app.usage.snapshot()))


async def test_status_report_lists_every_stage(settings) -> None:
    class Reporting(ProviderStatusController):
        async def _check(self, *, probe_inference: bool = False):  # type: ignore[override]
            report = ProviderReport(provider="openrouter", status=ProviderStatus.CREDITS_EXHAUSTED)
            report.stages = [
                CheckStage("credential", "ok", "accepted"),
                CheckStage("inference", "fail", "HTTP 402"),
            ]
            return report

    app = TUIAppShell(settings=settings, controller=Reporting(settings))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause(0.4)
        app.action_show_status()
        await pilot.pause()
        assert app.provider_status.last_report is not None
        assert len(app.provider_status.last_report.stages) == 2


# --------------------------------------------------------------------------- #
# usage tracking
# --------------------------------------------------------------------------- #
def test_measured_and_estimated_usage_are_distinguished() -> None:
    tracker = UsageTracker()
    tracker.record(model="m", input_tokens=100, output_tokens=50, measured=True)
    assert tracker.snapshot().fully_measured is True
    tracker.record_estimated(model="m", prompt="hello there", completion="general kenobi")
    assert tracker.snapshot().fully_measured is False
    assert tracker.snapshot().tokens_text().startswith("~")


def test_cost_is_computed_from_published_prices() -> None:
    tracker = UsageTracker()
    tracker.set_pricing("m", input_per_mtok=3.0, output_per_mtok=15.0)
    tracker.record(model="m", input_tokens=1_000_000, output_tokens=1_000_000)
    assert tracker.snapshot().cost_usd == pytest.approx(18.0)


def test_unpriced_models_are_labelled_not_reported_as_free() -> None:
    """Showing $0.00 for an unpriced model would be a lie."""

    tracker = UsageTracker()
    tracker.record(model="mystery", input_tokens=500, output_tokens=500)
    snapshot = tracker.snapshot()
    assert snapshot.fully_priced is False
    assert snapshot.cost_text() == "unpriced"


def test_usage_parses_openai_and_anthropic_payloads() -> None:
    tracker = UsageTracker()
    tracker.record_from_payload({"prompt_tokens": 10, "completion_tokens": 5}, model="m")
    tracker.record_from_payload({"input_tokens": 7, "output_tokens": 3}, model="m")
    snapshot = tracker.snapshot()
    assert snapshot.input_tokens == 17 and snapshot.output_tokens == 8
    assert snapshot.fully_measured is True


def test_per_model_breakdown() -> None:
    tracker = UsageTracker()
    tracker.record(model="a", input_tokens=100, output_tokens=10)
    tracker.record(model="b", input_tokens=5, output_tokens=5)
    models = {item.model: item for item in tracker.snapshot().by_model}
    assert models["a"].total_tokens == 110 and models["b"].total_tokens == 10


def test_context_percentage() -> None:
    tracker = UsageTracker(context_window=1000)
    tracker.set_context_tokens(250)
    assert tracker.snapshot().context_pct == pytest.approx(25.0)


def test_estimate_tokens_is_monotonic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello") < estimate_tokens("hello world, this is longer")


# --------------------------------------------------------------------------- #
# command palette
# --------------------------------------------------------------------------- #
def test_fuzzy_score_ranks_prefixes_first() -> None:
    assert fuzzy_score("mod", "switch model") > 0
    assert fuzzy_score("model", "switch model") > fuzzy_score("mdl", "switch model")
    assert fuzzy_score("zzz", "switch model") == 0


def test_registry_search_and_slash_lookup() -> None:
    registry = CommandRegistry(
        [
            Command("model", "Switch model", slash="model"),
            Command("provider", "Switch provider", slash="provider"),
        ]
    )
    assert registry.search("prov")[0].key == "provider"
    assert registry.by_slash("/model").key == "model"
    assert registry.by_slash("nope") is None


async def test_shell_registers_the_expected_commands(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    keys = {command.key for command in app.commands.all()}
    for expected in ("model", "provider", "mode", "setup", "retry", "status", "usage", "help", "quit"):
        assert expected in keys


async def test_unknown_slash_command_is_reported_not_fatal(settings) -> None:
    app = TUIAppShell(settings=settings, resolve_on_mount=False)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press(*"/nonsense")
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running
