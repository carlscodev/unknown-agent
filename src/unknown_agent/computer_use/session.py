"""The computer-use session: where every safety promise is enforced.

A session wraps one driver and applies, in order, for every action:

1. **enabled?**       [computer_use] enabled = false -> nothing runs, ever
2. **budget**         max_actions_per_task and max_minutes_per_task
3. **host allowlist** the driver may only display allowed hosts
4. **denied keys**    keystrokes on the denylist never reach the driver
5. **confirmation**   per [computer_use] confirm_actions and action mutability
6. **journal**        the action, its outcome and the observation land in
                      ``.unknown/computer-use/<session>.jsonl`` + artifacts

The session is also the *only* public entry point: tools and (later) provider
native computer-use adapters both go through ``perform``/``observe``, so a new
caller cannot bypass the ladder.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from unknown_agent.computer_use.actions import Action, ActionResult, Observation
from unknown_agent.computer_use.driver import Driver
from unknown_agent.config.models import ComputerUseConfig
from unknown_agent.errors import UnknownAgentError
from unknown_agent.observability.logging import get_logger

log = get_logger("computer_use")

__all__ = ["ComputerUseSession", "ComputerUseError"]


class ComputerUseError(UnknownAgentError):
    code = "computer_use_error"
    default_hint = "See [computer_use] in `ua config show computer_use`."


class ComputerUseSession:
    def __init__(
        self,
        config: ComputerUseConfig,
        driver: Driver,
        *,
        journal_dir: Path,
        artifacts_dir: Path,
        prompter: Any = None,
    ) -> None:
        self.config = config
        self.driver = driver
        self.prompter = prompter
        self.session_id = f"cu-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.journal_path = journal_dir / f"{self.session_id}.jsonl"
        self.screenshot_dir = artifacts_dir / self.session_id
        self.actions_used = 0
        self.started_at = time.time()
        self._closed = False

    # -- the ladder ------------------------------------------------------------
    def _check_enabled(self) -> None:
        if not self.config.enabled:
            raise ComputerUseError(
                "Computer use is disabled.",
                hint="Enable it with `ua config set computer_use.enabled true` (understand the risks first).",
            )

    def _check_budget(self) -> None:
        if self.actions_used >= self.config.max_actions_per_task:
            raise ComputerUseError(
                f"Computer-use action budget exhausted ({self.config.max_actions_per_task} actions).",
            )
        elapsed_min = (time.time() - self.started_at) / 60.0
        if elapsed_min > self.config.max_minutes_per_task:
            raise ComputerUseError(
                f"Computer-use time budget exhausted ({self.config.max_minutes_per_task:.0f} minutes).",
            )

    def _check_host(self, action: Action) -> str | None:
        allowed = [host.lower() for host in self.config.allowed_hosts if host.strip()]
        if not allowed:
            return None
        if action.kind == "navigate":
            from urllib.parse import urlparse

            host = (urlparse(action.text).hostname or "").lower()
            # Relative URLs resolve against the session base -> same host, fine.
            if host and not any(host == item or host.endswith("." + item) for item in allowed):
                return f"host {host!r} is not in [computer_use] allowed_hosts"
            return None
        current = self.driver.current_host().lower()
        if current and not any(current == item or current.endswith("." + item) for item in allowed):
            return f"current page host {current!r} is not in [computer_use] allowed_hosts"
        return None

    def _check_keys(self, action: Action) -> str | None:
        if action.kind != "key":
            return None
        pressed = action.text.strip().lower()
        for denied in self.config.denied_keys:
            if denied.strip().lower() and denied.strip().lower() in pressed:
                return f"key {action.text!r} is denied by [computer_use] denied_keys"
        return None

    def _needs_confirmation(self, action: Action) -> bool:
        policy = self.config.confirm_actions
        if policy == "all":
            return action.kind not in {"screenshot", "read_dom"}
        if policy == "mutating":
            return action.mutating
        return False

    def _confirm(self, action: Action) -> bool:
        if self.prompter is None:
            return False  # no way to ask == no permission; never default-allow
        confirm = getattr(self.prompter, "confirm", None)
        if confirm is None:
            return False
        try:
            return bool(confirm(f"computer use: {action.describe()} — allow?", default=False))
        except Exception:  # noqa: BLE001 - a broken prompter must fail closed
            return False

    # -- public operations -------------------------------------------------------
    async def observe(self, *, tag: str = "observe") -> Observation:
        self._check_enabled()
        observation = await self.driver.observe(screenshot_dir=self.screenshot_dir, tag=tag)
        self._journal({"event": "observe", "observation": observation.to_dict()})
        return observation

    async def perform(self, action: Action) -> ActionResult:
        """Run one action through the full ladder.  Never raises for denials."""

        self._check_enabled()
        started = time.perf_counter()

        def denied(reason: str) -> ActionResult:
            result = ActionResult(ok=False, action=action, error=reason, denied=True)
            self._journal({"event": "denied", "action": action.to_dict(), "reason": reason})
            return result

        try:
            self._check_budget()
        except ComputerUseError as exc:
            return denied(exc.message)
        reason = self._check_host(action) or self._check_keys(action)
        if reason:
            return denied(reason)
        if self._needs_confirmation(action) and not self._confirm(action):
            return denied(f"user did not approve: {action.describe()}")

        self.actions_used += 1
        try:
            await self.driver.perform(action)
            observation = await self.driver.observe(
                screenshot_dir=self.screenshot_dir,
                tag=f"{self.actions_used:03d}-{action.kind}",
            ) if self.config.screenshot_every_action or action.kind in {"screenshot", "read_dom"} else None
            result = ActionResult(
                ok=True,
                action=action,
                observation=observation,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - a failed click is data, not a crash
            result = ActionResult(
                ok=False,
                action=action,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        self._journal({"event": "action", "result": result.to_dict()})
        return result

    @property
    def remaining_actions(self) -> int:
        return max(0, self.config.max_actions_per_task - self.actions_used)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._journal({"event": "closed", "actions_used": self.actions_used})
        await self.driver.close()

    # -- journal ---------------------------------------------------------------
    def _journal(self, record: dict[str, Any]) -> None:
        record = {"ts": time.time(), "session": self.session_id, **record}
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:  # pragma: no cover - journalling must not break the action
            log.debug("could not write computer-use journal", exc_info=True)
