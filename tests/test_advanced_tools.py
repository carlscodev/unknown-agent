"""Advanced tool suite + ``ua vision`` CLI tests.

The advanced tools are deterministic by design, so they are tested against
purpose-built fixture repositories - no model, no network, no luck involved.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app
from unknown_agent.config.models import Settings
from unknown_agent.config.paths import resolve_paths
from unknown_agent.observability.events import EventBus
from unknown_agent.tools.base import ToolContext
from unknown_agent.tools.builtin.advanced_tools import build_tools

runner = CliRunner()

TOOLS = {tool.name: tool for tool in build_tools()}


def make_ctx(root: Path, settings: Settings | None = None) -> ToolContext:
    return ToolContext(
        settings=settings or Settings(),
        paths=resolve_paths(root),
        project_root=root,
        events=EventBus(),
    )


def run_tool(tool_name: str, ctx: ToolContext, **kwargs: Any):
    return asyncio.run(TOOLS[tool_name].run(ctx, **kwargs))


# --------------------------------------------------------------------------- #
# architecture_map
# --------------------------------------------------------------------------- #
class TestArchitectureMap:
    def test_import_graph_and_cycle_detection(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src" / "demo"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("from demo import b\n")
        (pkg / "b.py").write_text("from demo import a\n")  # cycle a <-> b
        (pkg / "c.py").write_text("from demo import a\nfrom demo import b\n")

        result = run_tool("architecture_map", make_ctx(tmp_path))
        assert result.ok
        assert result.data["modules"] == 4
        assert result.data["cycles"], "the a<->b cycle must be reported"
        assert "import cycles" in result.output

    def test_non_python_projects_get_a_structure_map(self, tmp_path: Path) -> None:
        (tmp_path / "web").mkdir()
        (tmp_path / "web" / "index.html").write_text("<html></html>")
        result = run_tool("architecture_map", make_ctx(tmp_path))
        assert result.ok and "directory structure" in result.output


# --------------------------------------------------------------------------- #
# dependency_audit
# --------------------------------------------------------------------------- #
class TestDependencyAudit:
    def test_detects_undeclared_and_unused(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                version = "0.1.0"
                dependencies = ["requests>=2.31", "unusedlib>=1.0"]
                """
            )
        )
        (tmp_path / "app.py").write_text("import requests\nimport sneaky_import\n")
        result = run_tool("dependency_audit", make_ctx(tmp_path))
        assert result.ok
        assert "sneaky_import" in result.data["undeclared"]
        assert "unusedlib" in result.data["possibly_unused"]
        assert "requests" not in result.data["possibly_unused"]

    def test_reports_unpinned_specs(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("requests\nflask==3.0\n")
        result = run_tool("dependency_audit", make_ctx(tmp_path))
        assert any("requests" in item for item in result.data["unpinned"])
        assert not any("flask" in item for item in result.data["unpinned"])

    def test_import_name_aliases(self) -> None:
        from unknown_agent.tools.builtin.advanced_tools import DependencyAuditTool

        assert DependencyAuditTool._import_name("PyYAML>=6.0") == "yaml"
        assert DependencyAuditTool._import_name("scikit-learn") == "sklearn"
        assert DependencyAuditTool._import_name("some-pkg>=1") == "some_pkg"


# --------------------------------------------------------------------------- #
# repo_health
# --------------------------------------------------------------------------- #
class TestRepoHealth:
    def test_scores_hygiene_and_flags_secrets(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        (tmp_path / ".gitignore").write_text("*.pyc\n")
        (tmp_path / "leaky.py").write_text('API_KEY = "sk-abcdefghij1234567890abcd"\n# TODO: rotate\n')

        result = run_tool("repo_health", make_ctx(tmp_path))
        assert result.ok
        assert result.data["checks"]["tests"] is True
        assert result.data["checks"]["ci"] is False
        assert result.data["todo_count"] >= 1
        assert "leaky.py" in result.data["secret_suspects"]


# --------------------------------------------------------------------------- #
# app_scaffold
# --------------------------------------------------------------------------- #
class TestAppScaffold:
    @pytest.mark.parametrize("template", ["python-cli", "python-api", "python-lib"])
    def test_templates_produce_valid_projects(self, tmp_path: Path, template: str) -> None:
        import tomllib

        result = run_tool("app_scaffold", make_ctx(tmp_path), template=template, name="My App")
        assert result.ok, result.error
        base = tmp_path / "my-app"
        pyproject = tomllib.loads((base / "pyproject.toml").read_text())
        assert pyproject["project"]["name"] == "my-app"
        # every generated .py must at least compile
        import py_compile

        for path in base.rglob("*.py"):
            py_compile.compile(str(path), doraise=True)

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path)
        assert run_tool("app_scaffold", ctx, template="python-lib", name="dup").ok
        second = run_tool("app_scaffold", ctx, template="python-lib", name="dup")
        assert not second.ok and "refusing to overwrite" in second.error

    def test_unknown_template_is_rejected(self, tmp_path: Path) -> None:
        result = run_tool("app_scaffold", make_ctx(tmp_path), template="cobol-mainframe", name="x")
        assert not result.ok and "unknown template" in result.error


# --------------------------------------------------------------------------- #
# plan_review
# --------------------------------------------------------------------------- #
class TestPlanReview:
    def test_vague_unverified_plans_are_rejected(self, tmp_path: Path) -> None:
        result = run_tool(
            "plan_review", make_ctx(tmp_path),
            plan="1. improve stuff\n2. handle the database migration",
        )
        assert result.data["verdict"] == "reject and re-plan"
        assert any("vague" in f for f in result.data["findings"])
        assert any("verification" in f for f in result.data["findings"])
        assert any("rollback" in f for f in result.data["findings"])

    def test_sound_plans_pass(self, tmp_path: Path) -> None:
        result = run_tool(
            "plan_review", make_ctx(tmp_path),
            plan="1. add the endpoint to routes.py\n2. write a test for the 404 case\n3. run pytest to verify",
        )
        assert result.data["verdict"] == "sound"
        assert result.data["findings"] == []


# --------------------------------------------------------------------------- #
# doc_query
# --------------------------------------------------------------------------- #
class TestDocQuery:
    def test_finds_relevant_passages(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text(
            "# Demo\n\nGeneric intro.\n\n## Deployment\n\nDeploy with `make deploy` after the tests pass.\n"
        )
        result = run_tool("doc_query", make_ctx(tmp_path), query="how to deploy")
        assert result.ok
        assert result.data["passages"]
        assert "make deploy" in result.data["passages"][0]["text"]

    def test_no_hits_is_a_clean_answer(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Nothing relevant here\n")
        result = run_tool("doc_query", make_ctx(tmp_path), query="kubernetes operators")
        assert result.ok and result.data["passages"] == []


# --------------------------------------------------------------------------- #
# skill_distill
# --------------------------------------------------------------------------- #
class TestSkillDistill:
    def test_writes_an_inert_draft(self, tmp_path: Path) -> None:
        import tomllib

        result = run_tool(
            "skill_distill", make_ctx(tmp_path),
            skill_id="Fix Flaky Tests!", summary="stabilise flaky suites",
            instructions="Always rerun failures in isolation first.",
            gates=["tests"],
        )
        assert result.ok
        draft = Path(result.data["path"])
        assert draft.name == "fix-flaky-tests-.draft.toml" or draft.name.endswith(".draft.toml")
        parsed = tomllib.loads(draft.read_text())
        assert parsed["id"] and parsed["summary"] == "stabilise flaky suites"
        assert parsed["gates"] == ["tests"]
        # the .draft suffix keeps it out of the loader
        assert ".draft" in draft.name


# --------------------------------------------------------------------------- #
# mcp_discover
# --------------------------------------------------------------------------- #
class TestMcpDiscover:
    def test_reports_unconfigured_state_with_suggestions(self, tmp_path: Path) -> None:
        result = run_tool("mcp_discover", make_ctx(tmp_path))
        assert result.ok
        assert "none configured" in result.output
        assert result.data["suggestions"]  # recommended catalog entries


# --------------------------------------------------------------------------- #
# ua vision CLI
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _noop() -> None:
    """Keep module-level fixtures out of the class-based tool tests above."""


def _payload(output: str) -> dict:
    return json.loads(output[output.index("{"):])


class TestVisionCli:
    @pytest.fixture(autouse=True)
    def _cwd(self, cli_env: None) -> None:
        """Vision CLI tests run inside the generated project."""

    def invoke(self, *args: str, expect: int | None = 0) -> str:
        result = runner.invoke(app, list(args))
        if expect is not None:
            assert result.exit_code == expect, f"`ua {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
        return result.output

    def test_group_is_reachable(self) -> None:
        assert "vision" in self.invoke("--help")
        out = self.invoke("vision", "--help")
        for command in ("status", "capabilities", "analyze"):
            assert command in out

    def test_status_json_reports_routing_and_policy(self) -> None:
        data = _payload(self.invoke("vision", "status", "--json"))
        assert data["enabled"] is True
        assert "route" in data and "policy" in data
        assert data["policy"]["allow_remote_urls_in_tools"] is False

    def test_capabilities_json_carries_sources(self) -> None:
        data = _payload(self.invoke("vision", "capabilities", "--json"))
        assert data["profiles"]
        entry = data["profiles"][0]
        assert {"value", "source"} <= set(entry["vision"])

    def test_status_human_output_renders(self) -> None:
        out = self.invoke("vision", "status")
        assert "Vision" in out and "Routing" in out
