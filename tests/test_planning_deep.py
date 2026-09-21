"""Deep planning: signals, axes, questions, synthesis and the ask-UI contract.

These tests assert the *decisions*, not the code paths: that a vague request gets
asked about the things that change the product, that a specific request is left
alone, that a change to an existing repo is not interrogated like a greenfield
build, and that the plan produced is executable by the core loop.
"""

from __future__ import annotations

import pytest

from unknown_agent.planning import (
    AXES,
    AXIS_ORDER,
    SCOPE_PROFILES,
    Answer,
    Blueprint,
    DeepPlanner,
    HeadlessAskUI,
    apply_answer,
    consistency_warnings,
    extract_signals,
    fill_defaults,
    get_axis,
    infer_answers,
    match_preset,
    plan_questions,
    recommend,
    resolve_plan,
    synthesize,
    to_core_plan,
    underspecification_score,
)
from unknown_agent.planning.implications import IMPLICATIONS, missing_implications
from unknown_agent.planning.inference import INFER_FROM, SPECIFIC_NEEDLES, is_specific_needle

VAGUE = "build me a website"
SPECIFIC = (
    "Build a FastAPI service that stores tasks in SQLite, exposes REST endpoints under /tasks, "
    "and deploys to Fly.io. No frontend."
)
CHANGE = "refactor the auth middleware to remove duplication"


def _short_label(label: str) -> str:
    """An approach label without its parenthetical, as the rationale quotes it."""

    return label.split("(")[0].strip()


def _blueprint(goal: str) -> Blueprint:
    """A blueprint carrying everything the request text already answered."""

    blueprint = Blueprint(goal=goal)
    signals = extract_signals(goal)
    blueprint.answers.update(infer_answers(goal, signals=signals))
    return blueprint


def _answered(goal: str) -> Blueprint:
    """A fully answered blueprint: inferred, then asked headlessly, then filled."""

    blueprint = _blueprint(goal)
    signals = extract_signals(goal)
    plan = plan_questions(goal, blueprint=blueprint, signals=signals, mode="auto")
    resolved = resolve_plan(plan, blueprint, HeadlessAskUI(), confirm=False)
    return fill_defaults(resolved, signals=signals)


# --------------------------------------------------------------------------- #
# the catalogue must be complete enough to choose from
# --------------------------------------------------------------------------- #
def test_axes_are_well_formed() -> None:
    assert len(AXIS_ORDER) == 21
    for axis_id in AXIS_ORDER:
        axis = get_axis(axis_id)
        assert axis.id == axis_id
        assert axis.question.endswith("?")
        assert axis.options, f"{axis_id} has no options"
        assert axis.impact >= 1
        assert 0.0 <= axis.ambiguity <= 1.0
        ids = [option.id for option in axis.options]
        assert len(ids) == len(set(ids)), f"{axis_id} has duplicate option ids"
        for option in axis.options:
            assert option.id and option.label
            # The authoring rule: an option the user cannot weigh is not an option.
            assert option.implications, f"{axis_id}.{option.id} states no consequences"
            assert 0 <= option.effort <= 5
            assert 0 <= option.risk <= 5
            for related, values in list(axis.applies_when.items()) + list(axis.skip_when.items()):
                assert related in AXES, f"{axis_id}.{related} is not an axis"
                for value in values:
                    assert AXES[related].option(value) is not None, f"{axis_id}: {related}={value} does not exist"
            for related, values in option.requires.items():
                assert related in AXES
                for value in values:
                    assert AXES[related].option(value) is not None


def test_no_option_is_missing_a_consequence() -> None:
    assert missing_implications(AXES) == []


def test_implication_copy_is_specific_not_decorative() -> None:
    """Guard against the consequence table degenerating into restated labels."""

    for axis_id, table in IMPLICATIONS.items():
        axis = AXES[axis_id]
        for option_id, lines in table.items():
            option = axis.option(option_id)
            assert option is not None, f"{axis_id}.{option_id} is not an option"
            for line in lines:
                assert len(line) > 12, f"{axis_id}.{option_id}: {line!r} says nothing"
                assert line.lower() != option.label.lower(), f"{axis_id}.{option_id} restates its label"


