"""Config that is *read* must also be config that is *honoured*.

A settings block that nothing consumes is worse than no settings block: it is a
promise the product does not keep.  These tests pin the two seams that used to be
decorative — ``[reasoning]`` (a product ladder that has to become vendor wire
parameters) and ``[appearance]`` (a theme that has to reach the actual consoles) —
plus the rule that a CLI flag beats a config file.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unknown_agent.cli.runtime import Runtime
from unknown_agent.config.loader import load_settings
from unknown_agent.config.models import ReasoningConfig
from unknown_agent.errors import ConfigError
from unknown_agent.reasoning import params_for_settings
from unknown_agent.ui.console import output
from unknown_agent.ui.theme import active_glyph_set, active_palette, apply_theme


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path: Path):
    loaded = load_settings(cwd=tmp_path)
    return loaded.settings


@pytest.fixture(autouse=True)
def restore_ui():
    """Appearance is process-global state; every test leaves it as it found it."""

    key = active_palette().key
    glyphs = active_glyph_set()
    state = (output.mode, output.quiet, output.verbose, output.unicode, output.animations, output.show_timings)
    yield
    apply_theme(key, glyphs=glyphs, rebind_consoles=True)
    output.configure(
        mode=state[0],
        quiet=state[1],
        verbose=state[2],
        unicode_ok=state[3],
        animations=state[4],
        show_timings=state[5],
    )


@pytest.fixture
def configured(tmp_path: Path):
    """A Runtime whose config file the test writes, in an empty project."""

    config = tmp_path / "ua.toml"

    def build(body: str, **flags) -> Runtime:
        config.write_text(body)
        runtime = Runtime(config_file=config, cwd=tmp_path, **flags)
        runtime.load()
        return runtime

    return build


def reasoning_provider(settings, *, protocol: str, model: str, supported: bool | None):
    profile = settings.providers[settings.provider]
    profile.type = protocol
    profile.model = model
    profile.supports_reasoning = supported
    return settings


# --------------------------------------------------------------------------- #
# [reasoning] -> wire parameters
# --------------------------------------------------------------------------- #
def test_each_surface_gets_its_own_configured_rung() -> None:
    config = ReasoningConfig()

    assert config.mode_for_surface("plan") == config.planning_mode
    assert config.mode_for_surface("code") == config.coding_mode
    assert config.mode_for_surface("review") == config.review_mode
    assert config.mode_for_surface("chat") == config.quick_mode
    assert config.mode_for_surface("deep") == config.reasoning_intensive_mode


def test_an_unknown_surface_falls_back_instead_of_raising() -> None:
    config = ReasoningConfig()

    assert config.mode_for_surface("something-new") == config.default_mode
    assert config.mode_for_surface("") == config.default_mode


def test_the_ladder_translates_per_vendor(settings) -> None:
    """One product rung, three different wire dialects — no adapter guesses."""

    cases = {
        ("anthropic", "claude-sonnet-4-5", True): "thinking",
        ("openai-compatible", "gpt-5-mini", True): "reasoning_effort",
        ("ollama", "qwen3:8b", True): "reasoning_tokens",
    }
    for (protocol, model, supported), expected_key in cases.items():
        reasoning_provider(settings, protocol=protocol, model=model, supported=supported)
        params, resolved = params_for_settings(settings, "code")

        assert expected_key in params, f"{protocol} did not receive {expected_key}: {params}"
        assert resolved is not None and not resolved.degraded


def test_a_model_with_no_native_control_degrades_instead_of_lying(settings) -> None:
    reasoning_provider(settings, protocol="openai-compatible", model="gpt-4o-mini", supported=False)

    params, resolved = params_for_settings(settings, "code")

    assert resolved is not None and resolved.degraded, "the product must admit it cannot make this model think harder"
    assert not any(key in params for key in ("reasoning_effort", "thinking", "reasoning_tokens"))


def test_reasoning_resolution_never_breaks_a_run() -> None:
    """Garbage in, empty params out: reasoning is an optimisation, not a dependency."""

    assert params_for_settings(SimpleNamespace(), "plan") == ({}, None)
    assert params_for_settings(SimpleNamespace(reasoning=None, active_provider=None), "plan") == ({}, None)


def test_escalation_raises_the_rung_after_failures(settings) -> None:
    reasoning_provider(settings, protocol="openai-compatible", model="gpt-5-mini", supported=True)
    settings.reasoning.coding_mode = "medium"
    settings.reasoning.auto_escalate = True
    settings.reasoning.escalation_steps = 2

    _calm, calm = params_for_settings(settings, "code")
    _hot, hot = params_for_settings(settings, "code", escalate_by=2)

    assert calm is not None and hot is not None
    assert hot.thinking_tokens > calm.thinking_tokens, "self-heal attempts must buy more thinking, not the same"


def test_escalation_is_off_when_auto_escalate_is_false(settings) -> None:
    reasoning_provider(settings, protocol="openai-compatible", model="gpt-5-mini", supported=True)
    settings.reasoning.auto_escalate = False

    _params, resolved = params_for_settings(settings, "code", escalate_by=3)

    assert resolved is not None
    assert resolved.mode == settings.reasoning.mode_for_surface("code")


def test_the_loop_resolves_reasoning_for_its_request(settings) -> None:
    """The loop is the seam that actually builds ModelRequest; prove it resolves."""

    from unknown_agent.core.loop import AgentLoop

    reasoning_provider(settings, protocol="anthropic", model="claude-sonnet-4-5", supported=True)
    loop = object.__new__(AgentLoop)
    loop.settings = settings
    loop.self_heal_attempts = 0
    loop.last_reasoning = None

    params = loop.reasoning_params("plan")

    assert "thinking" in params, f"the loop sent no reasoning control to Anthropic: {params}"
    assert loop.last_reasoning is not None, "the UI needs the resolution to report what was actually used"


def test_the_loop_picks_the_surface_from_the_run(settings) -> None:
    from unknown_agent.core.loop import AgentLoop

    assert AgentLoop._surface(SimpleNamespace(mode="fix", kind="run")) == "fix"
    assert AgentLoop._surface(SimpleNamespace(mode="auto", kind="plan")) == "plan"
    assert AgentLoop._surface(SimpleNamespace(mode="", kind="")) == "run"
    assert AgentLoop._surface(SimpleNamespace(mode=None, kind=None)) == "run"


def test_the_planner_uses_the_planning_rung(settings) -> None:
    from unknown_agent.core.planner import _reasoning

    reasoning_provider(settings, protocol="openai-compatible", model="gpt-5-mini", supported=True)
    settings.reasoning.planning_mode = "super"
    settings.reasoning.coding_mode = "medium"

    plan_params = _reasoning(settings, "plan")
    code_params = _reasoning(settings, "code")

    assert plan_params and code_params
    assert plan_params["reasoning_effort"] >= code_params["reasoning_effort"] or plan_params != code_params, (
        "planning must not silently inherit the cheap coding rung"
    )


def test_the_runner_side_channels_use_the_quick_rung(settings) -> None:
    from unknown_agent.core.runner import _reasoning_params

    reasoning_provider(settings, protocol="openai-compatible", model="gpt-5-mini", supported=True)
    settings.reasoning.quick_mode = "medium"
    settings.reasoning.planning_mode = "ultra"

    quick, _resolved = params_for_settings(settings, "summary")
    deep, _deep = params_for_settings(settings, "plan")

    assert _reasoning_params(settings, "summary") == quick
    assert quick != deep or settings.reasoning.quick_mode == settings.reasoning.planning_mode


# --------------------------------------------------------------------------- #
# [appearance] -> the real consoles
# --------------------------------------------------------------------------- #
def test_the_configured_theme_is_the_theme_in_force(configured) -> None:
    configured('[appearance]\ntheme = "paper"\n')

    assert active_palette().key == "paper"


def test_a_named_mode_pins_the_auto_theme(configured) -> None:
    configured('[appearance]\ntheme = "auto"\nmode = "light"\n')

    assert active_palette().key in {"paper", "linen", "frost"}, "auto+light must resolve to a light palette"


def test_ascii_glyphs_in_config_disable_unicode_output(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\nglyphs = "ascii"\n')

    assert active_glyph_set() == "ascii"
    assert output.unicode is False


def test_motion_off_disables_animations(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\nmotion = "off"\n')
    assert output.animations is False

    configured('[appearance]\ntheme = "midnight"\nmotion = "reduced"\n')
    assert output.animations is False, "reduced motion must not spin either"

    configured('[appearance]\ntheme = "midnight"\nmotion = "full"\n')
    assert output.animations is True


def test_show_timings_reaches_the_output_manager(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\nshow_timings = false\n')

    assert output.show_timings is False


def test_max_width_caps_the_console_instead_of_forcing_it(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\nmax_width = 72\n')

    assert output.data.width <= 72


def test_the_ascii_flag_beats_the_config(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\nglyphs = "unicode"\n', ascii_only=True)

    assert output.unicode is False, "--ascii is a more recent statement of intent than the config file"


def test_the_no_color_flag_beats_the_config(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\ncolor_depth = "truecolor"\n', force_color=False)

    assert output.data.no_color is True


def test_the_color_flag_forces_colour_even_when_piped(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\n', force_color=True)

    assert output.data.no_color is False


def test_the_no_animations_flag_beats_full_motion(configured) -> None:
    configured('[appearance]\ntheme = "midnight"\nmotion = "full"\n', animations=False)

    assert output.animations is False


def test_appearance_does_not_stick_between_invocations(configured) -> None:
    """A global that remembers the last flag would leak into the next run."""

    configured('[appearance]\ntheme = "midnight"\n', ascii_only=True)
    assert output.unicode is False

    configured('[appearance]\ntheme = "midnight"\n')
    assert output.unicode is True, "a later run without --ascii must get its box drawing back"


def test_a_bad_theme_in_the_config_file_names_the_valid_ones(tmp_path: Path) -> None:
    """The user-facing path: a typo must be a readable error, not a traceback."""

    from unknown_agent.ui.theme import PALETTE_ORDER

    config = tmp_path / "ua.toml"
    config.write_text('[appearance]\ntheme = "not-a-palette"\n')

    with pytest.raises(ConfigError) as excinfo:
        Runtime(config_file=config, cwd=tmp_path).load()

    message = f"{excinfo.value} {getattr(excinfo.value, 'hint', '')}"
    assert "not-a-palette" in message
    assert PALETTE_ORDER[0] in message, "the error must list what is actually available"


def test_a_bad_theme_on_a_duck_typed_settings_is_still_a_config_error() -> None:
    """Embedders pass plain objects; the guard must not depend on pydantic."""

    from unknown_agent.ui.theme import PALETTE_ORDER

    stub = SimpleNamespace(
        appearance=SimpleNamespace(
            theme="not-a-palette",
            mode=None,
            glyphs="unicode",
            color_depth="auto",
            motion="full",
            wants_motion=True,
            show_timings=True,
            max_width=120,
        )
    )

    with pytest.raises(ConfigError) as excinfo:
        Runtime(cwd=Path("."))._apply_appearance(stub)

    message = f"{excinfo.value} {getattr(excinfo.value, 'hint', '')}"
    assert "not-a-palette" in message
    assert PALETTE_ORDER[0] in message
