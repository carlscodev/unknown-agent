"""Background test execution with structured failure capture.

A synchronous ``pytest`` call blocks the agent for the length of the suite and
hands back a wall of text it must then re-read to find the three lines that
matter.  On a long mission that is the single largest source of wasted tokens
and wasted wall-clock.

This module fixes both halves:

**Execution is asynchronous.**  :class:`TestSessionRegistry` starts a suite as a
child process and returns immediately with a job id.  The agent keeps working —
reading code, drafting the next edit — and collects the result when it is ready.
Output is drained continuously into a bounded ring buffer, so a runaway suite
printing a gigabyte of logs cannot exhaust memory, and the tail is always
available while the job is still running.

**Failures come back typed, not as prose.**  :func:`parse_pytest_output` turns a
run into :class:`TestFailure` records — test id, file, line, exception type,
message, and just the relevant traceback tail.  The agent receives the three
failures it must fix instead of nine hundred lines in which ``PASSED`` appears
eight hundred times.  Routing them to whoever caused them is then trivial, which
is what :meth:`TestReportTool` exposes via ``group_by``.

Framework detection is by evidence rather than assumption: the runner looks at
what the repository actually contains (``pytest.ini``, a ``test`` script in
``package.json``, ``go.mod``, ``Cargo.toml``) and only falls back to a guess
when nothing is conclusive — and says which it did.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.observability.logging import get_logger
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

log = get_logger("tools.test_runner")

__all__ = [
    "JobState",
    "TestFailure",
    "TestSummary",
    "TestJob",
    "TestSessionRegistry",
    "detect_framework",
    "parse_pytest_output",
    "parse_vitest_output",
    "TestRunBackgroundTool",
    "TestStatusTool",
    "TestReportTool",
    "TestCancelTool",
    "build_tools",
]

#: Keep the last N lines of output per job. Enough for any traceback, bounded
#: so a chatty suite cannot exhaust memory on a month-long mission.
OUTPUT_RING_LINES = 4000
_REGISTRY_KEY = "test_runner:registry"


class JobState(str, Enum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ERROR = "error"
    TIMEOUT = "timeout"

    @property
    def finished(self) -> bool:
        return self is not JobState.RUNNING


# --------------------------------------------------------------------------- #
# structured results
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TestFailure:
    """One failing test, reduced to what is needed to fix it."""

    test_id: str
    file: str = ""
    line: int = 0
    exception_type: str = ""
    message: str = ""
    #: Only the tail of the traceback — the part that names the bug.
    traceback: str = ""
    kind: str = "failure"

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.file and self.line else self.file or "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "file": self.file,
            "line": self.line,
            "location": self.location,
            "exception_type": self.exception_type,
            "message": self.message,
            "traceback": self.traceback,
            "kind": self.kind,
        }

    def digest(self) -> str:
        head = f"{self.test_id} [{self.location}]"
        message = self.message
        # pytest's short summary already prefixes the exception type; repeating
        # it reads as a parsing bug even though it is not.
        prefix = f"{self.exception_type}: "
        if self.exception_type and message.startswith(prefix):
            message = message[len(prefix) :]
        detail = f"{self.exception_type}: {message}" if self.exception_type else message
        return f"{head}\n    {detail}" if detail else head


@dataclass(slots=True)
class TestSummary:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    total: int = 0
    duration_s: float = 0.0
    coverage_pct: float | None = None

    @property
    def green(self) -> bool:
        return self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "xfailed": self.xfailed,
            "total": self.total,
            "duration_s": round(self.duration_s, 2),
            "coverage_pct": self.coverage_pct,
            "green": self.green,
        }

    def line(self) -> str:
        bits = [f"{self.passed} passed"]
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.errors:
            bits.append(f"{self.errors} error(s)")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        if self.coverage_pct is not None:
            bits.append(f"coverage {self.coverage_pct:.1f}%")
        return ", ".join(bits) + f" in {self.duration_s:.1f}s"


# --------------------------------------------------------------------------- #
# framework detection
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Framework:
    name: str
    command: list[str]
    #: How confident detection is, and what it was based on.
    evidence: str
    parser: str = "generic"


def detect_framework(project_root: Path, *, hint: str = "") -> Framework:
    """Work out how this repository runs its tests, from what is on disk."""

    if hint:
        known = {
            "pytest": Framework("pytest", ["python", "-m", "pytest", "-q"], "explicit hint", "pytest"),
            "vitest": Framework("vitest", ["npx", "vitest", "run", "--reporter=verbose"], "explicit hint", "vitest"),
            "jest": Framework("jest", ["npx", "jest", "--ci"], "explicit hint", "vitest"),
            "go": Framework("go test", ["go", "test", "./..."], "explicit hint", "generic"),
            "cargo": Framework("cargo test", ["cargo", "test"], "explicit hint", "generic"),
        }
        if hint.lower() in known:
            return known[hint.lower()]

    pyproject = project_root / "pyproject.toml"
    if (project_root / "pytest.ini").exists() or (project_root / "tox.ini").exists():
        return Framework("pytest", ["python", "-m", "pytest", "-q"], "pytest.ini/tox.ini present", "pytest")
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "[tool.pytest" in text:
            return Framework(
                "pytest", ["python", "-m", "pytest", "-q"], "[tool.pytest] in pyproject.toml", "pytest"
            )

    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        scripts = payload.get("scripts", {}) if isinstance(payload, dict) else {}
        deps = {}
        if isinstance(payload, dict):
            deps = {**payload.get("dependencies", {}), **payload.get("devDependencies", {})}
        if "vitest" in deps:
            return Framework(
                "vitest", ["npx", "vitest", "run", "--reporter=verbose"], "vitest in package.json", "vitest"
            )
        if "jest" in deps:
            return Framework("jest", ["npx", "jest", "--ci"], "jest in package.json", "vitest")
        if isinstance(scripts, dict) and "test" in scripts:
            return Framework("npm test", ["npm", "test", "--silent"], "test script in package.json", "generic")

    if (project_root / "go.mod").exists():
        return Framework("go test", ["go", "test", "./..."], "go.mod present", "generic")
    if (project_root / "Cargo.toml").exists():
        return Framework("cargo test", ["cargo", "test"], "Cargo.toml present", "generic")
    if (project_root / "tests").is_dir() or list(project_root.glob("test_*.py")):
        return Framework("pytest", ["python", "-m", "pytest", "-q"], "tests/ directory present", "pytest")

    return Framework(
        "pytest",
        ["python", "-m", "pytest", "-q"],
        "no conclusive evidence — defaulted to pytest",
        "pytest",
    )


# --------------------------------------------------------------------------- #
# output parsing
# --------------------------------------------------------------------------- #
_PYTEST_COUNTS = re.compile(
    r"(?:(?P<n>\d+)\s+(?P<word>passed|failed|error|errors|skipped|xfailed|xpassed|deselected))"
)
_PYTEST_DURATION = re.compile(r"in\s+(?P<secs>[\d.]+)s")
_PYTEST_FAIL_HEADER = re.compile(r"^_{3,}\s+(?P<test>.+?)\s+_{3,}$")
_PYTEST_ERROR_HEADER = re.compile(r"^_{3,}\s+ERROR\s+(?P<test>.+?)\s+_{3,}$")
_PYTEST_SHORT = re.compile(r"^(?P<status>FAILED|ERROR)\s+(?P<test>\S+)(?:\s+-\s+(?P<msg>.*))?$")
_PYTEST_LOCATION = re.compile(r"^(?P<file>[^\s:]+\.py):(?P<line>\d+):")
_EXC_LINE = re.compile(r"^E\s+(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Failure))?:?\s*(?P<msg>.*)$")
_COVERAGE = re.compile(r"^TOTAL\s+.*?(?P<pct>\d+(?:\.\d+)?)%\s*$", re.MULTILINE)


def parse_pytest_output(text: str) -> tuple[TestSummary, list[TestFailure]]:
    """Turn a pytest run into counts plus one record per failing test."""

    summary = TestSummary()
    lines = text.splitlines()

    # The final status line carries the authoritative counts. Under ``-q`` it is
    # undecorated ("3 failed, 1 passed in 0.30s"), so match on the shape of the
    # line rather than on the "=" banner, which only appears in verbose mode.
    for line in reversed(lines):
        stripped = line.strip()
        if not _PYTEST_COUNTS.search(stripped):
            continue
        if not (_PYTEST_DURATION.search(stripped) or re.match(r"^=+.*=+$", stripped)):
            continue
        for match in _PYTEST_COUNTS.finditer(stripped):
            count = int(match.group("n"))
            word = match.group("word")
            if word == "passed":
                summary.passed = count
            elif word == "failed":
                summary.failed = count
            elif word.startswith("error"):
                summary.errors = count
            elif word == "skipped":
                summary.skipped = count
            elif word in {"xfailed", "xpassed"}:
                summary.xfailed += count
        duration = _PYTEST_DURATION.search(stripped)
        if duration:
            summary.duration_s = float(duration.group("secs"))
        break
    summary.total = summary.passed + summary.failed + summary.errors + summary.skipped

    coverage = _COVERAGE.search(text)
    if coverage:
        summary.coverage_pct = float(coverage.group("pct"))

    # The short summary block is the most reliable list of what failed.
    failures: dict[str, TestFailure] = {}
    for line in lines:
        short = _PYTEST_SHORT.match(line.strip())
        if short:
            test_id = short.group("test")
            failures[test_id] = TestFailure(
                test_id=test_id,
                message=(short.group("msg") or "").strip(),
                kind="error" if short.group("status") == "ERROR" else "failure",
            )

    # Walk the detail sections to attach location and traceback tail.
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        body = "\n".join(buffer).strip()
        record = failures.get(current) or TestFailure(test_id=current)
        tail = body.splitlines()[-40:]
        record.traceback = "\n".join(tail)
        for entry in reversed(body.splitlines()):
            stripped = entry.strip()
            location = _PYTEST_LOCATION.match(stripped)
            if location and not record.file:
                record.file = location.group("file")
                record.line = int(location.group("line"))
            exc = _EXC_LINE.match(stripped)
            if exc and not record.exception_type:
                found = exc.group("type")
                if found:
                    record.exception_type = found
                    if not record.message:
                        record.message = exc.group("msg").strip()
        failures[current] = record

    for line in lines:
        header = _PYTEST_FAIL_HEADER.match(line.strip()) or _PYTEST_ERROR_HEADER.match(line.strip())
        if header:
            flush()
            current = header.group("test").strip()
            buffer = []
            continue
        if current is not None:
            if line.startswith("=") and len(line) > 20:
                flush()
                current = None
                buffer = []
                continue
            buffer.append(line)
    flush()

    # The same test is reported twice: once in the short summary (which carries
    # the full "file::test" id) and once as a detail section header (which
    # carries only the bare name). Merge them so the agent is handed one record
    # per failing test, preferring the id that names the file.
    merged: dict[str, TestFailure] = {}
    for record in failures.values():
        key = _canonical_test_key(record.test_id)
        existing = merged.get(key)
        if existing is None:
            merged[key] = record
            continue
        merged[key] = _merge_failures(existing, record)

    ordered = sorted(merged.values(), key=lambda item: item.test_id)
    if not summary.failed and not summary.errors and ordered:
        summary.failed = sum(1 for item in ordered if item.kind == "failure")
        summary.errors = sum(1 for item in ordered if item.kind == "error")
        summary.total = summary.total or len(ordered)
    return summary, ordered


def _canonical_test_key(test_id: str) -> str:
    """Reduce "tests/x.py::Cls::test_a" and "Cls.test_a" to the same key."""

    tail = test_id.split("::")[-1] if "::" in test_id else test_id
    # A class-qualified detail header uses dots where the node id uses "::".
    return tail.replace("::", ".").strip()


def _merge_failures(first: TestFailure, second: TestFailure) -> TestFailure:
    """Combine two views of one failure, keeping the most specific of each."""

    # The node id containing "::" is the one a developer can paste back into
    # pytest, so it wins as the canonical identifier.
    winner = first if "::" in first.test_id else second
    other = second if winner is first else first
    return TestFailure(
        test_id=winner.test_id,
        file=winner.file or other.file,
        line=winner.line or other.line,
        exception_type=winner.exception_type or other.exception_type,
        message=winner.message or other.message,
        traceback=other.traceback if len(other.traceback) > len(winner.traceback) else winner.traceback,
        kind="error" if "error" in {winner.kind, other.kind} else winner.kind,
    )


_VITEST_FAIL = re.compile(r"^\s*(?:×|✗|FAIL)\s+(?P<test>.+?)(?:\s+\d+ms)?$")
_VITEST_COUNTS = re.compile(r"Tests\s+(?:(?P<failed>\d+)\s+failed\s*\|\s*)?(?P<passed>\d+)\s+passed")
_VITEST_LOCATION = re.compile(r"❯?\s*(?P<file>[\w./\\-]+\.(?:ts|tsx|js|jsx)):(?P<line>\d+)")


def parse_vitest_output(text: str) -> tuple[TestSummary, list[TestFailure]]:
    """Counts and failures for vitest/jest style reporters."""

    summary = TestSummary()
    counts = _VITEST_COUNTS.search(text)
    if counts:
        summary.passed = int(counts.group("passed") or 0)
        summary.failed = int(counts.group("failed") or 0)
    duration = re.search(r"Duration\s+(?P<secs>[\d.]+)s", text)
    if duration:
        summary.duration_s = float(duration.group("secs"))
    summary.total = summary.passed + summary.failed

    failures: list[TestFailure] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _VITEST_FAIL.match(line.rstrip())
        if not match:
            continue
        test_id = match.group("test").strip()
        if not test_id or test_id.lower().startswith("tests"):
            continue
        record = TestFailure(test_id=test_id)
        window = "\n".join(lines[index : index + 25])
        location = _VITEST_LOCATION.search(window)
        if location:
            record.file = location.group("file")
            record.line = int(location.group("line"))
        error = re.search(r"(?:AssertionError|Error|TypeError)[:\s](?P<msg>.+)", window)
        if error:
            record.message = error.group("msg").strip()[:300]
        record.traceback = window[:2000]
        failures.append(record)
    if not summary.failed and failures:
        summary.failed = len(failures)
    return summary, failures


def parse_output(text: str, parser: str) -> tuple[TestSummary, list[TestFailure]]:
    if parser == "pytest":
        return parse_pytest_output(text)
    if parser == "vitest":
        return parse_vitest_output(text)
    # Generic: count nothing, surface the tail so the agent still sees it.
    summary = TestSummary()
    return summary, []


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TestJob:
    """One background suite run."""

    id: str
    command: list[str]
    cwd: str
    framework: str
    parser: str
    state: JobState = JobState.RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    exit_code: int | None = None
    summary: TestSummary = field(default_factory=TestSummary)
    failures: list[TestFailure] = field(default_factory=list)
    error: str = ""
    #: Bounded tail of combined stdout/stderr.
    output: deque[str] = field(default_factory=lambda: deque(maxlen=OUTPUT_RING_LINES))
    truncated_lines: int = 0
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task[None] | None = None

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def text(self) -> str:
        return "\n".join(self.output)

    def to_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "command": " ".join(shlex.quote(part) for part in self.command),
            "cwd": self.cwd,
            "framework": self.framework,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "summary": self.summary.to_dict(),
            "failures": [item.to_dict() for item in self.failures],
            "error": self.error,
            "truncated_lines": self.truncated_lines,
        }
        if include_output:
            payload["output"] = self.text()
        return payload


class TestSessionRegistry:
    """Holds running jobs for the lifetime of a session.

    Kept on :attr:`ToolContext.extra` rather than in a module global so two
    concurrent agent sessions in one process cannot see each other's jobs.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, TestJob] = {}

    @staticmethod
    def for_context(ctx: ToolContext) -> "TestSessionRegistry":
        existing = ctx.extra.get(_REGISTRY_KEY)
        if isinstance(existing, TestSessionRegistry):
            return existing
        registry = TestSessionRegistry()
        ctx.extra[_REGISTRY_KEY] = registry
        return registry

    def add(self, job: TestJob) -> None:
        self._jobs[job.id] = job

    def get(self, job_id: str) -> TestJob | None:
        if job_id in self._jobs:
            return self._jobs[job_id]
        # Bare suffixes are convenient when the agent re-reads its own output.
        matches = [job for key, job in self._jobs.items() if key.endswith(job_id)]
        return matches[0] if len(matches) == 1 else None

    def latest(self) -> TestJob | None:
        if not self._jobs:
            return None
        return max(self._jobs.values(), key=lambda job: job.started_at)

    def all(self) -> list[TestJob]:
        return sorted(self._jobs.values(), key=lambda job: job.started_at, reverse=True)

    def running(self) -> list[TestJob]:
        return [job for job in self._jobs.values() if job.state is JobState.RUNNING]

    async def start(
        self,
        *,
        command: Sequence[str],
        cwd: Path,
        framework: str,
        parser: str,
        timeout_s: float,
        env: dict[str, str] | None = None,
    ) -> TestJob:
        job = TestJob(
            id=f"t-{uuid.uuid4().hex[:8]}",
            command=list(command),
            cwd=str(cwd),
            framework=framework,
            parser=parser,
        )
        merged = {**os.environ, **(env or {})}
        # Deterministic, machine-readable output beats pretty output here.
        merged.setdefault("PYTHONUNBUFFERED", "1")
        merged.setdefault("NO_COLOR", "1")
        merged.setdefault("FORCE_COLOR", "0")
        merged.setdefault("CI", "1")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=merged,
                start_new_session=True,
            )
        except FileNotFoundError:
            job.state = JobState.ERROR
            job.error = f"{command[0]!r} is not installed or not on PATH"
            job.finished_at = time.time()
            self.add(job)
            return job
        except OSError as exc:
            job.state = JobState.ERROR
            job.error = f"could not start the test process: {exc}"
            job.finished_at = time.time()
            self.add(job)
            return job

        job.process = process
        job.task = asyncio.create_task(self._supervise(job, timeout_s))
        self.add(job)
        log.info("test job %s started: %s", job.id, " ".join(command))
        return job

    async def _supervise(self, job: TestJob, timeout_s: float) -> None:
        process = job.process
        if process is None:  # pragma: no cover - guarded by caller
            return
        try:
            await asyncio.wait_for(self._drain(job, process), timeout=timeout_s)
            job.exit_code = await process.wait()
        except asyncio.TimeoutError:
            job.state = JobState.TIMEOUT
            job.error = f"suite exceeded {timeout_s:.0f}s and was terminated"
            await self._terminate(process)
            job.exit_code = -1
        except asyncio.CancelledError:
            job.state = JobState.CANCELLED
            await self._terminate(process)
            raise
        except Exception as exc:  # noqa: BLE001 - supervision must never propagate
            job.state = JobState.ERROR
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = time.time()
            text = job.text()
            summary, failures = parse_output(text, job.parser)
            if not summary.duration_s:
                summary.duration_s = job.duration_s
            job.summary = summary
            job.failures = failures
            if job.state is JobState.RUNNING:
                if job.exit_code == 0:
                    job.state = JobState.PASSED
                elif failures or summary.failed or summary.errors:
                    job.state = JobState.FAILED
                else:
                    job.state = JobState.FAILED if job.exit_code else JobState.PASSED
            log.info("test job %s -> %s (%s)", job.id, job.state.value, summary.line())

    @staticmethod
    async def _drain(job: TestJob, process: asyncio.subprocess.Process) -> None:
        stream = process.stdout
        if stream is None:  # pragma: no cover - PIPE is always set
            return
        while True:
            raw = await stream.readline()
            if not raw:
                break
            if len(job.output) == job.output.maxlen:
                job.truncated_lines += 1
            job.output.append(raw.decode("utf-8", errors="replace").rstrip("\n"))

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """Kill the whole process group — pytest spawns children."""

        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(process.pid), 15)
            else:  # pragma: no cover - Windows
                process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(process.pid), 9)
                else:  # pragma: no cover - Windows
                    process.kill()

    async def cancel(self, job: TestJob) -> bool:
        if job.state.finished:
            return False
        job.state = JobState.CANCELLED
        if job.task is not None:
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
        elif job.process is not None:
            await self._terminate(job.process)
        job.finished_at = job.finished_at or time.time()
        return True


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def _resolve_root(ctx: ToolContext) -> Path:
    return ctx.project_root


