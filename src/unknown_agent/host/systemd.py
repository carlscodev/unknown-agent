"""User-level systemd integration.

Unknown Agent is a CLI product, not a daemon - and we do not pretend
otherwise.  The one background job that genuinely earns its place is
**maintenance**: pruning old run journals and session indexes, enforcing
memory retention, rotating logs, and caching an update check so the CLI can
say "update available" without a network call at startup.

Design rules:

* **user units only** (``systemctl --user``): no root, no system-wide state,
  removable with one command.  The unit lives in
  ``~/.config/systemd/user/`` per the systemd.unit search path.
* the unit executes a *stable CLI verb* (``ua service run-maintenance``), so
  the unit file never encodes internal paths beyond the launcher.
* a **timer**, not a long-running service: the job runs weekly and exits.
* linger is *not* enabled by default; a timer that only matters while the
  user actually uses the machine does not justify ``loginctl enable-linger``.

The registry pattern keeps this extensible: a future dashboard daemon adds a
:class:`UnitSpec` here and ``ua service`` learns it with zero new plumbing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["UnitSpec", "UNITS", "systemd_available", "unit_dir", "install_unit", "remove_unit", "unit_status", "systemctl_user"]


@dataclass(frozen=True, slots=True)
class UnitSpec:
    key: str
    description: str
    #: Why a user would install this - shown by ``ua service list``.
    rationale: str
    service_name: str
    timer_name: str | None
    #: Rendered with format(launcher=...).
    service_body: str
    timer_body: str | None = None


_MAINTENANCE_SERVICE = """\
[Unit]
Description=Unknown Agent maintenance (prune journals, rotate logs, cache update check)
Documentation=https://github.com/{repo}
# Network is optional: the update check degrades gracefully offline.
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={launcher} --non-interactive --quiet service run-maintenance
# Maintenance is IO-light housekeeping; keep it out of the user's way.
Nice=10
IOSchedulingClass=idle
# Hardening that still fits a user unit touching only XDG state/config.
NoNewPrivileges=true
PrivateTmp=true
"""

_MAINTENANCE_TIMER = """\
[Unit]
Description=Weekly Unknown Agent maintenance

[Timer]
OnCalendar=weekly
# Catch up if the machine was off at the scheduled moment.
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
"""

UNITS: tuple[UnitSpec, ...] = (
    UnitSpec(
        key="maintenance",
        description="Weekly housekeeping: prune run journals and session indexes, enforce memory retention, rotate logs, cache the update check.",
        rationale=(
            "Keeps ~/.local/state/unknown-agent from growing without bound and lets the CLI "
            "report available updates instantly. Install it if you use the agent regularly; "
            "skip it if you prefer running `ua service run-maintenance` yourself."
        ),
        service_name="ua-maintenance.service",
        timer_name="ua-maintenance.timer",
        service_body=_MAINTENANCE_SERVICE,
        timer_body=_MAINTENANCE_TIMER,
    ),
)


def systemd_available() -> tuple[bool, str]:
    """Can user units work here at all?  (systemd distro + user session bus.)"""

    if os.name == "nt":
        return False, "systemd does not exist on Windows (use Task Scheduler; `ua service` prints the equivalent)."
    if shutil.which("systemctl") is None:
        return False, "systemctl is not on PATH (non-systemd distro or minimal container)."
    probe = subprocess.run(  # noqa: S603,S607 - fixed argv
        ["systemctl", "--user", "is-system-running"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    state = (probe.stdout or probe.stderr or "").strip()
    # 'running' and 'degraded' both mean the user manager is usable.
    if probe.returncode == 0 or state in {"running", "degraded"}:
        return True, state or "running"
    return False, f"user manager unavailable ({state or 'no user session bus'} - are you in an SSH session without lingering?)"


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


def _launcher() -> str:
    found = shutil.which("ua")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "ua"
    return str(fallback)


def render_unit(spec: UnitSpec, *, repo: str = "unknown-agent/unknown-agent") -> dict[str, str]:
    files = {spec.service_name: spec.service_body.format(launcher=_launcher(), repo=repo)}
    if spec.timer_name and spec.timer_body:
        files[spec.timer_name] = spec.timer_body
    return files


def systemctl_user(*args: str) -> tuple[int, str]:
    result = subprocess.run(  # noqa: S603,S607 - fixed command, variable verbs only
        ["systemctl", "--user", *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    return result.returncode, (result.stdout or result.stderr or "").strip()


def install_unit(spec: UnitSpec, *, enable_now: bool = True, repo: str = "unknown-agent/unknown-agent") -> list[str]:
    """Write unit files, reload, optionally enable the timer.  Returns actions taken."""

    actions: list[str] = []
    target = unit_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name, body in render_unit(spec, repo=repo).items():
        (target / name).write_text(body, encoding="utf-8")
        actions.append(f"wrote {target / name}")
    code, message = systemctl_user("daemon-reload")
    actions.append("systemctl --user daemon-reload" + ("" if code == 0 else f" -> {message}"))
    if enable_now and spec.timer_name:
        code, message = systemctl_user("enable", "--now", spec.timer_name)
        actions.append(f"enabled {spec.timer_name}" if code == 0 else f"enable failed: {message}")
    return actions


def remove_unit(spec: UnitSpec) -> list[str]:
    actions: list[str] = []
    if spec.timer_name:
        systemctl_user("disable", "--now", spec.timer_name)
        actions.append(f"disabled {spec.timer_name}")
    for name in filter(None, (spec.timer_name, spec.service_name)):
        path = unit_dir() / name
        if path.exists():
            path.unlink()
            actions.append(f"removed {path}")
    systemctl_user("daemon-reload")
    actions.append("systemctl --user daemon-reload")
    return actions


def unit_status(spec: UnitSpec) -> dict[str, str]:
    installed = (unit_dir() / spec.service_name).exists()
    status: dict[str, str] = {"unit": spec.key, "installed": "yes" if installed else "no"}
    if not installed:
        return status
    if spec.timer_name:
        _, enabled = systemctl_user("is-enabled", spec.timer_name)
        _, active = systemctl_user("is-active", spec.timer_name)
        status["timer"] = f"{enabled}/{active}"
        code, nxt = systemctl_user("list-timers", spec.timer_name, "--no-pager", "--no-legend")
        if code == 0 and nxt:
            status["next_run"] = nxt.split()[0] + " " + (nxt.split()[1] if len(nxt.split()) > 1 else "")
    _, result = systemctl_user("show", spec.service_name, "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp")
    status["last_result"] = result.replace("\n", " ")
    return status
