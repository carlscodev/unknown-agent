"""Configuration, paths and secret handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unknown_agent.config.loader import load_settings
from unknown_agent.config.models import Settings
from unknown_agent.config.secrets import REDACTOR, resolve_secret
from unknown_agent.errors import ConfigError


def test_defaults_are_complete_and_typed() -> None:
    settings = Settings()
    assert settings.agent.mode in {"auto", "plan", "react"}
    assert settings.agent.max_steps > 0
    assert settings.validation.is_required("tests", "balanced") is True
    assert settings.validation.is_required("tests", "advisory") is False
    assert settings.validation.is_required("format", "balanced") is False


def test_env_var_secret_is_resolved_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_SECRET", "s3cr3t-value")
    assert resolve_secret("env:DEMO_SECRET") == "s3cr3t-value"
    assert resolve_secret("s3cr3t-value") == "s3cr3t-value"


def test_missing_env_secret_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    with pytest.raises(ConfigError):
        resolve_secret("env:NOPE_NOT_SET")


def test_secrets_are_redacted_in_logs() -> None:
    from unknown_agent.config.secrets import SECRET_MASK

    REDACTOR.register("super-secret-token")
    redacted = REDACTOR.redact("token=super-secret-token")
    assert "super-secret-token" not in redacted
    assert SECRET_MASK in redacted


def test_project_config_is_loaded_and_overrides_defaults(project: Path) -> None:
    config_dir = project / ".unknown"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(
        '[project]\nname = "demo"\nkind = "backend-api"\n\n'
        "[project.commands]\ntest = \"pytest -q\"\n\n"
        "[agent]\nmax_steps = 7\n",
        encoding="utf-8",
    )
    loaded = load_settings(cwd=project)
    assert loaded.settings.agent.max_steps == 7
    assert loaded.settings.project.commands["test"] == "pytest -q"
    assert loaded.project_root == project


def test_cli_overrides_win_and_are_explained(project: Path) -> None:
    loaded = load_settings(cwd=project, overrides={"agent": {"max_steps": 11}}, flag_sources={"agent.max_steps": "--max-steps"})
    assert loaded.settings.agent.max_steps == 11
    assert "--max-steps" in loaded.explain("agent.max_steps")


def test_invalid_project_config_reports_the_field(project: Path) -> None:
    config_dir = project / ".unknown"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text("[agent]\nmax_steps = \"lots\"\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(cwd=project)
    assert "max_steps" in str(excinfo.value)


def test_project_context_detects_python_stack(project: Path) -> None:
    from unknown_agent.context.project import load_project_context

    context = load_project_context(project, Settings())
    assert context.kind
    assert "python" in context.languages
    assert context.commands or context.package_manager


def test_repo_map_lists_source_files(project: Path) -> None:
    from unknown_agent.context.repo_map import build_repo_map

    repo_map = build_repo_map(project, cache_dir=project / ".unknown" / "cache")
    payload = repo_map.to_dict()
    assert payload["total_files"] >= 2
    rendered = json.dumps(payload)
    assert "calc.py" in rendered
    assert "test_calc.py" in rendered
