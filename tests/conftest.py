"""Shared fixtures.

Every test runs against a throwaway project root and a throwaway *user* config
directory (``XDG_*`` pointing into ``tmp_path``), so the suite never depends on
the machine it runs on and never touches the developer's real state.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect all user-level state (config, cache, data) into a temp directory."""

    home = tmp_path_factory.mktemp("home")
    for name, folder in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
    ):
        target = home / folder
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(target))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("UNKNOWN_AGENT_TELEMETRY", "off")
    yield home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal but realistic Python project with a passing test suite."""

    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "demo"
            version = "0.1.0"
            requires-python = ">=3.11"

            [tool.pytest.ini_options]
            testpaths = ["tests"]
            pythonpath = ["src"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    # `.unknown/` marks the project root; several CLI tests write
    # `.unknown/config.toml` directly and must not have to mkdir first.
    (tmp_path / ".unknown").mkdir(exist_ok=True)
    (tmp_path / "src" / "demo" / "calc.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left + right\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from demo.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def failing_project(project: Path) -> Path:
    """The same project, but with a broken test and a broken module."""

    (project / "tests" / "test_calc.py").write_text(
        "from demo.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 4\n",
        encoding="utf-8",
    )
    return project


@pytest.fixture
def web_project(project: Path) -> Path:
    """A project with a static page and a browser flow file."""

    (project / "web").mkdir(exist_ok=True)
    (project / "web" / "index.html").write_text(
        "<!doctype html><title>Demo</title><h1>Demo</h1><div id='out'></div>",
        encoding="utf-8",
    )
    flows = project / ".unknown" / "flows"
    flows.mkdir(parents=True)
    (flows / "smoke.yaml").write_text(
        "name: smoke\nsteps:\n  - action: goto\n    url: /\n  - action: assert_text\n    text: Demo\n",
        encoding="utf-8",
    )
    return project


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, project: Path) -> Iterator[None]:
    """Make the current working directory the test project for CLI invocations."""

    monkeypatch.chdir(project)
    yield


@pytest.fixture
def env_password(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DEMO_SECRET", "s3cr3t-value")
    return "s3cr3t-value"


__all__ = ["REPO_ROOT", "os"]
