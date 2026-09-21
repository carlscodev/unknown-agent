"""Browser session: a live Chromium page plus evidence collection.

A session wraps Playwright with the instrumentation the agent actually needs:

* **console capture** - errors and warnings with their text and location,
* **network capture** - failed requests and non-2xx responses with status codes,
* **page errors** - uncaught exceptions,
* **screenshots / traces / video** - evidence artifacts written under
  ``.unknown/artifacts``,
* **flow execution** - a small, declarative step language the model can emit
  (goto/click/fill/press/wait/assert/scroll/select/eval) with per-step results.

Everything is optional and defensive: a missing browser, a dead page or a
navigation timeout produces a structured finding, never a crash that derails the
agent run.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.config.models import BrowserConfig
from unknown_agent.observability.logging import get_logger

log = get_logger("browser.session")

IGNORABLE_URL_PATTERNS = (
    r"favicon\.ico$",
    r"chrome-extension://",
    r"^data:",
    r"hot-update",
    r"__vite_ping",
    r"\.map$",
)


@dataclass(slots=True)
class FlowStepResult:
    index: int
    action: str
    target: str = ""
    ok: bool = True
    detail: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "target": self.target,
            "ok": self.ok,
            "detail": self.detail[:400],
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass(slots=True)
class FlowReport:
    url: str = ""
    passed: bool = True
    steps: list[FlowStepResult] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    console_warnings: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    title: str = ""
    text_excerpt: str = ""
    duration_ms: float = 0.0

    @property
    def failures(self) -> list[str]:
        failures = [f"step {item.index} ({item.action}): {item.detail}" for item in self.steps if not item.ok]
        failures.extend(f"page error: {item}" for item in self.page_errors)
        return failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "passed": self.passed,
            "steps": [step.to_dict() for step in self.steps],
            "console_errors": self.console_errors,
            "console_warnings": self.console_warnings,
            "failed_requests": self.failed_requests,
            "page_errors": self.page_errors,
            "artifacts": self.artifacts,
            "text_excerpt": self.text_excerpt[:2000],
            "duration_ms": round(self.duration_ms, 1),
        }

    def render(self, *, max_steps: int = 40) -> str:
        lines = [
            f"browser flow: {self.url or '(no url)'}",
            f"result: {'PASS' if self.passed else 'FAIL'}  steps={len(self.steps)}  duration={self.duration_ms / 1000:.1f}s",
        ]
        if self.title:
            lines.append(f"title: {self.title}")
        lines.append("steps:")
        lines.append(
            "  "
            + "\n  ".join(
                f"{step.index}. {step.action} {step.target} -> {'ok' if step.ok else 'FAIL'} {step.detail[:120]}".strip()
                for step in self.steps[:max_steps]
            )
        )
        if self.console_errors:
            lines.append("console errors:")
            lines.extend(f"  {item}" for item in self.console_errors[:10])
        if self.failed_requests:
            lines.append("failed requests:")
            lines.extend(f"  {item}" for item in self.failed_requests[:10])
        if self.page_errors:
            lines.append("page errors:")
            lines.extend(f"  {item}" for item in self.page_errors[:10])
        if self.text_excerpt:
            lines.append("visible text excerpt:")
            lines.append("  " + self.text_excerpt[:400].replace("\n", " "))
        if self.artifacts:
            lines.append("artifacts:")
            lines.extend(f"  {item}" for item in self.artifacts)
        return "\n".join(lines)


class BrowserSession:
    """A Playwright page with instrumentation and a flow runner."""

    def __init__(self, page: Any, context: Any, config: BrowserConfig, *, artifacts_dir: Path | None = None, base_url: str | None = None) -> None:
        self.page = page
        self.context = context
        self.config = config
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else Path.cwd()
        self.base_url = (base_url or config.base_url or "").rstrip("/")
        self.console_errors: list[str] = []
        self.console_warnings: list[str] = []
        self.failed_requests: list[str] = []
        self.page_errors: list[str] = []
        self._install_listeners()

    # ------------------------------------------------------------------ #
    def _install_listeners(self) -> None:
        def on_console(message: Any) -> None:
            try:
                location = message.location or {}
                where = f"{location.get('url', '')}:{location.get('lineNumber', '')}".strip(":")
                text = message.text
                if any(re.search(pattern, where or text) for pattern in IGNORABLE_URL_PATTERNS):
                    return
                if message.type == "error":
                    self.console_errors.append(f"{text} ({where})" if where else text)
                elif message.type == "warning":
                    self.console_warnings.append(text)
            except Exception:  # pragma: no cover - defensive
                pass

        def on_page_error(error: Any) -> None:
            self.page_errors.append(str(error))

        def on_request_failed(request: Any) -> None:
            url = request.url
            if any(re.search(pattern, url) for pattern in IGNORABLE_URL_PATTERNS):
                return
            failure = getattr(request, "failure", None)
            reason = failure.get("errorText") if isinstance(failure, dict) else (failure or "unknown")
            if "net::ERR_ABORTED" in str(reason):
                return
            self.failed_requests.append(f"{request.method} {url} - {reason}")

        def on_response(response: Any) -> None:
            try:
                if response.status >= 400 and not any(re.search(pattern, response.url) for pattern in IGNORABLE_URL_PATTERNS):
                    self.failed_requests.append(f"{response.request.method} {response.url} - HTTP {response.status}")
            except Exception:  # pragma: no cover
                pass

        for event, handler in (
            ("console", on_console),
            ("pageerror", on_page_error),
            ("requestfailed", on_request_failed),
            ("response", on_response),
        ):
            try:
                getattr(self.page, "on")(event, handler)
            except Exception:  # pragma: no cover - older playwright versions
                log.debug("could not attach browser listener %s", event)

    # ------------------------------------------------------------------ #
    async def goto(self, url: str) -> None:
        target = self.resolve(url)
        await self.page.goto(target, wait_until="domcontentloaded", timeout=self.config.navigation_timeout_ms)
        # Give client-rendered apps a moment to paint before assertions.
        with_network_idle = getattr(self.page, "wait_for_load_state", None)
        if with_network_idle is not None:
            try:
                await self.page.wait_for_load_state("networkidle", timeout=min(5000, self.config.navigation_timeout_ms))
            except Exception:  # pragma: no cover - pages with polling never go idle
                pass

    def resolve(self, url: str) -> str:
        if not url:
            return self.base_url or "about:blank"
        if url.startswith(("http://", "https://", "file://", "about:")):
            return url
        if not self.base_url:
            return f"http://127.0.0.1:3000{url if url.startswith('/') else '/' + url}"
        return f"{self.base_url}{url if url.startswith('/') else '/' + url}"

    async def screenshot(self, path: str, *, full_page: bool | None = None) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(target), full_page=self.config.screenshots if full_page is None else full_page)
        return str(target)

    async def text(self, *, limit: int = 4000) -> str:
        try:
            body = await self.page.inner_text("body", timeout=5000)
        except Exception:  # pragma: no cover - about:blank, detached frames
            return ""
        return " ".join(body.split())[:limit]

    async def close(self) -> None:
        for closable in (self.page,):
            try:
                await closable.close()
            except Exception:  # pragma: no cover - already closed
                pass

    # ------------------------------------------------------------------ #
    # flow execution
    # ------------------------------------------------------------------ #
    async def run_flow(self, url: str = "", steps: list[dict[str, Any]] | None = None) -> FlowReport:
        """Execute a declarative flow and collect evidence."""

        report = FlowReport(url=self.resolve(url) if url else self.base_url)
        started = time.perf_counter()
        if url:
            try:
                await self.goto(url)
            except Exception as exc:  # noqa: BLE001 - navigation errors are findings
                report.passed = False
                report.steps.append(FlowStepResult(index=0, action="goto", target=self.resolve(url), ok=False, detail=f"{type(exc).__name__}: {exc}"))
                return self._finalise(report, started)

        for index, raw in enumerate(steps or [], start=1):
            step = raw if isinstance(raw, dict) else {"action": str(raw)}
            action = str(step.get("action", "noop")).lower()
            step_started = time.perf_counter()
            result = FlowStepResult(index=index, action=action, target=_target(step))
            try:
                detail = await self._execute_step(action, step)
                result.detail = detail or ""
            except Exception as exc:  # noqa: BLE001 - each step failure becomes evidence
                result.ok = False
                result.detail = f"{type(exc).__name__}: {str(exc)[:300]}"
                report.passed = False
            result.duration_ms = (time.perf_counter() - step_started) * 1000
            report.steps.append(result)
            if self.config.screenshot_on_failure and not result.ok:
                with_artifact = await self._safe_screenshot(f"failure-{index}-{action}.png")
                if with_artifact:
                    report.artifacts.append(with_artifact)
            if not result.ok and step.get("stop_on_failure", True):
                break

        return self._finalise(report, started)

    async def _execute_step(self, action: str, step: dict[str, Any]) -> str:
        page = self.page
        timeout = int(step.get("timeout_ms", self.config.timeout_ms))
        selector = step.get("selector") or step.get("target") or ""

        if action == "goto":
            await self.goto(str(step.get("url") or step.get("value") or ""))
            return f"loaded {self.resolve(str(step.get('url') or step.get('value') or ''))}"

        if action == "click":
            locator = self._locator(step)
            await locator.click(timeout=timeout)
            return f"clicked {selector}"

        if action == "fill":
            locator = self._locator(step)
            await locator.fill(str(step.get("value", "")), timeout=timeout)
            return f"filled {selector}"

        if action == "type":
            locator = self._locator(step)
            await locator.type(str(step.get("value", "")), timeout=timeout)
            return f"typed into {selector}"

        if action == "press":
            key = str(step.get("value") or step.get("key") or "Enter")
            if selector:
                await self._locator(step).press(key, timeout=timeout)
            else:
                await page.keyboard.press(key)
            return f"pressed {key}"

        if action == "select":
            await self._locator(step).select_option(str(step.get("value", "")), timeout=timeout)
            return f"selected {step.get('value')} in {selector}"

        if action == "check":
            await self._locator(step).check(timeout=timeout)
            return f"checked {selector}"

        if action == "wait_for":
            if selector:
                await self._locator(step).wait_for(state=str(step.get("state", "visible")), timeout=timeout)
                return f"{selector} reached state {step.get('state', 'visible')}"
            await asyncio.sleep(float(step.get("value", 1.0)))
            return "waited"

        if action in {"wait_for_timeout", "wait"}:
            await asyncio.sleep(min(30.0, float(step.get("value", 1.0)) / (1000 if action == "wait_for_timeout" else 1)))
            return "waited"

        if action == "assert_text":
            needle = str(step.get("text") or step.get("value") or "")
            if selector:
                content = await self._locator(step).inner_text(timeout=timeout)
            else:
                content = await self.text(limit=20_000)
            if needle.lower() not in content.lower():
                raise AssertionError(f"expected text {needle!r} was not found")
            return f"found text {needle!r}"

        if action == "assert_no_text":
            needle = str(step.get("text") or step.get("value") or "")
            content = await self.text(limit=20_000)
            if needle.lower() in content.lower():
                raise AssertionError(f"unexpected text {needle!r} is present")
            return f"text {needle!r} absent"

        if action == "assert_visible":
            await self._locator(step).wait_for(state="visible", timeout=timeout)
            return f"{selector} is visible"

        if action == "assert_hidden":
            await self._locator(step).wait_for(state="hidden", timeout=timeout)
            return f"{selector} is hidden"

        if action == "assert_url":
            needle = str(step.get("value") or step.get("url") or "")
            current = page.url
            if needle not in current:
                raise AssertionError(f"current url {current!r} does not contain {needle!r}")
            return f"url contains {needle!r}"

        if action == "assert_status":
            expected = int(step.get("value", 200))
            response = await self.context.request.head(self.resolve(str(step.get("url") or "/")))
            if response.status != expected:
                raise AssertionError(f"expected HTTP {expected}, got {response.status}")
            return f"HTTP {response.status}"

        if action == "assert_no_console_errors":
            if self.console_errors:
                raise AssertionError(f"{len(self.console_errors)} console error(s): {self.console_errors[0][:200]}")
            return "no console errors"

        if action == "screenshot":
            name = str(step.get("value") or "screenshot.png")
            path = await self.screenshot(str(self.artifacts_dir / name))
            return f"saved {path}"

        if action == "eval":
            expression = str(step.get("value") or "")
            value = await page.evaluate(expression)
            return f"eval -> {str(value)[:200]}"

        if action == "reload":
            await page.reload(timeout=timeout)
            return "reloaded"

        if action == "scroll":
            await page.mouse.wheel(0, int(step.get("value", 800)))
            return "scrolled"

        if action in {"noop", "note"}:
            return str(step.get("value", ""))

        raise ValueError(f"unknown flow action {action!r} - supported actions are documented in `ua browser flow --help`")

    def _locator(self, step: dict[str, Any]) -> Any:
        selector = step.get("selector") or step.get("target")
        fallback = step.get("or")
        text = step.get("text")
        if selector:
            locator = self.page.locator(str(selector)).first
        elif text:
            locator = self.page.get_by_text(str(text)).first
        elif step.get("role"):
            locator = self.page.get_by_role(str(step["role"]), name=step.get("name")).first
        elif step.get("label"):
            locator = self.page.get_by_label(str(step["label"])).first
        elif step.get("test_id"):
            locator = self.page.get_by_test_id(str(step["test_id"])).first
        else:
            raise ValueError("step needs a selector, text, role, label or test_id")
        if fallback:
            locator = locator.or_(self.page.locator(str(fallback)).first)
        return locator

    async def _safe_screenshot(self, name: str) -> str | None:
        try:
            return await self.screenshot(str(self.artifacts_dir / name))
        except Exception:  # pragma: no cover - page may be closed
            return None

    def _finalise(self, report: FlowReport, started: float) -> FlowReport:
        report.duration_ms = (time.perf_counter() - started) * 1000
        report.console_errors = list(dict.fromkeys(self.console_errors))
        report.console_warnings = list(dict.fromkeys(self.console_warnings))
        report.failed_requests = list(dict.fromkeys(self.failed_requests))
        report.page_errors = list(dict.fromkeys(self.page_errors))
        if report.console_errors and len(report.console_errors) > self.config.console_error_budget:
            report.passed = False
        if report.failed_requests and len(report.failed_requests) > self.config.failed_request_budget:
            report.passed = False
        if report.page_errors:
            report.passed = False
        return report


def _target(step: dict[str, Any]) -> str:
    for key in ("selector", "target", "url", "text", "value"):
        if step.get(key):
            return str(step[key])[:120]
    return ""


__all__ = ["BrowserSession", "FlowReport", "FlowStepResult"]
