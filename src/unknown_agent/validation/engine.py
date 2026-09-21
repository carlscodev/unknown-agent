"""Validation engine: run gates, capture evidence, decide pass/fail.

The engine is the product's integrity layer.  It decides:

* which gates apply (auto-detection, config overrides, per-gate enablement),
* which are *required* (blocking) versus *advisory* (reported),
* how failures map onto the definition of done,
* what evidence is preserved (exit codes, trimmed output, artifacts, hints).

``ua verify`` calls this directly; the agent loop calls it after mutating work,
which is what makes "the agent claims it works" mean something.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.core.models import GateResult, GateStatus, VerificationReport
from unknown_agent.observability.events import Event, EventBus
from unknown_agent.observability.logging import get_logger
from unknown_agent.validation.gates import Gate, GateContext, default_gates

log = get_logger("validation.engine")


@dataclass(slots=True)
class EngineOptions:
    gates: list[str] | None = None
    required_only: bool = False
    fail_fast: bool | None = None
    base_url: str | None = None
    changed_files: list[str] | None = None


class ValidationEngine:
    """Runs the project's quality gates."""

    def __init__(
        self,
        settings: Settings,
        *,
        project_root: Path,
        events: EventBus | None = None,
        run_id: str = "",
        browser: Any = None,
        project: Any = None,
        gates: list[Gate] | None = None,
    ) -> None:
        self.settings = settings
        self.project_root = Path(project_root)
        self.events = events
        self.run_id = run_id
        self.browser = browser
        self.project = project
        self.gates = gates if gates is not None else default_gates(settings)

    # ------------------------------------------------------------------ #
    def select(self, options: EngineOptions) -> list[tuple[Gate, bool]]:
        """Return ``[(gate, required)]`` honouring config and CLI selection."""

        requested = {name.lower() for name in (options.gates or [])}
        selected: list[tuple[Gate, bool]] = []
        for gate in self.gates:
            setting = self.settings.validation.gate(gate.name)
            if requested and gate.name.lower() not in requested and gate.name != "diff":
                continue
            is_required = self.settings.validation.is_required(gate.name, self.settings.agent.definition_of_done)
            if options.required_only and not (is_required or requested):
                continue
            required = bool(is_required or (requested and gate.name.lower() in requested))
            if gate.name == "diff":
                required = False
            selected.append((gate, required))
        return selected

    async def run(
        self,
        *,
        gates: list[str] | None = None,
        required_only: bool = False,
        base_url: str | None = None,
        changed_files: list[str] | None = None,
        fail_fast: bool | None = None,
    ) -> VerificationReport:
        options = EngineOptions(
            gates=gates,
            required_only=required_only,
            base_url=base_url,
            changed_files=changed_files,
            fail_fast=self.settings.validation.fail_fast if fail_fast is None else fail_fast,
        )
        selectable = self.select(options)
        ctx = GateContext(
            settings=self.settings,
            project_root=self.project_root,
            project=self.project,
            browser=self.browser,
            base_url=base_url or self.settings.browser.base_url,
            changed_files=changed_files,
            events=self.events,
            run_id=self.run_id,
        )
        report = VerificationReport()
        started = time.perf_counter()
        self._emit("gate.started", f"running {len(selectable)} gate(s)")

        for gate, required in selectable:
            applies, reason = gate.applicable(ctx)
            if not applies:
                report.results.append(
                    GateResult(name=gate.name, status=GateStatus.SKIPPED, summary=reason, required=required)
                )
                self._emit("gate.finished", f"{gate.name}: skipped ({reason})", gate=gate.name, status="skipped", required=required)
                continue
            self._emit("gate.started", f"{gate.name}: {reason if isinstance(reason, str) else ''}".strip(": "), gate=gate.name, required=required)
            gate_started = time.perf_counter()
            try:
                result = await gate.run(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a gate crash must not kill verification
                log.exception("gate %s crashed", gate.name)
                result = GateResult(
                    name=gate.name,
                    status=GateStatus.ERROR,
                    duration_s=time.perf_counter() - gate_started,
                    summary=f"gate crashed: {type(exc).__name__}: {exc}",
                    fix_hint="This is an Unknown Agent bug or an environment problem; run `ua doctor` and re-run the gate manually.",
                )
            result.required = required
            result.duration_s = result.duration_s or (time.perf_counter() - gate_started)
            report.results.append(result)
            self._emit(
                "gate.finished",
                f"{gate.name}: {result.status.value} ({result.duration_s:.1f}s)",
                gate=gate.name,
                status=result.status.value,
                required=required,
                summary=result.summary,
            )
            if result.status in {GateStatus.FAILED, GateStatus.ERROR} and (options.fail_fast or self.settings.validation.gate(gate.name).fail_fast):
                remaining = [item for item, _ in selectable if item.name not in {entry.name for entry in report.results}]
                for skipped_gate in remaining:
                    report.results.append(
                        GateResult(
                            name=skipped_gate.name,
                            status=GateStatus.SKIPPED,
                            summary="skipped because fail_fast is enabled and an earlier gate failed",
                            required=bool(self.settings.validation.gate(skipped_gate.name).required),
                        )
                    )
                break

        report.results.sort(key=lambda item: _ORDER.get(item.name, 50))
        log.debug("verification finished in %.1fs passed=%s", time.perf_counter() - started, report.passed)
        return report

    # ------------------------------------------------------------------ #
    async def run_async_safe(self, **kwargs: Any) -> VerificationReport:
        """Run gates inside any event loop context (used by sync CLI paths)."""

        return await self.run(**kwargs)

    def _emit(self, event_type: str, message: str, **data: Any) -> None:
        if self.events is None:
            return
        self.events.emit(Event(type=event_type, run_id=self.run_id, message=message, data=data))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    def persist(self, report: VerificationReport, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "passed": report.passed,
            "created_at": report.created_at,
            "results": [
                {**result.to_dict(), "output": result.output[-8000:]}
                for result in report.results
            ],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def summary_lines(self, report: VerificationReport) -> list[str]:
        lines: list[str] = []
        for result in report.results:
            icon = {
                GateStatus.PASSED: "PASS",
                GateStatus.FAILED: "FAIL",
                GateStatus.SKIPPED: "SKIP",
                GateStatus.ERROR: "ERR ",
            }.get(result.status, "??? ")
            scope = "required" if result.required else "advisory"
            lines.append(f"[{icon}] {result.name:<10} {scope:<8} {result.summary}".rstrip())
        return lines


_ORDER = {"syntax": 0, "lint": 1, "format": 2, "typecheck": 3, "tests": 4, "build": 5, "browser": 6, "diff": 7, "custom": 8}

__all__ = ["ValidationEngine", "EngineOptions"]
