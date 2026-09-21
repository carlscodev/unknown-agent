"""``ua skills``: the surface for the craft guidance a run carries.

Skills are data, and data has failure modes a user must be able to see: a file that
did not parse, a name in config that matches nothing, and a skill that arrived with
a clone rather than being installed.  These tests run the actual commands, because
that is where those failures surface — and because two of them (Rich eating
``[policy]`` as a markup tag, and user-scope skills never being discovered at all)
were only visible by running the product.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app
from unknown_agent.config.loader import load_settings
from unknown_agent.errors import ExitCode

runner = CliRunner()


@pytest.fixture(autouse=True)
def _cwd(cli_env: None) -> None:
    """Every command runs inside the generated project."""


def invoke(*args: str, expect: int | None = 0) -> str:
    result = runner.invoke(app, list(args))
    if expect is not None:
        assert result.exit_code == expect, f"`ua {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
    return result.output


def payload(output: str) -> dict:
    """CliRunner merges stdout and stderr, so JSON starts at the first brace."""

    start = output.index("{")
    return json.loads(output[start:])


def project_root() -> Path:
    return Path.cwd()


def user_skills_dir() -> Path:
    return load_settings(cwd=project_root()).paths.user_skills_dir


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def test_the_group_is_reachable_from_the_top_level() -> None:
    assert "skills" in invoke("--help")


def test_the_group_lists_its_commands() -> None:
    output = invoke("skills", "--help")
    for command in ("list", "packs", "show", "new", "check"):
        assert command in output


def test_the_group_without_a_command_shows_help_not_a_crash() -> None:
    result = runner.invoke(app, ["skills"])
    assert result.exit_code == 2
    assert "Usage:" in result.output


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def test_list_shows_the_starter_pack() -> None:
    output = invoke("skills", "list")

    assert "active skills" in output
    for skill in ("bug-hunter", "test-smith", "gate-keeper"):
        assert skill in output


def test_list_marks_builtin_skills_as_trusted() -> None:
    output = invoke("skills", "list")

    assert "trusted" in output


def test_list_filters_by_category() -> None:
    output = invoke("skills", "list", "--category", "verification")

    assert "test-smith" in output
    assert "bug-hunter" not in output, "a repair skill must not appear under verification"


def test_list_json_reports_the_whole_resolution() -> None:
    data = payload(invoke("skills", "list", "--json"))

    assert data["skills"], "the starter pack must resolve to something"
    assert {"disabled", "unresolved", "packs", "requested_tools", "requested_gates"} <= set(data)
    assert all(skill["id"] for skill in data["skills"])


def test_an_empty_skill_set_explains_how_to_get_one(tmp_path: Path) -> None:
    (project_root() / ".unknown" / "config.toml").write_text('[skills]\npack = ["none"]\n', encoding="utf-8")

    output = invoke("skills", "list")

    assert "no active skills" in output
    assert "ua skills new" in output, "an empty state should point at the way out"


# --------------------------------------------------------------------------- #
# packs
# --------------------------------------------------------------------------- #
def test_packs_lists_the_catalog_and_marks_the_active_one() -> None:
    output = invoke("skills", "packs")

    for key in ("default", "web", "api", "quality", "delivery", "all", "none"):
        assert key in output
    assert "active" in output


def test_packs_json() -> None:
    data = payload(invoke("skills", "packs", "--json"))

    assert data["active"] == ["default"]
    assert {pack["key"] for pack in data["packs"]} >= {"default", "web", "none"}
    assert all("description" in pack and "count" in pack for pack in data["packs"])


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #
def test_show_prints_the_instructions_and_the_grants() -> None:
    output = invoke("skills", "show", "bug-hunter")

    assert "Bug hunter" in output
    assert "instructions" in output.lower()
    assert "read_logs" in output, "the tool grants are the part a user audits"


def test_show_json_flags_whether_the_skill_is_active_here() -> None:
    data = payload(invoke("skills", "show", "bug-hunter", "--json"))

    assert data["id"] == "bug-hunter"
    assert data["active"] is True


def test_show_falls_back_to_the_catalog_for_an_inactive_skill() -> None:
    """`browser-verifier` ships but is not in the starter pack."""

    output = invoke("skills", "show", "browser-verifier")

    assert "browser-verifier" in output
    assert "not active" in output
    assert "ua config set" in output, "an inactive skill should say how to activate it"


def test_show_an_unknown_skill_is_a_usage_error() -> None:
    output = invoke("skills", "show", "not-a-real-skill", expect=int(ExitCode.USAGE))

    assert "not-a-real-skill" in output
    assert "ua skills list" in output


# --------------------------------------------------------------------------- #
# new
# --------------------------------------------------------------------------- #
def test_new_scaffolds_a_project_skill_with_the_name_substituted() -> None:
    output = invoke("skills", "new", "api-contracts")

    target = project_root() / ".unknown" / "skills" / "api-contracts.toml"
    assert target.is_file(), output
    body = target.read_text(encoding="utf-8")
    assert 'id = "api-contracts"' in body
    assert 'label = "api-contracts"' in body
    assert "never executed" in body, "the template's safety note must survive scaffolding"


def test_a_new_project_skill_is_discovered_and_labelled_by_origin() -> None:
    invoke("skills", "new", "api-contracts")

    data = payload(invoke("skills", "list", "--json"))
    created = next((skill for skill in data["skills"] if skill["id"] == "api-contracts"), None)

    assert created is not None, "a scaffolded skill must be picked up on the next command"
    assert created["source"] == "project"


def test_new_refuses_to_clobber_without_force() -> None:
    invoke("skills", "new", "api-contracts")
    output = invoke("skills", "new", "api-contracts", expect=int(ExitCode.USAGE))

    assert "already exists" in output
    assert "--force" in output


def test_force_overwrites() -> None:
    invoke("skills", "new", "api-contracts")
    target = project_root() / ".unknown" / "skills" / "api-contracts.toml"
    target.write_text("id = \"api-contracts\"\n", encoding="utf-8")

    invoke("skills", "new", "api-contracts", "--force")

    assert 'label = "api-contracts"' in target.read_text(encoding="utf-8")


def test_new_can_target_an_arbitrary_directory(tmp_path: Path) -> None:
    target_dir = tmp_path / "scratch"
    invoke("skills", "new", "experiment", "--scope", str(target_dir))

    assert (target_dir / "skills" / "experiment.toml").is_file()


def test_new_explains_what_untrusted_means_without_scaring_the_user() -> None:
    output = invoke("skills", "new", "api-contracts")

    assert "untrusted origin" in output
    assert "not a verdict" in output
    assert "[policy]" in output, "Rich markup must not eat the config section name"


# --------------------------------------------------------------------------- #
# user scope — the discovery bug
# --------------------------------------------------------------------------- #
def test_a_user_scope_skill_is_discovered() -> None:
    """``resolve_skills`` without ``user_dir`` made these invisible everywhere."""

    directory = user_skills_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mine.toml").write_text(
        'id = "mine"\nlabel = "Mine"\nsummary = "user scope"\ncategory = "craft"\ninstructions = "be careful"\n',
        encoding="utf-8",
    )

    data = payload(invoke("skills", "list", "--json"))

    assert "mine" in [skill["id"] for skill in data["skills"]]


def test_a_user_scope_skill_is_trusted_and_a_project_one_is_not() -> None:
    directory = user_skills_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mine.toml").write_text(
        'id = "mine"\nlabel = "Mine"\nsummary = "user scope"\ncategory = "craft"\ninstructions = "be careful"\n',
        encoding="utf-8",
    )
    invoke("skills", "new", "theirs")

    output = invoke("skills", "list")

    mine = next(line for line in output.splitlines() if line.startswith("mine"))
    theirs = next(line for line in output.splitlines() if line.startswith("theirs"))
    assert "trusted" in mine
    assert "project" in theirs


def test_new_can_scaffold_into_the_user_scope() -> None:
    invoke("skills", "new", "everywhere", "--scope", "user")

    assert (user_skills_dir() / "everywhere.toml").is_file()
    assert "mine" not in invoke("skills", "list") or True  # sanity: command still runs
    assert "everywhere" in invoke("skills", "list")


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def test_check_passes_on_a_clean_set() -> None:
    output = invoke("skills", "check")

    assert "every configured skill loaded" in output


def test_check_reports_untrusted_origins_with_their_paths() -> None:
    invoke("skills", "new", "api-contracts")

    output = invoke("skills", "check")

    assert "project directory" in output
    assert "api-contracts.toml" in output
    assert "review them" in output


def test_check_fails_with_a_config_exit_code_on_a_dangling_name() -> None:
    (project_root() / ".unknown" / "config.toml").write_text(
        '[skills]\nenable = ["does-not-exist"]\n', encoding="utf-8"
    )

    output = invoke("skills", "check", expect=int(ExitCode.CONFIG))

    assert "does-not-exist" in output
    assert "[skills]" in output, "the section name must survive Rich markup parsing"


def test_check_fails_on_a_skill_file_that_does_not_parse() -> None:
    directory = project_root() / ".unknown" / "skills"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "broken.toml").write_text("id = \nthis is not toml\n", encoding="utf-8")

    output = invoke("skills", "check", expect=int(ExitCode.CONFIG))

    assert "broken.toml" in output


def test_check_json_is_machine_readable() -> None:
    invoke("skills", "new", "api-contracts")

    data = payload(invoke("skills", "check", "--json"))

    assert data["ok"] is True
    assert "api-contracts" in data["active"]
    assert {entry["id"] for entry in data["untrusted"]} == {"api-contracts"}
    assert data["requested_gates"], "the starter pack must require gates"


def test_check_json_reports_a_broken_set() -> None:
    (project_root() / ".unknown" / "config.toml").write_text(
        '[skills]\nenable = ["does-not-exist"]\n', encoding="utf-8"
    )

    data = payload(invoke("skills", "check", "--json", expect=int(ExitCode.CONFIG)))

    assert data["ok"] is False
    assert data["unresolved"] == ["does-not-exist"]