class TestRunBackgroundTool(Tool):
    name = "test_run_bg"
    description = (
        "Start the test suite in the background and return a job id immediately, so you can keep "
        "working while it runs. Detects the framework from the repository. Poll with test_status "
        "and read structured failures with test_report."
    )
    category = "validation"
    risk = RiskLevel.EXECUTE
    tags = ("test", "async", "primary")
    timeout_s = 60.0
    parameters = (
        ParameterSpec("target", "string", "Path, test id, or -k expression to narrow the run.", default=""),
        ParameterSpec("framework", "string", "Force a framework: pytest, vitest, jest, go, cargo.", default=""),
        ParameterSpec("extra_args", "string", "Extra CLI arguments, shell-quoted.", default=""),
        ParameterSpec("timeout_s", "number", "Kill the suite after this many seconds.", default=1800.0),
        ParameterSpec("wait", "boolean", "Block until the suite finishes instead of returning a job id.", default=False),
    )
    examples = (
        "ua tools call test_run_bg",
        "ua tools call test_run_bg --arg target=tests/test_engine.py",
    )

    async def run(
        self,
        ctx: ToolContext,
        target: str = "",
        framework: str = "",
        extra_args: str = "",
        timeout_s: float = 300.0,
        wait: bool = False,
    ) -> ToolResult:
        root = _resolve_root(ctx)
        detected = detect_framework(root, hint=framework)
        command = list(detected.command)
        if target:
            command.append(target)
        if extra_args:
            try:
                command.extend(shlex.split(extra_args))
            except ValueError as exc:
                return ToolResult.failure(f"extra_args could not be parsed: {exc}")

        registry = TestSessionRegistry.for_context(ctx)
        job = await registry.start(
            command=command,
            cwd=root,
            framework=detected.name,
            parser=detected.parser,
            timeout_s=max(5.0, float(timeout_s)),
        )
        if job.state is JobState.ERROR:
            return ToolResult.failure(job.error, output=f"command: {' '.join(command)}")

        ctx.emit(
            "test.started",
            f"{detected.name} started ({job.id})",
            job_id=job.id,
            command=command,
        )

        if wait and job.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
            return _render_job(job, include_failures=True)

        return ToolResult.success(
            f"started {detected.name} as job {job.id}\n"
            f"  command : {' '.join(shlex.quote(part) for part in command)}\n"
            f"  detected: {detected.evidence}\n"
            f"  timeout : {timeout_s:.0f}s\n"
            f"Keep working; call test_status with job_id={job.id} when you need the result.",
            job_id=job.id,
            command=command,
            framework=detected.name,
            evidence=detected.evidence,
        )


