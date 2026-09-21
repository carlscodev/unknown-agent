"""QA-only stub backend: real web server + frontend wiring, fast stubbed Runner
and provider. Lets the browser QA exercise mode chips, artifact kinds, monitor
and WS reconnect without depending on the live gateway. Not shipped behavior."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/uaweb/config")
os.environ.setdefault("XDG_DATA_HOME", "/tmp/uaweb/data")
os.environ.setdefault("XDG_STATE_HOME", "/tmp/uaweb/state")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/uaweb/cache")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from unknown_agent.config.paths import resolve_paths
from unknown_agent.web.auth import WebAuthStore
from unknown_agent.web.server import create_app
import unknown_agent.core.runner as runner_mod
import unknown_agent.providers.registry as registry

PORT = 8790

CODE = "\n".join(f"<div class=\"row{i}\">fila {i}</div>" for i in range(30))
CHAT_TEXT = (
    "Aqui va el codigo pedido:\n\n```html\n<!DOCTYPE html>\n<html>\n"
    + CODE
    + "\n</html>\n```\n\nFin del codigo."
)


class StubRunner:
    def __init__(self, loaded, request=None, hooks=None):
        self.hooks = hooks

    async def setup(self):
        await asyncio.sleep(0.6)
        self.hooks.on_thought("planeando los tres artefactos de qa9")
        self.hooks.on_tool_start(SimpleNamespace(name="write_file"))
        self.hooks.on_tool_end(
            SimpleNamespace(name="write_file"), SimpleNamespace(ok=True, summary="wrote qa9/page.html")
        )
        self.hooks.on_tool_start(SimpleNamespace(name="write_file"))
        self.hooks.on_tool_end(
            SimpleNamespace(name="write_file"), SimpleNamespace(ok=True, summary="wrote qa9/logo.png")
        )

    async def execute(self):
        step = SimpleNamespace(decision=SimpleNamespace(answer="Listo: page.html, logo.svg y notas.md en qa9/."))
        return SimpleNamespace(
            summary="boilerplate",
            outcome=SimpleNamespace(steps=[step]),
            ok=True,
            blockers=[],
            status="completed",
            files_changed=["qa9/page.html", "qa9/logo.svg", "qa9/notas.md"],
            state=SimpleNamespace(model="stub", provider="unli"),
        )


class StubProvider:
    supports_streaming = True
    model = "stub-1"
    temperature = 0.2
    max_output_tokens = 512

    async def stream(self, request):
        for chunk in [CHAT_TEXT[i : i + 24] for i in range(0, len(CHAT_TEXT), 24)]:
            await asyncio.sleep(0.02)
            yield SimpleNamespace(delta=chunk, error=None, usage=None, finish_reason=None)

    async def complete(self, request):
        return SimpleNamespace(content=CHAT_TEXT, error=None)


runner_mod.Runner = StubRunner
registry.build_provider = lambda *_, **__: StubProvider()

paths = resolve_paths(Path("/tmp/uaweb/work"))
auth = WebAuthStore(paths.config_dir / "web_auth.json")
app = create_app(
    auth=auth,
    paths=paths,
    static_dir=Path(__file__).resolve().parents[2] / "src" / "unknown_agent" / "web" / "static",
)


async def kill():
    asyncio.get_event_loop().call_later(0.2, lambda: os._exit(0))
    return {"ok": True}


# The SPA catch-all mount inside create_app would swallow this route if it were
# appended normally, so it goes in front of everything else.
from fastapi.routing import APIRoute

app.router.routes.insert(0, APIRoute("/admin/kill", kill, methods=["GET"]))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
