"""Unknown Agent - a Python-first hyperagent for building real software.

The public surface is deliberately small: the CLI (``unknown_agent.cli``) and the
programmatic API (``unknown_agent.api``).  Everything else is internal and may
change between minor releases; see ``docs/architecture.md`` for stability tiers.
"""

from __future__ import annotations

from unknown_agent.errors import UnknownAgentError
from unknown_agent.version import VERSION_LINE, __version__

__all__ = ["UnknownAgentError", "__version__", "VERSION_LINE"]
