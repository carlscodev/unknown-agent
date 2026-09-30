"""Reproducible subprocess benchmarks with durable regression analysis."""

from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from unknown_agent.config.secrets import redact_text

_OUTPUT_LIMIT = 1_048_576


class BenchmarkError(RuntimeError):
    """Benchmark execution or comparison failed."""


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    command: tuple[str, ...] = Field(min_length=1, max_length=64)
    repetitions: int = Field(default=3, ge=1, le=20)
    warmups: int = Field(default=1, ge=0, le=5)
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    expected_exit_code: int = Field(default=0, ge=0, le=255)
    stdout_contains: tuple[str, ...] = Field(default=(), max_length=32)
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def command_parts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part or "\x00" in part for part in value):
            raise ValueError("command arguments cannot be empty or contain NUL")
        return value

    @field_validator("environment")
    @classmethod
    def bounded_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 64 or any(
            len(key) > 128 or len(item) > 4096 for key, item in value.items()
        ):
            raise ValueError("benchmark environment exceeds limits")
        return value


class BenchmarkSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    scenarios: tuple[Scenario, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_names(self) -> BenchmarkSuite:
        names = [item.name for item in self.scenarios]
        if len(names) != len(set(names)):
            raise ValueError("scenario names must be unique")
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> BenchmarkSuite:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("benchmark suite must be a mapping")
        return cls.model_validate(payload)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scenario: str
    iteration: int
    duration_seconds: float = Field(ge=0)
    exit_code: int
    passed: bool
    timed_out: bool = False
    stdout: str = Field(max_length=_OUTPUT_LIMIT)
    stderr: str = Field(max_length=_OUTPUT_LIMIT)


class ScenarioSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scenario: str
    samples: int
    passed: int
    pass_rate: float
    median_seconds: float
    p95_seconds: float


class BenchmarkRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    suite: str
    revision: str
    started_at: datetime
    finished_at: datetime
    summaries: tuple[ScenarioSummary, ...]


class Regression(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scenario: str
    metric: Literal["median_seconds", "p95_seconds", "pass_rate"]
    baseline: float
    candidate: float
    change_percent: float
    regressed: bool


class RegressionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    baseline_run: str
    candidate_run: str
    threshold_percent: float
    regressions: tuple[Regression, ...]
    passed: bool


class BenchmarkRunner:
    def __init__(self, root: Path, database: Path) -> None:
        self.root = root.resolve()
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS benchmark_runs(
                  id TEXT PRIMARY KEY,suite TEXT NOT NULL,revision TEXT NOT NULL,
                  started_at TEXT NOT NULL,finished_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS benchmark_samples(
                  run_id TEXT NOT NULL,scenario TEXT NOT NULL,iteration INTEGER NOT NULL,
                  duration_seconds REAL NOT NULL,exit_code INTEGER NOT NULL,passed INTEGER NOT NULL,
                  timed_out INTEGER NOT NULL,stdout TEXT NOT NULL,stderr TEXT NOT NULL,
                  PRIMARY KEY(run_id,scenario,iteration));
                """
            )

    async def run(self, suite: BenchmarkSuite, *, revision: str = "unknown") -> BenchmarkRun:
        run_id = str(uuid.uuid4())
        started = datetime.now(UTC)
        samples: list[Sample] = []
        for scenario in suite.scenarios:
            for _ in range(scenario.warmups):
                await self._execute(scenario, iteration=-1)
            for iteration in range(scenario.repetitions):
                samples.append(await self._execute(scenario, iteration=iteration))
        finished = datetime.now(UTC)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO benchmark_runs VALUES(?,?,?,?,?)",
                (run_id, suite.name, revision, started.isoformat(), finished.isoformat()),
            )
            db.executemany(
                "INSERT INTO benchmark_samples VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        item.scenario,
                        item.iteration,
                        item.duration_seconds,
                        item.exit_code,
                        int(item.passed),
                        int(item.timed_out),
                        item.stdout,
                        item.stderr,
                    )
                    for item in samples
                ],
            )
        return BenchmarkRun(
            id=run_id,
            suite=suite.name,
            revision=revision,
            started_at=started,
            finished_at=finished,
            summaries=tuple(self._summaries(samples)),
        )

    def get_run(self, run_id: str) -> BenchmarkRun:
        with self._connect() as db:
            run = db.execute("SELECT * FROM benchmark_runs WHERE id=?", (run_id,)).fetchone()
            rows = db.execute(
                "SELECT * FROM benchmark_samples WHERE run_id=? ORDER BY scenario,iteration",
                (run_id,),
            ).fetchall()
        if run is None:
            raise BenchmarkError("benchmark run not found")
        samples = [
            Sample(
                scenario=row["scenario"],
                iteration=row["iteration"],
                duration_seconds=row["duration_seconds"],
                exit_code=row["exit_code"],
                passed=bool(row["passed"]),
                timed_out=bool(row["timed_out"]),
                stdout=row["stdout"],
                stderr=row["stderr"],
            )
            for row in rows
        ]
        return BenchmarkRun(
            id=run["id"],
            suite=run["suite"],
            revision=run["revision"],
            started_at=datetime.fromisoformat(run["started_at"]),
            finished_at=datetime.fromisoformat(run["finished_at"]),
            summaries=tuple(self._summaries(samples)),
        )

    def compare(
        self, baseline_run: str, candidate_run: str, *, threshold_percent: float = 10
    ) -> RegressionReport:
        if not 0 <= threshold_percent <= 1000:
            raise ValueError("regression threshold must be between 0 and 1000 percent")
        baseline = self.get_run(baseline_run)
        candidate = self.get_run(candidate_run)
        if baseline.suite != candidate.suite:
            raise BenchmarkError("benchmark suites do not match")
        baseline_values = {item.scenario: item for item in baseline.summaries}
        candidate_values = {item.scenario: item for item in candidate.summaries}
        if baseline_values.keys() != candidate_values.keys():
            raise BenchmarkError("benchmark scenarios do not match")
        values: list[Regression] = []
        for name in sorted(baseline_values):
            old, new = baseline_values[name], candidate_values[name]
            for metric in ("median_seconds", "p95_seconds"):
                before, after = getattr(old, metric), getattr(new, metric)
                change = _percent(before, after)
                values.append(
                    Regression(
                        scenario=name,
                        metric=metric,
                        baseline=before,
                        candidate=after,
                        change_percent=change,
                        regressed=change > threshold_percent,
                    )
                )
            pass_change = (new.pass_rate - old.pass_rate) * 100
            values.append(
                Regression(
                    scenario=name,
                    metric="pass_rate",
                    baseline=old.pass_rate,
                    candidate=new.pass_rate,
                    change_percent=pass_change,
                    regressed=pass_change < -threshold_percent,
                )
            )
        return RegressionReport(
            baseline_run=baseline_run,
            candidate_run=candidate_run,
            threshold_percent=threshold_percent,
            regressions=tuple(values),
            passed=not any(item.regressed for item in values),
        )

    async def _execute(self, scenario: Scenario, *, iteration: int) -> Sample:
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "SYSTEMROOT")
            if key in os.environ
        }
        environment.update(scenario.environment)
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *scenario.command,
            cwd=self.root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        stdout_task = asyncio.create_task(_read_limited(process.stdout))
        stderr_task = asyncio.create_task(_read_limited(process.stderr))
        try:
            await asyncio.wait_for(process.wait(), timeout=scenario.timeout_seconds)
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task), timeout=min(5, scenario.timeout_seconds)
            )
        except TimeoutError:
            stdout_task.cancel()
            stderr_task.cancel()
            stdout_raw, stderr_raw = b"", b"output stream did not close after process exit"
        duration = time.perf_counter() - started
        stdout = redact_text(stdout_raw.decode(errors="replace"))
        stderr = redact_text(stderr_raw.decode(errors="replace"))
        exit_code = process.returncode if process.returncode is not None else -1
        passed = (
            not timed_out
            and exit_code == scenario.expected_exit_code
            and all(value in stdout for value in scenario.stdout_contains)
        )
        return Sample(
            scenario=scenario.name,
            iteration=iteration,
            duration_seconds=duration,
            exit_code=exit_code,
            passed=passed,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def _summaries(samples: list[Sample]) -> list[ScenarioSummary]:
        grouped: dict[str, list[Sample]] = {}
        for sample in samples:
            grouped.setdefault(sample.scenario, []).append(sample)
        summaries: list[ScenarioSummary] = []
        for name in sorted(grouped):
            items = grouped[name]
            durations = sorted(item.duration_seconds for item in items)
            rank = max(0, math.ceil(len(durations) * 0.95) - 1)
            passed = sum(item.passed for item in items)
            summaries.append(
                ScenarioSummary(
                    scenario=name,
                    samples=len(items),
                    passed=passed,
                    pass_rate=passed / len(items),
                    median_seconds=statistics.median(durations),
                    p95_seconds=durations[rank],
                )
            )
        return summaries

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database, timeout=30)
        database.row_factory = sqlite3.Row
        return database


async def _read_limited(reader: asyncio.StreamReader | None) -> bytes:
    if reader is None:
        return b""
    retained = bytearray()
    while block := await reader.read(64 * 1024):
        remaining = _OUTPUT_LIMIT - len(retained)
        if remaining > 0:
            retained.extend(block[:remaining])
    return bytes(retained)


def _percent(before: float, after: float) -> float:
    if before == 0:
        return 0 if after == 0 else math.inf
    return ((after - before) / before) * 100
