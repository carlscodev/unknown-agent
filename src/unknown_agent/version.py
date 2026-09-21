"""Single source of truth for the Unknown Agent version.

The version is read by the build backend (see ``[tool.hatch.version]`` in
``pyproject.toml``), by the CLI (``ua version``), and by the runtime
(``unknown_agent.__version__``).  Never duplicate it anywhere else.

Versioning policy
-----------------
``MAJOR``  breaking change to the CLI contract, the plugin API, or on-disk formats.
``MINOR``  new commands, tools, providers, gates, or memory capabilities.
``PATCH``  bug fixes, prompt tuning, performance and UX polish.
"""

from __future__ import annotations

__version__ = "1.2.0"

#: ``<major>.<minor>`` release line, used for cache keys and migration targeting.
VERSION_LINE = ".".join(__version__.split(".")[:2])

#: Minimum on-disk state schema this build can read without migration.
MIN_STATE_SCHEMA = 1
