"""Install-mode detection and self-management facts.

One question, answered once, consumed everywhere (``ua version``, ``ua
upgrade``, ``ua doctor``, ``ua service``): *how did this copy of Unknown Agent
get onto this machine, and how should it therefore be upgraded, repaired or
removed?*

Recognised modes:

``pipx``        isolated pipx venv (the recommended Linux path)
``uv-tool``     ``uv tool install`` shim
``bootstrap``   the venv the official install.sh creates under
                ``~/.local/share/unknown-agent/venv``
``virtualenv``  a developer's own venv (editable/source installs land here)
``container``   inside Docker/Podman
``system``      global interpreter (works, but discouraged - we say so)

The module also answers *where the launcher lives* and *whether PATH can see
it*, which is exactly what a user needs when ``ua`` prints during install but
"command not found" afterwards.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["InstallInfo", "detect_install", "bootstrap_home", "upgrade_command_for"]

#: Where the official installer keeps its state (venv, receipts, units).
def bootstrap_home() -> Path:
    return Path(os.environ.get("UA_HOME", "")).expanduser() if os.environ.get("UA_HOME") else Path.home() / ".local" / "share" / "unknown-agent"


@dataclass(slots=True)
class InstallInfo:
    mode: str
    python: str
    prefix: str
    #: The ``ua`` launcher that PATH resolves to right now ('' when none does).
    launcher_on_path: str = ""
    #: The launcher this installation *provides* (its scripts dir + 'ua').
    provided_launcher: str = ""
    #: True when the providing scripts dir is missing from PATH.
    path_gap: bool = False
    #: Install receipt written by install.sh, when present.
    receipt: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "python": self.python,
            "prefix": self.prefix,
            "launcher_on_path": self.launcher_on_path,
            "provided_launcher": self.provided_launcher,
            "path_gap": self.path_gap,
            "receipt": dict(self.receipt),
        }


def _read_receipt() -> dict[str, str]:
    receipt = bootstrap_home() / "install-receipt.txt"
    if not receipt.is_file():
        return {}
    facts: dict[str, str] = {}
    try:
        for line in receipt.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                facts[key.strip()] = value.strip()
    except OSError:
        return {}
    return facts


def detect_install() -> InstallInfo:
    executable = sys.executable or ""
    prefix = sys.prefix
    home = str(bootstrap_home())

    if prefix.startswith(home) or executable.startswith(home):
        mode = "bootstrap"
    elif "pipx" in executable or "pipx" in prefix:
        mode = "pipx"
    elif ("uv" in executable and ("tool" in executable or "tools" in executable)) or os.sep + "uv" + os.sep in prefix:
        mode = "uv-tool"
    elif Path("/.dockerenv").exists() or os.environ.get("container"):
        mode = "container"
    elif sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        mode = "virtualenv"
    else:
        mode = "system"

    scripts_dir = Path(sysconfig.get_path("scripts") or "")
    launcher_name = "ua.exe" if os.name == "nt" else "ua"
    provided = scripts_dir / launcher_name
    # pipx/uv expose the shim in ~/.local/bin, not the venv's own bin.
    if mode in {"pipx", "uv-tool", "bootstrap"}:
        shim = Path.home() / ".local" / "bin" / launcher_name
        if shim.exists():
            provided = shim

    on_path = shutil.which("ua") or ""
    path_gap = provided.exists() and not on_path

    return InstallInfo(
        mode=mode,
        python=executable,
        prefix=prefix,
        launcher_on_path=on_path,
        provided_launcher=str(provided) if provided.exists() else "",
        path_gap=path_gap,
        receipt=_read_receipt(),
    )


def upgrade_command_for(info: InstallInfo) -> list[str] | None:
    """The argv that upgrades *this* installation, or None when we honestly cannot know."""

    source = info.receipt.get("source", "")
    git_url = info.receipt.get("git_url", "")
    if info.mode == "pipx":
        target = git_url and f"git+{git_url}" or "unknown-agent"
        if source == "git" and git_url:
            return ["pipx", "install", "--force", f"git+{git_url}"]
        return ["pipx", "upgrade", "unknown-agent"]
    if info.mode == "uv-tool":
        if source == "git" and git_url:
            return ["uv", "tool", "install", "--force", f"git+{git_url}"]
        return ["uv", "tool", "upgrade", "unknown-agent"]
    if info.mode == "bootstrap":
        python = str(bootstrap_home() / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python"))
        target = f"git+{git_url}" if git_url else "unknown-agent"
        return [python, "-m", "pip", "install", "--upgrade", target]
    if info.mode in {"virtualenv", "container", "system"}:
        return [sys.executable, "-m", "pip", "install", "--upgrade", "unknown-agent"]
    return None