def test_inference_vocabulary_points_at_real_options() -> None:
    for axis_id, table in INFER_FROM.items():
        axis = AXES[axis_id]
        for option_id in table:
            assert axis.option(option_id) is not None, f"{axis_id}.{option_id} is not an option"


def test_every_axis_recommends_something_with_a_reason() -> None:
    for axis_id in AXIS_ORDER:
        recommendation = recommend(get_axis(axis_id))
        assert recommendation.option_id
        assert recommendation.rationale
        assert 0.0 <= recommendation.confidence <= 1.0


def test_recommendation_moves_with_evidence() -> None:
    axis = get_axis("data_strategy")
    plain = recommend(axis)
    informed = recommend(axis, signals=extract_signals("store everything in postgres with proper joins"))
    assert informed.option_id == "sql"
    assert informed.confidence >= plain.confidence


def test_explicit_technology_beats_the_preset() -> None:
    """A named technology is a decision; a preset is a guess."""

    goal = "build me a marketing site with a blog, using Rust and Actix"
    blueprint = _blueprint(goal)
    plan = plan_questions(goal, blueprint=blueprint, mode="always", max_questions=10)
    resolved = resolve_plan(plan, blueprint, HeadlessAskUI(), confirm=False)
    tech = resolved.answers.get("tech_preference")
    assert tech is not None and tech.value == "rust"


# --------------------------------------------------------------------------- #
# signals: read the request before asking anything
# --------------------------------------------------------------------------- #
def test_signals_read_a_specific_request() -> None:
    signals = extract_signals(SPECIFIC)
    assert "fastapi" in signals.tech
    assert "sqlite" in signals.tech
    assert signals.preset == "api-service"
    assert signals.hits["app_type"][0][0] == "api"


def test_a_single_generic_word_does_not_answer_a_high_impact_axis() -> None:
    """"api" is evidence of what it is, not of how it is stored or secured."""

    answers = infer_answers("an internal api")
    assert answers.get("app_type") is not None
    assert "data_strategy" not in answers
    assert "auth_strategy" not in answers


def test_a_specific_mention_does_answer_the_axis_it_names() -> None:
    answers = infer_answers("use postgres with saml sso for enterprise customers")
    assert answers["data_strategy"].value == "sql"
    assert answers["auth_strategy"].value == "sso"
    assert answers["audience"].value == "enterprise"
    for answer in answers.values():
        assert answer.source == "inferred"
        # Every inference must be able to point at the phrase it came from.
        assert "from your wording" in answer.rationale


def test_inference_does_not_fire_on_substrings_of_unrelated_words() -> None:
    """"exposes" contains "ses"; "latest" contains "test". Neither is evidence."""

    answers = infer_answers("expose REST endpoints and show the latest test results")
    assert answers.get("integrations") is None or answers["integrations"].value != "email"


def test_specific_needles_are_curated() -> None:
    assert is_specific_needle("postgres")
    assert not is_specific_needle("api")
    assert not is_specific_needle("fast")
    # Every specific needle must appear in some axis's vocabulary, otherwise the
    # list drifts away from the data it scores.
    used = {needle for table in INFER_FROM.values() for needles in table.values() for needle in needles}
    used |= {needle for axis_id in AXIS_ORDER for needles in AXES[axis_id].infer_from.values() for needle in needles}
    unused = sorted(needle for needle in SPECIFIC_NEEDLES if needle not in used)
    assert not unused, f"specific needles that match nothing: {unused}"


def test_underspecification_separates_vague_from_specific() -> None:
    vague = underspecification_score(VAGUE)
    specific = underspecification_score(SPECIFIC)
    assert vague["score"] > specific["score"]
    assert vague["score"] >= 0.9
    assert vague["recommend_clarify"] is True
    assert specific["score"] < 0.55
    assert specific["scope"] == "greenfield"


