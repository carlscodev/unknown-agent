"""Computer-use drivers.

:class:`Driver` is the seam future backends implement (native OS control,
remote VMs, provider-hosted virtual displays).  The shipped implementation is
:class:`BrowserDriver` - the Playwright page as display + input surface - which
is where computer use is genuinely useful for a software-creation agent today:
interacting with the app being built.

A driver is deliberately dumb: it performs one action and reports one
observation.  Budgets, confirmation, allowlists and journalling live in
:class:`~unknown_agent.computer_use.session.ComputerUseSession`, so a new
driver cannot accidentally skip the safety layer.
"""

from __future__ import annotations

import abc
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from unknown_agent.computer_use.actions import Action, Observation
from unknown_agent.observability.logging import get_logger

log = get_logger("computer_use.driver")

__all__ = ["Driver", "BrowserDriver"]


class Driver(abc.ABC):
    """One display+input surface."""

    name: str = "abstract"

    @abc.abstractmethod
    async def observe(self, *, screenshot_dir: Path, tag: str) -> Observation:
        """Capture the current state (screenshot + text snapshot)."""

    @abc.abstractmethod
    async def perform(self, action: Action) -> None:
        """Execute one action.  Raise for driver-level failures."""

    @abc.abstractmethod
    async def close(self) -> None: ...

    def current_host(self) -> str:
        """Hostname currently displayed, for allowlist checks.  '' = unknown."""

        return ""


class BrowserDriver(Driver):
    """Playwright-backed driver over the product's managed browser session."""

    name = "browser"

    def __init__(self, browser_manager: Any, *, viewport: tuple[int, int] = (1440, 900)) -> None:
        self._manager = browser_manager
        self._session: Any = None
        self._viewport = viewport
        self._counter = 0

    async def _page(self) -> Any:
        if self._session is None:
            self._session = await self._manager.session(viewport=self._viewport)
        return self._session.page

    def current_host(self) -> str:
        if self._session is None:
            return ""
        try:
            return urlparse(self._session.page.url).hostname or ""
        except Exception:  # noqa: BLE001
            return ""

    async def observe(self, *, screenshot_dir: Path, tag: str) -> Observation:
        page = await self._page()
        self._counter += 1
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        shot = screenshot_dir / f"cu-{self._counter:03d}-{tag}.png"
        screenshot_path = ""
        try:
            await page.screenshot(path=str(shot))
            screenshot_path = str(shot)
        except Exception as exc:  # noqa: BLE001 - a failed screenshot is a degraded observation
            log.debug("screenshot failed: %s", exc)
        dom_text = ""
        try:
            body = await page.inner_text("body", timeout=3000)
            dom_text = " ".join(body.split())[:6000]
        except Exception:  # noqa: BLE001
            dom_text = ""
        return Observation(
            url=str(getattr(page, "url", "") or ""),
            title=await _safe_title(page),
            screenshot_path=screenshot_path,
            dom_text=dom_text,
            viewport=self._viewport,
            captured_at=time.time(),
        )

    async def perform(self, action: Action) -> None:
        page = await self._page()
        kind = action.kind
        if kind == "navigate":
            await self._session.goto(action.text)
        elif kind in {"click", "double_click"}:
            clicks = 2 if kind == "double_click" else 1
            await page.mouse.click(float(action.x or 0), float(action.y or 0), click_count=clicks)
        elif kind == "hover":
            await page.mouse.move(float(action.x or 0), float(action.y or 0))
        elif kind == "type":
            await page.keyboard.type(action.text, delay=20)
        elif kind == "key":
            await page.keyboard.press(action.text)
        elif kind == "scroll":
            await page.mouse.wheel(float(action.dx), float(action.dy))
        elif kind == "wait":
            await page.wait_for_timeout(min(int(action.ms or 500), 15_000))
        elif kind == "back":
            await page.go_back()
        elif kind in {"screenshot", "read_dom"}:
            pass  # observation-only; the session observes after every action anyway
        else:  # pragma: no cover - parse_action guards this
            raise ValueError(f"unsupported action {kind!r}")

    async def close(self) -> None:
        self._session = None  # the browser manager owns the real lifecycle


async def _safe_title(page: Any) -> str:
    try:
        return str(await page.title())
    except Exception:  # noqa: BLE001
        return ""