class TestStatusTool(Tool):
    name = "test_status"
    description = (
        "Check a background test job: still running, passed, or failed, with counts. "
        "Omit job_id for the most recent job."
    )
    category = "validation"
    risk = RiskLevel.READ
    tags = ("test", "async")
    parameters = (
        ParameterSpec("job_id", "string", "Job to inspect (default: the latest).", default=""),
        ParameterSpec("wait", "boolean", "Block until the job finishes.", default=False),
        ParameterSpec("wait_timeout_s", "number", "Give up waiting after this long.", default=600.0),
        ParameterSpec("tail_lines", "integer", "Lines of raw output to include.", default=0),
    )

    async def run(
        self,
        ctx: ToolContext,
        job_id: str = "",
        wait: bool = False,
        wait_timeout_s: float = 300.0,
        tail_lines: int = 0,
    ) -> ToolResult:
        registry = TestSessionRegistry.for_context(ctx)
        job = registry.get(job_id) if job_id else registry.latest()
        if job is None:
            known = ", ".join(item.id for item in registry.all()) or "none"
            return ToolResult.failure(f"no such test job {job_id!r}. Known jobs: {known}")

        if wait and job.task is not None and not job.state.finished:
            try:
                await asyncio.wait_for(asyncio.shield(job.task), timeout=max(1.0, wait_timeout_s))
            except asyncio.TimeoutError:
                return ToolResult.success(
                    f"job {job.id} is still running after {wait_timeout_s:.0f}s of waiting "
                    f"({job.duration_s:.0f}s elapsed)\n" + _tail(job, tail_lines or 20),
                    **job.to_dict(),
                )

        return _render_job(job, include_failures=job.state is JobState.FAILED, tail_lines=tail_lines)


