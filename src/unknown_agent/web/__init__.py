"""Web control panel: FastAPI backend + Vue 3 frontend, token/device-pairing auth."""

from unknown_agent.web.auth import WebAuthStore
from unknown_agent.web.server import create_app

__all__ = ["WebAuthStore", "create_app"]