def test_a_small_change_to_an_existing_repo_is_not_interrogated() -> None:
    """The module's own promise: a one-line change scores low."""

    scored = underspecification_score("add a healthcheck endpoint to the FastAPI app")
    assert scored["scope"] == "change"
    assert scored["level"] == "low"
    assert scored["recommend_clarify"] is False
    assert plan_questions("add a healthcheck endpoint to the FastAPI app", mode="auto").ask == []


def test_preset_matches_a_web_request_and_an_api_request_differently() -> None:
    web = match_preset(extract_signals("build me a marketing site with a blog and a contact form"))
    api = match_preset(extract_signals("add REST endpoints for the orders service"))
    assert web is not None and api is not None
    assert web.key != api.key


# --------------------------------------------------------------------------- #
# question planning
# --------------------------------------------------------------------------- #
def test_vague_request_asks_the_high_impact_questions_in_order() -> None:
    plan = plan_questions(VAGUE, mode="always", max_questions=6)
    assert plan.ask, "a vague request must produce questions"
    assert len(plan.ask) <= 6
    priorities = [question.priority for question in plan.ask]
    assert priorities == sorted(priorities, reverse=True), "questions must be ordered by consequence"
    asked = plan.axis_ids()
    assert "app_type" in asked
    assert "problem" in asked


def test_specific_request_is_left_alone() -> None:
    plan = plan_questions(SPECIFIC, blueprint=_blueprint(SPECIFIC), mode="auto", max_questions=6)
    asked = set(plan.axis_ids())
    for axis_id in ("tech_preference", "deployment", "data_strategy", "platform"):
        assert axis_id not in asked, f"{axis_id} was stated in the request"
    assert len(plan.ask) <= 3
    # Silence must still be explained.
    assert plan.declined
    for _axis_id, reason in plan.declined:
        assert reason


def test_a_modification_is_only_asked_about_proof_and_size() -> None:
    plan = plan_questions(CHANGE, mode="auto", max_questions=6)
    assert plan.scope == "change"
    assert plan.change_kind == "modification"
    assert set(plan.axis_ids()) <= {"scope_level", "testing_bar"}
    declined = {axis_id for axis_id, _ in plan.declined}
    # The repository already decided these; asking would be noise.
    assert {"design_direction", "brand_color", "backend_style", "monetization", "audience"} <= declined


def test_an_extension_may_be_asked_about_what_it_adds() -> None:
    extension = plan_questions("add a payments page with subscriptions", mode="always", max_questions=21)
    assert extension.change_kind == "extension"
    relevance = plan_questions("add a payments page with subscriptions", mode="auto", max_questions=21)
    assert relevance.scope == "change"


def test_clarify_off_never_asks() -> None:
    assert plan_questions(VAGUE, mode="off").ask == []


def test_max_questions_is_a_hard_ceiling_in_every_mode() -> None:
    for mode in ("auto", "always"):
        for ceiling in (0, 1, 3, 6):
            plan = plan_questions(VAGUE, mode=mode, max_questions=ceiling)
            assert len(plan.ask) <= ceiling, f"{mode} exceeded max_questions={ceiling}"


def test_forced_axes_are_asked_even_when_the_mode_would_skip_them() -> None:
    plan = plan_questions("add a healthcheck endpoint", mode="auto", force_axes=("testing_bar",))
    assert "testing_bar" in plan.axis_ids()


def test_inapplicable_axes_are_skipped_not_asked() -> None:
    """An API has no design direction; a static site has no auth strategy."""

    api_plan = plan_questions("build a rest api for the orders service", mode="always", max_questions=21)
    assert "design_direction" not in api_plan.axis_ids()
    assert "design_direction" in api_plan.skipped
    static_plan = plan_questions("build a static marketing site", mode="always", max_questions=21)
    assert "auth_strategy" not in static_plan.axis_ids()


def test_answered_axes_are_not_asked_again() -> None:
    blueprint = _blueprint(VAGUE)
    blueprint.answers["app_type"] = Answer(axis="app_type", value="cli-tool", label="CLI", source="user")
    plan = plan_questions(VAGUE, blueprint=blueprint, mode="always", max_questions=10)
    assert "app_type" not in plan.axis_ids()
    assert any(question.axis.id == "app_type" for question in plan.auto)