class TestReportTool(Tool):
    name = "test_report"
    description = (
        "Structured failures from a finished test job: test id, file and line, exception and the "
        "relevant traceback tail — not the whole log. Group by file to route each failure to "
        "whoever owns that code."
    )
    category = "validation"
    risk = RiskLevel.READ
    tags = ("test", "async", "primary")
    parameters = (
        ParameterSpec("job_id", "string", "Job to report on (default: the latest).", default=""),
        ParameterSpec("limit", "integer", "Maximum failures to include.", default=10),
        ParameterSpec("group_by", "string", "Group failures: none or file.", default="none", enum=("none", "file")),
        ParameterSpec("include_traceback", "boolean", "Include the traceback tail per failure.", default=True),
    )

    async def run(
        self,
        ctx: ToolContext,
        job_id: str = "",
        limit: int = 10,
        group_by: str = "none",
        include_traceback: bool = True,
    ) -> ToolResult:
        registry = TestSessionRegistry.for_context(ctx)
        job = registry.get(job_id) if job_id else registry.latest()
        if job is None:
            return ToolResult.failure("no test job has been started in this session")
        if not job.state.finished:
            return ToolResult.success(
                f"job {job.id} is still running ({job.duration_s:.0f}s). "
                f"Call test_status with wait=true to block until it finishes.",
                **job.to_dict(),
            )
        if not job.failures:
            return ToolResult.success(
                f"job {job.id} ({job.framework}): {job.summary.line()} — nothing to report",
                **job.to_dict(),
            )

        selected = job.failures[: max(1, limit)]
        lines = [
            f"{job.framework} job {job.id}: {job.summary.line()}",
            f"showing {len(selected)} of {len(job.failures)} failure(s)",
            "",
        ]
        if group_by == "file":
            grouped: dict[str, list[TestFailure]] = {}
            for failure in selected:
                grouped.setdefault(failure.file or "unknown", []).append(failure)
            for file_name, items in sorted(grouped.items()):
                lines.append(f"### {file_name} ({len(items)} failure(s))")
                for failure in items:
                    lines.append(f"  - {failure.digest()}")
                    if include_traceback and failure.traceback:
                        body = "\n".join(f"      {row}" for row in failure.traceback.splitlines()[-12:])
                        lines.append(body)
                lines.append("")
        else:
            for failure in selected:
                lines.append(failure.digest())
                if include_traceback and failure.traceback:
                    lines.append("\n".join(f"    {row}" for row in failure.traceback.splitlines()[-15:]))
                lines.append("")

        return ToolResult(
            ok=True,
            output="\n".join(lines).rstrip(),
            data={
                **job.to_dict(),
                "grouped_by": group_by,
                "files_affected": sorted({item.file for item in job.failures if item.file}),
            },
        )


