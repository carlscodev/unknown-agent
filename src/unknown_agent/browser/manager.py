"""Browser session manager: lifecycle, app bootstrapping, graceful degradation.

The manager owns:

* Playwright/browser availability checks with actionable errors
  (``ua doctor --install-browsers``),
* one reusable browser instance per run, contexts per session,
* optional dev-server bootstrapping: if the configured URL is unreachable and a
  ``start_command`` is set, the app is started, polled until it responds, and
  shut down afterwards - so browser validation works in a cold CI container,
* process-safe shutdown (the whole process group is killed, no orphan servers).

Playwright is an optional dependency: importing this module never fails, and
using it without Playwright installed raises a :class:`ConfigError` that tells
the user exactly what to install.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from unknown_agent.config.models import BrowserConfig
from unknown_agent.errors import ConfigError, ToolError
from unknown_agent.host.process import run_process, split_command
from unknown_agent.observability.logging import get_logger

log = get_logger("browser.manager")

BROWSER_INSTALL_HINT = "pipx install 'unknown-agent[browser]' && ua doctor --install-browsers"


@dataclass(slots=True)
class ManagedApp:
    """A dev server started by the agent for validation purposes."""

    command: str
    url: str
    process: Any = None
    log_lines: list[str] = field(default_factory=list)
    started_at: float = 0.0
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "url": self.url,
            "started": bool(self.process),
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
        }


def playwright_available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False, f"playwright is not installed - run: {BROWSER_INSTALL_HINT}"
    return True, "playwright is installed"


def browsers_installed() -> tuple[bool, str]:
    """Best-effort check that a browser binary exists."""

    candidates = [
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
        Path.home() / "AppData" / "Local" / "ms-playwright",
    ]
    for candidate in candidates:
        if candidate.exists() and any(candidate.glob("*chromium*")):
            return True, f"browsers found in {candidate}"
    return False, "no Playwright browser binaries found - run: ua doctor --install-browsers"


async def install_browsers(browser: str = "chromium", *, with_deps: bool = False) -> tuple[bool, str]:
    """Install browser binaries (called by ``ua doctor --install-browsers``)."""

    argv = ["python3", "-m", "playwright", "install", browser]
    if with_deps and _has_root():
        argv.append("--with-deps")
    result = await run_process(argv, timeout=1800)
    return result.ok, (result.combined() or "")[-4000:]


def _has_root() -> bool:
    import os

    return hasattr(os, "geteuid") and os.geteuid() == 0


class BrowserSessionManager:
    """Creates and disposes browser sessions for a run."""

    def __init__(self, config: BrowserConfig, *, artifacts_dir: Path | None = None) -> None:
        self.config = config
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else Path.cwd() / ".unknown" / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._playwright: Any = None
        self._browser: Any = None
        self._session: Any = None
        self.app: ManagedApp | None = None

    # ------------------------------------------------------------------ #
    def ensure_available(self) -> None:
        available, message = playwright_available()
        if not available:
            raise ConfigError(
                "Browser validation requires Playwright.",
                hint=BROWSER_INSTALL_HINT,
                context={"detail": message},
            )

    async def session(
        self,
        *,
        headless: bool | None = None,
        viewport: tuple[int, int] | None = None,
        base_url: str | None = None,
        start_app: bool = False,
    ) -> Any:
        """Return a live :class:`BrowserSession`, creating one if needed."""

        from unknown_agent.browser.session import BrowserSession

        self.ensure_available()
        if self._session is not None:
            if base_url and not self._session.base_url:
                self._session.base_url = base_url.rstrip("/")
            return self._session

        if self.config.start_command and start_app:
            await self.start_app()

        started = time.perf_counter()
        self._playwright = await self._start_playwright()
        launch_options: dict[str, Any] = {
            "headless": self.config.headless if headless is None else headless,
            "args": [*self.config.extra_args],
        }
        if self.config.browser == "chromium" and not self.config.extra_args:
            launch_options["args"] = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        try:
            self._browser = await getattr(self._playwright, self.config.browser).launch(**launch_options)
        except Exception as exc:  # noqa: BLE001 - browser missing or sandbox issue
            raise ToolError(
                f"Could not launch {self.config.browser}: {exc}",
                hint="Run `ua doctor --install-browsers`, or launch with headless=false on a desktop session.",
            ) from exc

        width, height = viewport or (self.config.viewport_width, self.config.viewport_height)
        context_options: dict[str, Any] = {
            "viewport": {"width": width, "height": height},
            "ignore_https_errors": self.config.ignore_https_errors,
            "device_scale_factor": self.config.device_scale_factor,
        }
        if self.config.record_har:
            context_options["record_har_path"] = str(self.artifacts_dir / "network.har")
        if self.config.video:
            context_options["record_video_dir"] = str(self.artifacts_dir / "video")
        context = await self._browser.new_context(**context_options)
        context.set_default_timeout(self.config.timeout_ms)
        if self.config.trace:
            with contextlib.suppress(Exception):
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        session = BrowserSession(page, context, self.config, artifacts_dir=self.artifacts_dir, base_url=base_url or self.config.base_url)
        self._session = session
        log.debug("browser session ready in %.2fs", time.perf_counter() - started)
        return session

    async def _start_playwright(self) -> Any:
        from playwright.async_api import async_playwright

        manager = async_playwright()
        return await manager.start()

    # ------------------------------------------------------------------ #
    # app bootstrapping
    # ------------------------------------------------------------------ #
    async def start_app(self, *, timeout_s: float | None = None, url: str | None = None) -> ManagedApp:
        """Start the configured dev server and wait until it answers."""

        if self.app is not None and self.app.process is not None:
            return self.app
        command = self.config.start_command
        if not command:
            raise ConfigError(
                "No start command configured for browser validation.",
                hint="Set [browser] start_command = \"npm run dev\" (or [project.commands] dev = ...).",
            )
        target_url = url or self.config.base_url or "http://127.0.0.1:3000"
        app = ManagedApp(command=command, url=target_url, started_at=time.time())
        argv = split_command(command)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path.cwd()),
        )
        app.process = process

        async def pump() -> None:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                app.log_lines.append(line.decode("utf-8", errors="replace").rstrip())
                if len(app.log_lines) > 400:
                    del app.log_lines[:100]

        pump_task = asyncio.create_task(pump())
        ready = await self._wait_until_ready(target_url, timeout_s or self.config.start_timeout_s)
        self.app = app
        if not ready:
            await self.stop_app()
            raise ToolError(
                f"The app did not become reachable at {target_url} within {timeout_s or self.config.start_timeout_s:.0f}s.",
                hint="Check the start command output; the URL may need to be set with [browser] base_url.",
                context={"log": "\n".join(app.log_lines[-30:])},
            )
        app.stdout = "\n".join(app.log_lines[-80:])
        log.info("started app for browser validation: %s", command)
        pump_task.add_done_callback(lambda _task: None)
        return app

    async def _wait_until_ready(self, url: str, timeout_s: float) -> bool:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if await asyncio.to_thread(_tcp_open, host, port, 0.5):
                return True
            if self.app and self.app.process and self.app.process.returncode is not None:
                return False
            await asyncio.sleep(0.5)
        return False

    async def stop_app(self) -> None:
        if self.app is None or self.app.process is None:
            return
        process = self.app.process
        with contextlib.suppress(Exception):
            if process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=8)
            if process.returncode is None:  # pragma: no cover - stubborn server
                process.kill()
                await process.wait()
        self.app = None

    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        """Release everything.  Safe to call multiple times."""

        session, browser, playwright = self._session, self._browser, self._playwright
        self._session = self._browser = self._playwright = None
        if session is not None:
            with contextlib.suppress(Exception):
                if self.config.trace and session.context is not None:
                    trace_path = self.artifacts_dir / "trace.zip"
                    await session.context.tracing.stop(path=str(trace_path))
            with contextlib.suppress(Exception):
                await session.context.close()
            with contextlib.suppress(Exception):
                await session.close()
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()
        await self.stop_app()

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "browser": self.config.browser,
            "headless": self.config.headless,
            "base_url": self.config.base_url,
            "start_command": self.config.start_command,
            "playwright_installed": playwright_available()[0],
            "session_open": self._session is not None,
            "app": self.app.to_dict() if self.app else None,
            "artifacts_dir": str(self.artifacts_dir),
        }


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


__all__ = [
    "BrowserSessionManager",
    "ManagedApp",
    "playwright_available",
    "browsers_installed",
    "install_browsers",
    "BROWSER_INSTALL_HINT",
]
