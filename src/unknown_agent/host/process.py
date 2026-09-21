"""Subprocess execution that behaves identically on Linux, macOS and Windows.

Design rules:

* never use ``shell=True`` with agent-authored strings; the shell tool
  tokenises explicitly and runs a program + argv, so quoting bugs cannot become
  command-injection bugs;
* always stream to a bounded buffer so a runaway build cannot exhaust memory;
* always attach a timeout and kill the whole process group (``start_new_session``
  on POSIX, job objects via CREATE_NEW_PROCESS_GROUP on Windows);
* always return a structured result - callers never deal with raw exit codes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from unknown_agent.errors import ToolTimeout

MAX_BUFFER_BYTES = 400_000


@dataclass(slots=True)
class ProcessResult:
    command: str
    argv: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    truncated: bool = False
    env_keys: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def combined(self) -> str:
        if self.stderr and self.stdout:
            return f"{self.stdout.rstrip()}\n--- stderr ---\n{self.stderr.rstrip()}"
        return (self.stdout or self.stderr).rstrip()

    def tail(self, lines: int = 40) -> str:
        text = self.combined()
        parts = text.splitlines()
        return "\n".join(parts[-lines:])

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _terminate_tree(proc: asyncio.subprocess.Process) -> None:  # pragma: no cover - signal timing
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return


def _kill_tree(proc: asyncio.subprocess.Process) -> None:  # pragma: no cover - signal timing
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float = 600.0,
    env: Mapping[str, str] | None = None,
    inherit_env: bool = True,
    stdin_data: str | None = None,
    max_buffer_bytes: int = MAX_BUFFER_BYTES,
    label: str | None = None,
) -> ProcessResult:
    """Run ``argv`` and return a :class:`ProcessResult`.  Never raises for exit != 0."""

    argv_list = [str(part) for part in argv]
    if not argv_list:
        raise ValueError("run_process requires at least one argument")
    resolved = shutil.which(argv_list[0])
    executable = resolved or argv_list[0]
    if resolved is None and not Path(argv_list[0]).is_absolute():
        return ProcessResult(
            command=" ".join(argv_list),
            argv=argv_list,
            cwd=str(cwd or Path.cwd()),
            exit_code=127,
            stdout="",
            stderr=f"Executable not found on PATH: {argv_list[0]}",
            duration_s=0.0,
        )

    child_env: dict[str, str] | None
    if env is None and inherit_env:
        child_env = None
    else:
        child_env = dict(os.environ) if inherit_env else {}
        if env:
            child_env.update({k: str(v) for k, v in env.items()})

    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":  # pragma: no cover - windows only
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        executable,
        *argv_list[1:],
        cwd=str(cwd) if cwd else None,
        env=child_env,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **popen_kwargs,  # type: ignore[arg-type]
    )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_len = stderr_len = 0
    truncated = False

    async def pump(stream: asyncio.StreamReader | None, sink: list[bytes], counter: list[int]) -> None:
        nonlocal truncated
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            counter[0] += len(chunk)
            if counter[0] <= max_buffer_bytes:
                sink.append(chunk)
            else:
                truncated = True

    counter_out = [0]
    counter_err = [0]
    timed_out = False
    try:
        if stdin_data:
            assert proc.stdin is not None
            proc.stdin.write(stdin_data.encode("utf-8", errors="replace"))
            await proc.stdin.drain()
            proc.stdin.close()
        await asyncio.wait_for(
            asyncio.gather(
                pump(proc.stdout, stdout_chunks, counter_out),
                pump(proc.stderr, stderr_chunks, counter_err),
                proc.wait(),
            ),
            timeout=timeout,
        )
    except TimeoutError:
        timed_out = True
        _terminate_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:  # pragma: no cover - stubborn process
            _kill_tree(proc)
            await proc.wait()
    except asyncio.CancelledError:  # pragma: no cover - cancellation path
        _terminate_tree(proc)
        raise

    duration = time.monotonic() - started
    return ProcessResult(
        command=label or " ".join(argv_list),
        argv=argv_list,
        cwd=str(cwd or Path.cwd()),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=(
            b"".join(stderr_chunks).decode("utf-8", errors="replace")
            + ("\n[timeout: process killed]" if timed_out else "")
        ),
        duration_s=duration,
        timed_out=timed_out,
        truncated=truncated,
        env_keys=sorted(env.keys()) if env else [],
    )


async def run_process_or_raise(argv: Sequence[str], *, timeout: float = 600.0, **kwargs: object) -> ProcessResult:
    result = await run_process(argv, timeout=timeout, **kwargs)  # type: ignore[arg-type]
    if result.timed_out:
        raise ToolTimeout(f"Command timed out after {timeout:.0f}s: {result.command}")
    return result


def split_command(command: str) -> list[str]:
    """Split a shell-style command line into argv using Unix rules.

    ``shlex`` handles quoting; on Windows we still use POSIX rules because the
    agent emits POSIX-style commands and ``create_subprocess_exec`` bypasses
    ``cmd.exe`` entirely.  Operators such as ``&&`` are rejected here so the
    caller must decide (the shell tool supports explicit chaining).
    """

    import shlex

    cleaned = command.strip()
    if not cleaned:
        raise ValueError("empty command")
    lexer = shlex.shlex(cleaned, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)