class TestCancelTool(Tool):
    name = "test_cancel"
    description = "Stop a running background test job and its child processes."
    category = "validation"
    risk = RiskLevel.EXECUTE
    tags = ("test", "async")
    parameters = (
        ParameterSpec("job_id", "string", "Job to cancel (default: all running).", default=""),
    )

    async def run(self, ctx: ToolContext, job_id: str = "") -> ToolResult:
        registry = TestSessionRegistry.for_context(ctx)
        targets = []
        if job_id:
            job = registry.get(job_id)
            if job is None:
                return ToolResult.failure(f"no such test job {job_id!r}")
            targets = [job]
        else:
            targets = registry.running()
        if not targets:
            return ToolResult.success("no running test jobs")

        cancelled = [job.id for job in targets if await registry.cancel(job)]
        return ToolResult.success(
            f"cancelled {len(cancelled)} job(s): {', '.join(cancelled) or 'none'}", cancelled=cancelled
        )


def _tail(job: TestJob, lines: int) -> str:
    if lines <= 0:
        return ""
    rows = list(job.output)[-lines:]
    if not rows:
        return ""
    prefix = f"(last {len(rows)} line(s)"
    if job.truncated_lines:
        prefix += f", {job.truncated_lines} earlier line(s) dropped"
    return prefix + ")\n" + "\n".join(rows)


