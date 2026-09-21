"""MCP: models, selection, bridge semantics, manager policy, CLI management.

The live-transport tests spawn a real stdio server (the installed SDK serving a
tiny probe app) because the transport layer is exactly the part unit stubs
cannot vouch for.  Everything else runs offline.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app
from unknown_agent.config.models import McpServerConfig, Settings
from unknown_agent.core.models import RiskLevel
from unknown_agent.mcp import mcp_available
from unknown_agent.mcp.bridge import McpProxyTool, build_proxies, proxy_name, risk_for
from unknown_agent.mcp.catalog import MCP_CATALOG, catalog_entry, config_from_entry
from unknown_agent.mcp.manager import McpManager, select_tools
from unknown_agent.mcp.models import McpToolInfo

runner = CliRunner()

HAS_MCP = mcp_available()[0]

PROBE_SERVER = textwrap.dedent(
    '''
    from mcp.server.mcpserver import MCPServer

    srv = MCPServer("probe-server", instructions="A test server.")

    @srv.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @srv.tool()
    def fail(message: str) -> str:
        """Always raises."""
        raise ValueError(message)

    @srv.resource("probe://greeting")
    def greeting() -> str:
        """A greeting resource."""
        return "hello from probe"

    @srv.prompt()
    def review(code: str) -> str:
        """Review some code."""
        return f"Please review: {code}"

    if __name__ == "__main__":
        srv.run()
    '''
)


@pytest.fixture
def probe_settings(tmp_path: Path) -> Settings:
    server = tmp_path / "probe_server.py"
    server.write_text(PROBE_SERVER, encoding="utf-8")
    settings = Settings()
    settings.mcp.servers["probe"] = McpServerConfig(
        transport="stdio", command=sys.executable, args=[str(server)], trust="trusted"
    )
    return settings


# --------------------------------------------------------------------------- #
# config schema
# --------------------------------------------------------------------------- #
def test_stdio_requires_a_command() -> None:
    with pytest.raises(Exception, match="stdio transport requires"):
        McpServerConfig(transport="stdio")


def test_remote_requires_a_url() -> None:
    with pytest.raises(Exception, match="http transport requires"):
        McpServerConfig(transport="http")


def test_settings_carry_an_mcp_section_by_default() -> None:
    settings = Settings()
    assert settings.mcp.enabled is True
    assert settings.mcp.servers == {}
    assert settings.mcp.fail_soft is True


# --------------------------------------------------------------------------- #
# selection (allow/deny/cap)
# --------------------------------------------------------------------------- #
def _tools(*names: str) -> list[McpToolInfo]:
    return [McpToolInfo(name=name) for name in names]


def test_deny_beats_allow() -> None:
    config = McpServerConfig(transport="http", url="https://x", allow_tools=["a", "b"], deny_tools=["b"])
    kept, excluded = select_tools(config, _tools("a", "b", "c"))
    assert [tool.name for tool in kept] == ["a"]
    assert excluded["b"].startswith("denied")
    assert excluded["c"] == "not in allow_tools"


def test_cap_is_applied_after_explicit_choices() -> None:
    config = McpServerConfig(transport="http", url="https://x", max_tools=2)
    kept, excluded = select_tools(config, _tools("a", "b", "c", "d"))
    assert len(kept) == 2
    assert set(excluded) == {"c", "d"}
    assert "max_tools" in excluded["c"]


# --------------------------------------------------------------------------- #
# bridge: naming, risk, trust
# --------------------------------------------------------------------------- #
def test_proxy_names_are_namespaced_by_server() -> None:
    assert proxy_name("git", "diff") == "mcp.git.diff"


def test_unannotated_tools_are_destructive_risk() -> None:
    # The spec default for destructiveHint is true when absent; an un-annotated
    # tool from an untrusted party must sit at the top of the risk ladder.
    info = McpToolInfo(name="anything")
    assert risk_for(info, "trusted") is RiskLevel.DESTRUCTIVE


def test_read_only_hint_is_only_believed_from_full_trust() -> None:
    info = McpToolInfo(name="peek", annotations={"readOnlyHint": True, "destructiveHint": False})
    assert risk_for(info, "full") is RiskLevel.READ
    assert risk_for(info, "trusted") is RiskLevel.WRITE
    assert risk_for(info, "sandboxed") is RiskLevel.EXECUTE


def test_a_proxy_carries_the_server_schema_and_mcp_metadata() -> None:
    config = McpServerConfig(transport="http", url="https://x", trust="trusted", group="web")
    info = McpToolInfo(
        name="search",
        description="Find things.",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    )
    proxy = McpProxyTool("web", config, info)
    assert proxy.name == "mcp.web.search"
    assert proxy.json_schema()["required"] == ["q"]
    spec = proxy.spec()
    assert spec["mcp"]["server"] == "web"
    assert spec["mcp"]["trust"] == "trusted"
    assert "mcp:web" in spec["tags"]
    cleaned, problems = proxy.validate_arguments({})
    assert problems == ["missing required argument 'q'"]


def test_duplicate_proxies_are_dropped_not_doubled() -> None:
    config = McpServerConfig(transport="http", url="https://x")
    info = McpToolInfo(name="t")
    proxies = build_proxies([("s", config, info), ("s", config, info)])
    assert len(proxies) == 1


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def test_the_catalog_covers_the_core_ecosystem() -> None:
    keys = {entry.key for entry in MCP_CATALOG}
    assert {"filesystem", "git", "memory", "fetch", "sequential-thinking", "time"} <= keys


def test_catalog_configs_substitute_the_project_root() -> None:
    entry = catalog_entry("git")
    assert entry is not None
    config = config_from_entry(entry, project_root="/repo")
    assert "/repo" in config.args
    assert config.origin == "catalog:git"


def test_pure_reasoning_servers_default_to_full_trust() -> None:
    entry = catalog_entry("sequential-thinking")
    assert entry is not None and entry.default_trust == "full"


# --------------------------------------------------------------------------- #
# manager policy (no connections)
# --------------------------------------------------------------------------- #
def test_blocked_and_disabled_servers_are_not_runnable() -> None:
    settings = Settings()
    settings.mcp.servers["a"] = McpServerConfig(transport="http", url="https://x", enabled=False)
    settings.mcp.servers["b"] = McpServerConfig(transport="http", url="https://x", trust="blocked")
    settings.mcp.servers["c"] = McpServerConfig(transport="http", url="https://x")
    manager = McpManager(settings)
    assert manager.server_names(include_disabled=False) == ["c"]


def test_unknown_server_is_a_helpful_keyerror() -> None:
    manager = McpManager(Settings())
    with pytest.raises(KeyError, match="none configured"):
        manager.server_config("ghost")


def test_describe_masks_nothing_it_should_not_know() -> None:
    settings = Settings()
    settings.mcp.servers["s"] = McpServerConfig(
        transport="http", url="https://mcp.example/api", headers={"Authorization": "store:corp"}
    )
    described = McpManager(settings).describe()
    assert described["servers"][0]["target"] == "https://mcp.example/api"
    assert "store:corp" not in json.dumps(described)


# --------------------------------------------------------------------------- #
# live transport (spawns a real stdio server)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.integration
def test_live_stdio_end_to_end(probe_settings: Settings) -> None:
    async def scenario() -> None:
        manager = McpManager(probe_settings)
        try:
            health = await manager.test("probe")
            assert health.ok, health.error

            snap = await manager.snapshot("probe")
            assert snap.connected
            assert snap.capabilities is not None and snap.capabilities.server_name == "probe-server"
            assert {tool.name for tool in snap.tools} == {"add", "fail"}
            assert [item.uri for item in snap.resources] == ["probe://greeting"]
            assert [item.name for item in snap.prompts] == ["review"]

            ok, text, structured = await manager.call_tool("probe", "add", {"a": 20, "b": 22})
            assert ok and text == "42" and structured.get("result") == 42

            # A raising tool is a domain failure (isError), not an exception.
            # (The 2.x SDK masks exception details server-side, so assert on the
            # flag and on the fact the text names the tool, not on "boom".)
            ok, text, _ = await manager.call_tool("probe", "fail", {"message": "boom"})
            assert not ok and "fail" in text
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.integration
def test_live_agent_tools_and_result_normalisation(probe_settings: Settings) -> None:
    async def scenario() -> None:
        manager = McpManager(probe_settings)
        try:
            selected, warnings = await manager.agent_tools()
            assert warnings == []
            proxies = build_proxies(selected)
            names = {proxy.name for proxy in proxies}
            assert names == {"mcp.probe.add", "mcp.probe.fail"}

            proxy = next(p for p in proxies if p.name == "mcp.probe.add")

            class Ctx:
                extra = {"mcp_manager": manager}

            result = await proxy.run(Ctx(), a=1, b=2)
            assert result.ok and result.output == "3"
            assert result.data.get("result") == 3
            assert result.metadata["server"] == "probe"
        finally:
            await manager.close()

    asyncio.run(scenario())


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.integration
def test_a_dead_server_is_fail_soft(tmp_path: Path) -> None:
    settings = Settings()
    settings.mcp.servers["dead"] = McpServerConfig(
        transport="stdio", command=sys.executable, args=["-c", "import sys; sys.exit(3)"], startup_timeout_s=5
    )

    async def scenario() -> None:
        manager = McpManager(settings)
        try:
            health = await manager.test("dead")
            assert not health.ok and health.error

            selected, warnings = await manager.agent_tools()
            assert selected == []
            assert warnings and "dead" in warnings[0]
        finally:
            await manager.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# CLI management
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _cwd(cli_env: None) -> None:
    """CLI tests run inside the generated project."""


def invoke(*args: str, expect: int | None = 0) -> str:
    result = runner.invoke(app, list(args))
    if expect is not None:
        assert result.exit_code == expect, f"`ua {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
    return result.output


def payload(output: str) -> dict:
    start = output.index("{")
    return json.loads(output[start:])


def test_the_mcp_group_is_reachable() -> None:
    assert "mcp" in invoke("--help")
    for command in ("catalog", "add", "remove", "list", "show", "test", "enable", "disable", "trust", "tools"):
        assert command in invoke("mcp", "--help")


def test_catalog_lists_the_ecosystem() -> None:
    data = payload(invoke("mcp", "catalog", "--json"))
    assert {entry["key"] for entry in data["catalog"]} >= {"git", "fetch", "time"}


def test_add_writes_a_validated_entry_and_list_shows_it() -> None:
    data = payload(invoke("mcp", "add", "probe", "--command", "echo", "--arg", "hi", "--no-test", "--json"))
    assert data["added"] == "probe"
    assert data["config"]["command"] == "echo"

    listed = payload(invoke("mcp", "list", "--json"))
    assert [server["name"] for server in listed["servers"]] == ["probe"]
    assert listed["servers"][0]["trust"] == "trusted"


def test_add_from_catalog_by_key() -> None:
    data = payload(invoke("mcp", "add", "time", "--no-test", "--json"))
    assert data["config"]["command"] == "uvx"
    assert data["config"]["origin"] == "catalog:time"


def test_add_rejects_a_duplicate_name() -> None:
    invoke("mcp", "add", "probe", "--command", "echo", "--no-test", "--json")
    output = invoke("mcp", "add", "probe", "--command", "echo", "--no-test", expect=2)
    assert "already exists" in output


def test_add_requires_catalog_key_or_explicit_transport() -> None:
    output = invoke("mcp", "add", "not-in-catalog", "--no-test", expect=2)
    assert "not in the catalog" in output


def test_trust_and_enable_disable_are_persisted() -> None:
    invoke("mcp", "add", "probe", "--command", "echo", "--no-test", "--json")
    invoke("mcp", "trust", "probe", "sandboxed", "--json")
    data = payload(invoke("mcp", "trust", "probe", "--json"))
    assert data["trust"] == "sandboxed"

    invoke("mcp", "disable", "probe", "--json")
    listed = payload(invoke("mcp", "list", "--json"))
    assert listed["servers"][0]["enabled"] is False

    invoke("mcp", "enable", "probe", "--json")
    listed = payload(invoke("mcp", "list", "--json"))
    assert listed["servers"][0]["enabled"] is True


def test_trust_rejects_an_unknown_level() -> None:
    invoke("mcp", "add", "probe", "--command", "echo", "--no-test", "--json")
    output = invoke("mcp", "trust", "probe", "omniscient", expect=2)
    assert "omniscient" in output


def test_remove_deletes_the_entry() -> None:
    invoke("mcp", "add", "probe", "--command", "echo", "--no-test", "--json")
    data = payload(invoke("mcp", "remove", "probe", "--yes", "--json"))
    assert data["removed"] == "probe"
    listed = payload(invoke("mcp", "list", "--json"))
    assert listed["servers"] == []


def test_show_without_connect_reports_config_only() -> None:
    invoke("mcp", "add", "probe", "--command", "echo", "--no-test", "--json")
    data = payload(invoke("mcp", "show", "probe", "--no-connect", "--json"))
    assert data["name"] == "probe"
    assert data["connected"] is False


def test_test_with_nothing_configured_is_a_usage_error() -> None:
    output = invoke("mcp", "test", expect=2)
    assert "No MCP servers configured" in output


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.integration
def test_cli_test_against_a_live_server(tmp_path: Path) -> None:
    server = Path.cwd() / "probe_server.py"
    server.write_text(PROBE_SERVER, encoding="utf-8")
    invoke(
        "mcp", "add", "probe",
        "--command", sys.executable, "--arg", str(server),
        "--no-test", "--json",
    )
    data = payload(invoke("mcp", "test", "probe", "--json"))
    assert data["ok"] is True
    assert data["results"]["probe"]["ok"] is True
