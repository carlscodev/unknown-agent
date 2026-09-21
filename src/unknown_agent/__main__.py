"""Enable ``python -m unknown_agent`` as an equivalent entry point to ``ua``."""

from __future__ import annotations

from unknown_agent.cli.app import main

if __name__ == "__main__":
    main()