def test_custom_answers_keep_the_users_words() -> None:
    plan = plan_questions(VAGUE, mode="always")
    question = next(item for item in plan.ask if item.axis.allow_custom)
    blueprint = Blueprint(goal=VAGUE)
    text = "a terminal-based inventory tool for a warehouse floor"
    resolved = resolve_plan(plan, blueprint, HeadlessAskUI(overrides={question.axis.id: text}), confirm=False)
    answer = resolved.answers[question.axis.id]
    # An override handed to the headless UI is a flag, and is recorded as one.
    assert answer.source == "flag"
    # Free text is resolved to the nearest option for scoring, but the user's own
    # words are kept — that is what the agent plans against.
    assert answer.value
    assert "warehouse" in (answer.detail or "")


def test_apply_answer_reports_what_it_changed() -> None:
    blueprint = Blueprint(goal=VAGUE)
    axis = get_axis("app_type")
    changes = apply_answer(blueprint, Answer(axis=axis.id, value="api", label="API", source="user"))
    assert changes
    assert blueprint.answers["app_type"].value == "api"
    # An answer with consequences must apply them: choosing "api" removes the design track.
    assert not get_axis("design_direction").applies(blueprint.answers)


def test_headless_ui_falls_back_to_recommendations() -> None:
    plan = plan_questions(VAGUE, mode="always")
    resolved = resolve_plan(plan, Blueprint(goal=VAGUE), HeadlessAskUI(), confirm=False)
    assert set(resolved.answers) >= set(plan.axis_ids())
    for axis_id in plan.axis_ids():
        assert resolved.answers[axis_id].source in {"default", "user", "preset", "inferred", "model", "flag"}


def test_resolve_plan_records_a_scope() -> None:
    plan = plan_questions(VAGUE, mode="always")
    resolved = fill_defaults(resolve_plan(plan, Blueprint(goal=VAGUE), HeadlessAskUI(), confirm=False))
    assert resolved.scope in SCOPE_PROFILES


def test_scope_is_chosen_from_the_answers_when_no_preset_matched() -> None:
    cases = {
        "a cli tool to rename my photos": "cli-tool",
        "an internal dashboard for the ops team": "internal-tool",
        "add REST endpoints for the orders service": "api-service",
        "throwaway script to scrape a page": "cli-tool",
    }
    for goal, expected in cases.items():
        assert fill_defaults(_blueprint(goal)).scope == expected, goal


def test_consistency_warnings_catch_contradictions() -> None:
    blueprint = Blueprint(goal="a brochure site")
    apply_answer(blueprint, Answer(axis="backend_style", value="static", label="Static", source="user"))
    apply_answer(blueprint, Answer(axis="auth_strategy", value="email-password", label="Email", source="user"))
    assert consistency_warnings(blueprint), "auth on a static backend must be named"


def test_fill_defaults_completes_every_applicable_axis() -> None:
    filled = fill_defaults(Blueprint(goal=VAGUE))
    applicable = [axis_id for axis_id in AXIS_ORDER if AXES[axis_id].applies(filled.answers)]
    assert set(applicable) <= set(filled.answers)
    for answer in filled.answers.values():
        assert answer.source in {"default", "user", "preset", "inferred"}
        assert answer.rationale


# --------------------------------------------------------------------------- #
# synthesis: the actual plan
# --------------------------------------------------------------------------- #
def test_synthesize_produces_a_scored_ranking_with_a_leader() -> None:
    plan = synthesize(_answered(SPECIFIC))
    assert plan.approaches
    # ``approaches`` carries the final order, which may be tie-broken toward the
    # user's answers; either way it must be consistent with ``chosen``.
    assert plan.approaches[0].id == plan.chosen
    assert 0 <= plan.approaches[0].total <= 100
    assert plan.chosen == plan.approaches[0].id
    assert plan.approaches[0].recommended
    assert sum(1 for approach in plan.approaches if approach.recommended) == 1