def _render_job(job: TestJob, *, include_failures: bool = False, tail_lines: int = 0) -> ToolResult:
    icon = {
        JobState.RUNNING: "RUNNING",
        JobState.PASSED: "PASS",
        JobState.FAILED: "FAIL",
        JobState.CANCELLED: "CANCELLED",
        JobState.ERROR: "ERROR",
        JobState.TIMEOUT: "TIMEOUT",
    }[job.state]

    head = f"[{icon}] {job.framework} job {job.id} — {job.summary.line()}"
    body = [head]
    if job.error:
        body.append(f"  error: {job.error}")
    if job.exit_code is not None:
        body.append(f"  exit code: {job.exit_code}")

    if include_failures and job.failures:
        body.append("")
        body.append(f"{len(job.failures)} failing test(s):")
        for failure in job.failures[:10]:
            body.append(f"  - {failure.digest()}")
        if len(job.failures) > 10:
            body.append(f"  ... and {len(job.failures) - 10} more (call test_report)")

    tail = _tail(job, tail_lines)
    if tail:
        body.append("")
        body.append(tail)

    return ToolResult(
        ok=job.state in {JobState.RUNNING, JobState.PASSED},
        output="\n".join(body),
        error=None if job.state in {JobState.RUNNING, JobState.PASSED} else (job.error or f"{job.summary.failed} test(s) failing"),
        data=job.to_dict(),
    )


def build_tools() -> list[Tool]:
    return [TestRunBackgroundTool(), TestStatusTool(), TestReportTool(), TestCancelTool()]
