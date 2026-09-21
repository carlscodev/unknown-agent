"""Configuration subsystem: typed schema, layered loading, secrets, paths."""

from unknown_agent.config.loader import LoadedSettings, load_settings, write_default_config
from unknown_agent.config.models import Settings
from unknown_agent.config.paths import AppPaths, find_project_root, resolve_paths
from unknown_agent.config.secrets import REDACTOR, redact_text, resolve_secret

__all__ = [
    "Settings",
    "LoadedSettings",
    "load_settings",
    "write_default_config",
    "AppPaths",
    "resolve_paths",
    "find_project_root",
    "resolve_secret",
    "redact_text",
    "REDACTOR",
]
