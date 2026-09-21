"""Plugin/extension layer: entry points, user and project plugins."""

from unknown_agent.extensions.base import Plugin, PluginContext, PluginRecord, SimplePlugin, load_object
from unknown_agent.extensions.loader import ENTRY_POINT_GROUP, PluginManager, write_plugin_skeleton

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginRecord",
    "SimplePlugin",
    "load_object",
    "PluginManager",
    "write_plugin_skeleton",
    "ENTRY_POINT_GROUP",
]
