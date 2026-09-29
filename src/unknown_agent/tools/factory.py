"""Build the default tool registry for a session.

One function wires every builtin tool, applies user configuration (disabled
categories, read-only sessions) and then layers plugin tools on top.  Keeping
construction in one place means ``ua tools list``, the agent loop and the plugin
system always see exactly the same toolset.
"""

from __future__ import annotations

from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.observability.logging import get_logger
from unknown_agent.tools.base import Tool
from unknown_agent.tools.registry import ToolRegistry

log = get_logger("tools.factory")

#: Categories shipped with the product.  ``all`` in config selects everything.
BUILTIN_CATEGORIES = (
    "filesystem",
    "search",
    "repo",
    "execution",
    "memory",
    "validation",
    "browser",
    "network",
    "analysis",
    "scaffold",
    "docs",
    "planning",
    "skills",
    "mcp",
    "vision",
    "computer_use",
)


def build_default_registry(
    settings: Settings,
    *,
    include_browser: bool | None = None,
    categories: tuple[str, ...] | None = None,
) -> ToolRegistry:
    """Assemble the registry for the current settings."""

    from unknown_agent.tools.builtin import (
        ast_tools,
        browser_tools,
        exec_tools,
        fs_tools,
        memory_tools,
        repo_tools,
        test_runner_tools,
        validation_tools,
    )

    registry = ToolRegistry()
    wanted = categories or BUILTIN_CATEGORIES

    def add(tools: list[Tool], category_filter: str | None = None) -> None:
        for tool in tools:
            if category_filter and category_filter not in wanted:
                continue
            registry.register(tool, source="builtin")

    if "filesystem" in wanted or "search" in wanted:
        for tool in fs_tools.build_tools():
            if tool.category in wanted or (tool.category == "filesystem" and "filesystem" in wanted) or (tool.category == "search" and "search" in wanted):
                registry.register(tool, source="builtin")
    add(repo_tools.build_tools(), "repo")
    add(exec_tools.build_tools(), "execution")
    add(memory_tools.build_tools(), "memory")
    add(validation_tools.build_tools(), "validation")
    # Structure-aware editing and background test execution. Registered by their
    # own category so `[tools] categories` filtering keeps working per tool.
    add(ast_tools.build_tools(), "analysis")
    add(test_runner_tools.build_tools(), "validation")

    # Advanced tools span several categories (analysis, scaffold, docs, planning,
    # mcp, skills, browser) - register each against its own category so
    # [tools] categories filtering works per tool, not per module.
    from unknown_agent.tools.builtin import advanced_tools

    for tool in advanced_tools.build_tools():
        if tool.category in wanted:
            registry.register(tool, source="builtin")

    # Vision tools: only advertised when the vision system is enabled. Routing
    # (including fallback profiles) is decided per call by VisionService, so a
    # non-vision active model still gets working tools if a fallback exists.
    if "vision" in wanted and settings.vision.enabled:
        from unknown_agent.vision import tools as vision_tools

        for tool in vision_tools.build_tools():
            registry.register(tool, source="builtin")

    # Computer-use tools: doubly gated (config + explicit category) because they
    # drive real UI interaction. Disabled config withholds with the reason.
    if "computer_use" in wanted:
        from unknown_agent.computer_use import tools as cu_tools

        if settings.computer_use.enabled:
            for tool in cu_tools.build_tools():
                registry.register(tool, source="builtin")
        else:
            for tool in cu_tools.build_tools():
                registry.withhold(tool.name, "computer use is disabled ([computer_use] enabled = false)")

    browser_enabled = settings.browser.enabled if include_browser is None else include_browser
    if browser_enabled and "browser" in wanted:
        # Never advertise browser tools that cannot run: a missing Playwright install
        # would otherwise cost the agent steps and produce noisy failures. The user is
        # told exactly what to install by `ua doctor` and `ua browser status`.
        from unknown_agent.browser.manager import playwright_available

        available, message = playwright_available()
        if available or include_browser:
            for tool in browser_tools.build_tools():
                registry.register(tool, source="builtin")
        else:
            for tool in browser_tools.build_tools():
                registry.withhold(tool.name, f"browser tools need Playwright: {message}")
            log.debug("browser tools disabled: playwright is not installed")
    else:
        for tool in browser_tools.build_tools():
            registry.withhold(tool.name, "browser tools are disabled ([browser] enabled = false)")

    if settings.policy.read_only:
        for tool in list(registry):
            if tool.risk.value != "read":
                registry.withhold(tool.name, "this is a read-only session (policy.read_only = true)")
        log.debug("read-only session: mutating tools removed")

    return registry


def describe_registry(registry: ToolRegistry) -> dict[str, Any]:
    """Machine-readable inventory used by ``ua tools list --json`` and the docs."""

    return {
        "count": len(registry),
        "categories": {name: [tool.name for tool in tools] for name, tools in registry.by_category().items()},
        "tools": registry.catalog(),
    }


__all__ = ["build_default_registry", "describe_registry", "BUILTIN_CATEGORIES"]
