"""Host environment detection (Linux-first, cross-platform safe)."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostInfo:
    os_name: str          # "linux" | "darwin" | "windows"
    os_release: str
    distro: str | None
    arch: str
    python_version: str
    python_executable: str
    shell: str | None
    is_wsl: bool
    is_container: bool
    is_root: bool
    cpu_count: int

    @property
    def is_windows(self) -> bool:
        return self.os_name == "windows"

    @property
    def is_posix(self) -> bool:
        return self.os_name in {"linux", "darwin"}

    def as_dict(self) -> dict[str, object]:
        return {
            "os": self.os_name,
            "os_release": self.os_release,
            "distro": self.distro,
            "arch": self.arch,
            "python": self.python_version,
            "python_executable": self.python_executable,
            "shell": self.shell,
            "wsl": self.is_wsl,
            "container": self.is_container,
            "root": self.is_root,
            "cpu_count": self.cpu_count,
        }


def _read_os_release() -> str | None:
    path = "/etc/os-release"
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:  # pragma: no cover - unreadable /etc/os-release
        return None
    return None


def _in_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as handle:
            content = handle.read()
        return any(marker in content for marker in ("docker", "kubepods", "containerd", "lxc"))
    except OSError:
        return False


def detect_host() -> HostInfo:
    system = platform.system().lower()
    os_name = {"darwin": "darwin", "windows": "windows"}.get(system, "linux" if system == "linux" else system)
    release = platform.release()
    return HostInfo(
        os_name=os_name,
        os_release=release,
        distro=_read_os_release(),
        arch=platform.machine() or "unknown",
        python_version=platform.python_version(),
        python_executable=sys.executable,
        shell=detect_shell()[0],
        is_wsl="microsoft" in release.lower() or "WSL_DISTRO_NAME" in os.environ,
        is_container=_in_container(),
        is_root=(os.geteuid() == 0) if hasattr(os, "geteuid") else False,
        cpu_count=os.cpu_count() or 1,
    )


def find_executable(*names: str, extra_dirs: list[str] | None = None) -> str | None:
    """Locate an executable across ``PATH`` plus common per-user install dirs.

    GUI-launched processes and ``pipx``/``uv`` installs frequently miss
    ``~/.local/bin`` on ``PATH``; searching a short, explicit list avoids the
    classic "command not found in CI but works in my shell" failure.
    """

    for name in names:
        found = shutil.which(name)
        if found:
            return found
    candidates: list[str] = list(extra_dirs or [])
    candidates += [
        str(os.path.expanduser("~/.local/bin")),
        "/usr/local/bin",
        "/usr/bin",
        "/opt/homebrew/bin",
        str(os.path.expanduser("~/bin")),
    ]
    for name in names:
        for directory in candidates:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def detect_shell() -> tuple[str | None, str]:
    """Return ``(shell_path, dialect)`` where dialect is bash|zsh|fish|powershell|cmd|sh."""

    if os.name == "nt":
        shell = os.environ.get("COMSPEC") or shutil.which("powershell") or shutil.which("pwsh")
        dialect = "powershell" if shell and ("powershell" in shell or "pwsh" in shell) else "cmd"
        return shell, dialect
    shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh")
    if not shell:
        return None, "sh"
    name = os.path.basename(shell).lower()
    for known in ("bash", "zsh", "fish", "sh", "dash", "ksh", "nu"):
        if known in name:
            return shell, "sh" if known in {"sh", "dash", "ksh"} else known
    return shell, "sh"


def is_ci() -> bool:
    return bool(os.environ.get("CI")) or bool(os.environ.get("GITHUB_ACTIONS"))


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and not is_ci()