def test_the_leader_rationale_describes_the_true_runner_up() -> None:
    """This is the ordering bug the tie-break used to have."""

    plan = synthesize(_answered(VAGUE))
    leader = plan.approaches[0]
    runner_up = max(plan.approaches[1:], key=lambda approach: approach.total, default=None)
    if runner_up is None or runner_up.total <= leader.total:
        pytest.skip("no tie-break applied: the leader won outright")
    rationale = leader.rationale
    assert _short_label(runner_up.label) in rationale or runner_up.id in rationale
    assert str(runner_up.total) in rationale


def test_synthesize_emits_milestones_architecture_decisions_and_gates() -> None:
    plan = synthesize(_answered(SPECIFIC))
    assert plan.milestones and plan.milestones[0].id
    assert plan.verification
    assert plan.architecture
    assert plan.decisions
    assert plan.estimate.likely_hours > 0
    assert plan.estimate.low_hours <= plan.estimate.likely_hours <= plan.estimate.high_hours
    for milestone in plan.milestones:
        assert milestone.title
    for item in plan.verification:
        assert str(item).strip()
    for decision in plan.decisions:
        assert decision.topic and decision.decision and decision.rationale


def test_a_ui_project_definition_of_done_includes_a_browser_gate() -> None:
    plan = synthesize(_answered("build me a marketing site with a blog, make it look premium"))
    joined = " ".join(str(item).lower() for item in plan.verification)
    assert "browser" in joined or "viewport" in joined
    assert "browser" in plan.architecture.get("verification", "")


def test_an_api_project_does_not_get_a_browser_gate() -> None:
    plan = synthesize(_answered(SPECIFIC))
    assert "browser" not in plan.architecture.get("verification", "")


def test_payments_bring_webhook_and_idempotency_into_the_plan() -> None:
    plan = synthesize(_answered("build an online store with stripe checkout and subscriptions"))
    joined = " ".join(str(item).lower() for item in plan.verification)
    assert "webhook" in joined or "idempot" in joined


def test_an_accessibility_choice_adds_an_accessibility_gate() -> None:
    blueprint = _answered("build me a marketing site")
    blueprint.answers["accessibility"] = Answer(axis="accessibility", value="aa", label="WCAG AA", source="user")
    plan = synthesize(blueprint)
    joined = " ".join(str(item).lower() for item in plan.verification)
    assert "accessib" in joined or "wcag" in joined or "contrast" in joined


def test_a_tdd_choice_changes_the_order_of_work() -> None:
    blueprint = _answered("build me a marketing site")
    blueprint.answers["testing_bar"] = Answer(axis="testing_bar", value="tdd", label="Test-first", source="user")
    plan = synthesize(blueprint)
    joined = " ".join(f"{milestone.title} {milestone.outcome}".lower() for milestone in plan.milestones)
    assert "test" in joined


def test_to_core_plan_is_executable() -> None:
    tools = ["read_file", "write_file", "shell_run", "test_run", "run_gates", "inspect_project"]
    plan = synthesize(_answered(SPECIFIC))
    core = to_core_plan(plan, tool_names=tools)
    assert core.steps
    for step in core.steps:
        assert step.title
        for tool in step.tools or []:
            assert tool in tools, f"plan references an unknown tool {tool!r}"


async def test_deep_planner_runs_without_a_provider() -> None:
    plan = await DeepPlanner().plan(_answered(SPECIFIC), explore=False)
    assert plan.approaches
    assert plan.chosen
    assert plan.milestones


@pytest.mark.parametrize("goal", [VAGUE, SPECIFIC, CHANGE])
def test_planning_is_deterministic_for_the_same_input(goal: str) -> None:
    first = synthesize(_answered(goal))
    second = synthesize(_answered(goal))
    assert [approach.id for approach in first.approaches] == [approach.id for approach in second.approaches]
    assert [approach.total for approach in first.approaches] == [approach.total for approach in second.approaches]
    assert [milestone.id for milestone in first.milestones] == [milestone.id for milestone in second.milestones]
    assert first.estimate.likely_hours == second.estimate.likely_hours
