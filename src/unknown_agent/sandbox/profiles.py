"""Native sandbox command construction with explicit fail-open/fail-closed modes."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SandboxProfile = Literal["trusted", "workspace", "isolated"]
SandboxMode = Literal["off", "auto", "required"]


class SandboxError(RuntimeError):
    """Raised when a required sandbox cannot be established."""


class SandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: SandboxMode = "auto"
    profile: SandboxProfile = "workspace"
    workspace: Path
    cwd: Path
    allow_network: bool = False
    writable_paths: list[Path] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def paths_are_bounded(self) -> SandboxRequest:
        root = self.workspace.expanduser().resolve()
        current = self.cwd.expanduser().resolve()
        if not current.is_relative_to(root):
            raise ValueError("sandbox cwd must be inside the workspace")
        for path in self.writable_paths:
            if not path.expanduser().resolve().is_relative_to(root):
                raise ValueError("sandbox writable paths must be inside the workspace")
        return self


class SandboxStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: str
    backend: str | None
    available: bool
    detail: str


class SandboxDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: list[str]
    backend: str | None
    profile: SandboxProfile
    enforced: bool
    network_allowed: bool
    detail: str


def sandbox_status(*, system: str | None = None, which: object = shutil.which) -> SandboxStatus:
    current = (system or platform.system()).lower()
    lookup = which  # injection seam for deterministic platform tests
    if current == "linux":
        binary = lookup("bwrap")  # type: ignore[operator]
        return SandboxStatus(
            platform="linux",
            backend="bubblewrap" if binary else None,
            available=bool(binary),
            detail="bubblewrap available" if binary else "install bubblewrap (bwrap)",
        )
    if current == "darwin":
        binary = lookup("sandbox-exec")  # type: ignore[operator]
        return SandboxStatus(
            platform="darwin",
            backend="seatbelt" if binary else None,
            available=bool(binary),
            detail="sandbox-exec available" if binary else "sandbox-exec is unavailable",
        )
    if current == "windows":
        # A restricted-token/job-object broker requires a native helper. Running
        # without it is reported explicitly and `required` fails closed.
        binary = lookup("unknown-agent-sandbox.exe")  # type: ignore[operator]
        return SandboxStatus(
            platform="windows",
            backend="restricted-token" if binary else None,
            available=bool(binary),
            detail=(
                "restricted-token helper available"
                if binary
                else "unknown-agent-sandbox.exe is not installed"
            ),
        )
    return SandboxStatus(
        platform=current or os.name,
        backend=None,
        available=False,
        detail="no supported native sandbox backend",
    )


def build_sandbox_command(
    argv: list[str],
    request: SandboxRequest,
    *,
    system: str | None = None,
    which: object = shutil.which,
) -> SandboxDecision:
    if not argv:
        raise ValueError("sandbox command must not be empty")
    if request.mode == "off" or request.profile == "trusted":
        return SandboxDecision(
            argv=argv,
            backend=None,
            profile=request.profile,
            enforced=False,
            network_allowed=True,
            detail="sandbox disabled or trusted profile selected",
        )
    status = sandbox_status(system=system, which=which)
    if not status.available:
        if request.mode == "required":
            raise SandboxError(f"required sandbox unavailable: {status.detail}")
        return SandboxDecision(
            argv=argv,
            backend=None,
            profile=request.profile,
            enforced=False,
            network_allowed=request.allow_network,
            detail=f"sandbox unavailable in auto mode: {status.detail}",
        )

    root = request.workspace.expanduser().resolve()
    cwd = request.cwd.expanduser().resolve()
    writable = sorted({root, *(path.expanduser().resolve() for path in request.writable_paths)})
    if status.backend == "bubblewrap":
        binary = str(which("bwrap"))  # type: ignore[operator]
        command = [
            binary,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        if not request.allow_network or request.profile == "isolated":
            command.append("--unshare-net")
        for path in writable:
            command.extend(["--bind", str(path), str(path)])
        command.extend(["--chdir", str(cwd), "--", *argv])
    elif status.backend == "seatbelt":
        binary = str(which("sandbox-exec"))  # type: ignore[operator]
        rules = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow sysctl-read)",
            "(allow file-read*)",
        ]
        for path in writable:
            escaped = str(path).replace('"', '\\"')
            rules.append(f'(allow file-write* (subpath "{escaped}"))')
        rules.append("(allow network*)" if request.allow_network else "(deny network*)")
        command = [binary, "-p", " ".join(rules), *argv]
    else:
        binary = str(which("unknown-agent-sandbox.exe"))  # type: ignore[operator]
        command = [
            binary,
            "--workspace",
            str(root),
            "--cwd",
            str(cwd),
            "--network",
            "allow" if request.allow_network else "deny",
            "--",
            *argv,
        ]
    return SandboxDecision(
        argv=command,
        backend=status.backend,
        profile=request.profile,
        enforced=True,
        network_allowed=request.allow_network and request.profile != "isolated",
        detail=f"enforced by {status.backend}",
    )
