"""Install detection, systemd unit design, and the ``ua service`` group.

systemctl is never invoked for real here: availability is monkeypatched and
the sandbox has no user bus anyway. What these tests pin is the *contract*:
detection precedence, unit file correctness, graceful degradation, and the
maintenance job's idempotence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from unknown_agent.cli.app import app

runner = CliRunner()


def _payload(output: str) -> dict:
    return json.loads(output[output.index("{"):])


# --------------------------------------------------------------------------- #
# install detection
# --------------------------------------------------------------------------- #
class TestDetectInstall:
    def test_detects_a_mode_and_launcher(self) -> None:
        from unknown_agent.host.install import detect_install

        info = detect_install()
        assert info.mode in {"pipx", "uv-tool", "bootstrap", "virtualenv", "container", "system"}
        assert info.python
        payload = info.to_dict()
        assert {"mode", "launcher_on_path", "path_gap", "receipt"} <= set(payload)

    def test_bootstrap_mode_wins_over_everything(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unknown_agent.host import install

        monkeypatch.setenv("UA_HOME", str(tmp_path / "ua-home"))
        monkeypatch.setattr(install.sys, "prefix", str(tmp_path / "ua-home" / "venv"))
        info = install.detect_install()
        assert info.mode == "bootstrap"

    def test_receipt_is_parsed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unknown_agent.host import install

        home = tmp_path / "ua-home"
        home.mkdir()
        (home / "install-receipt.txt").write_text(
            "installed_at=2026-01-01T00:00:00Z\nstrategy=venv\nsource=git\n"
            "git_url=https://github.com/owner/repo\ngit_ref=main\n"
        )
        monkeypatch.setenv("UA_HOME", str(home))
        info = install.detect_install()
        assert info.receipt["source"] == "git"
        assert info.receipt["git_url"] == "https://github.com/owner/repo"

    def test_upgrade_command_respects_git_origin(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unknown_agent.host.install import InstallInfo, upgrade_command_for

        git_receipt = {"source": "git", "git_url": "https://github.com/owner/repo"}
        pipx_git = upgrade_command_for(InstallInfo(mode="pipx", python="p", prefix="x", receipt=git_receipt))
        assert pipx_git == ["pipx", "install", "--force", "git+https://github.com/owner/repo"]
        pipx_pypi = upgrade_command_for(InstallInfo(mode="pipx", python="p", prefix="x"))
        assert pipx_pypi == ["pipx", "upgrade", "unknown-agent"]

    def test_bootstrap_upgrade_uses_the_managed_venv_python(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unknown_agent.host import install

        monkeypatch.setenv("UA_HOME", str(tmp_path))
        info = install.InstallInfo(mode="bootstrap", python="p", prefix="x", receipt={"git_url": "https://g/r", "source": "git"})
        command = install.upgrade_command_for(info)
        assert command is not None
        assert command[0].startswith(str(tmp_path / "venv"))
        assert command[-1] == "git+https://g/r"


# --------------------------------------------------------------------------- #
# systemd unit design
# --------------------------------------------------------------------------- #
class TestSystemdUnits:
    def test_registry_has_the_maintenance_unit(self) -> None:
        from unknown_agent.host.systemd import UNITS

        keys = [unit.key for unit in UNITS]
        assert keys == ["maintenance"]
        spec = UNITS[0]
        assert spec.rationale  # every unit must justify its existence to the user

    def test_rendered_units_are_wellformed_and_user_level(self) -> None:
        from unknown_agent.host.systemd import UNITS, render_unit

        files = render_unit(UNITS[0], repo="owner/repo")
        service = files["ua-maintenance.service"]
        timer = files["ua-maintenance.timer"]
        # oneshot job calling a stable CLI verb, hardened, never root
        assert "Type=oneshot" in service
        assert "service run-maintenance" in service
        assert "NoNewPrivileges=true" in service
        assert "User=" not in service  # user units must not set User=
        assert "OnCalendar=weekly" in timer
        assert "Persistent=true" in timer
        assert "WantedBy=timers.target" in timer
        # documentation link uses the repo argument
        assert "github.com/owner/repo" in service

    def test_unit_dir_honours_xdg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unknown_agent.host.systemd import unit_dir

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        assert unit_dir() == tmp_path / "cfg" / "systemd" / "user"

    def test_install_and_remove_roundtrip_without_systemctl(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unknown_agent.host import systemd

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(systemd, "systemctl_user", lambda *a: (0, "ok"))
        actions = systemd.install_unit(systemd.UNITS[0], enable_now=True)
        target = systemd.unit_dir()
        assert (target / "ua-maintenance.service").is_file()
        assert (target / "ua-maintenance.timer").is_file()
        assert any("enabled" in action for action in actions)

        removed = systemd.remove_unit(systemd.UNITS[0])
        assert not (target / "ua-maintenance.service").exists()
        assert not (target / "ua-maintenance.timer").exists()
        assert any("removed" in action for action in removed)


# --------------------------------------------------------------------------- #
# ua service CLI
# --------------------------------------------------------------------------- #
class TestServiceCli:
    @pytest.fixture(autouse=True)
    def _cwd(self, cli_env: None) -> None:
        """Run inside the generated test project."""

    def invoke(self, *args: str, expect: int | None = 0) -> str:
        result = runner.invoke(app, list(args))
        if expect is not None:
            assert result.exit_code == expect, f"`ua {' '.join(args)}` exited {result.exit_code}:\n{result.output}"
        return result.output

    def test_group_is_reachable(self) -> None:
        assert "service" in self.invoke("--help")
        out = self.invoke("service", "--help")
        for command in ("list", "install", "remove", "status", "run-maintenance"):
            assert command in out

    def test_list_reports_units_and_systemd_state(self) -> None:
        data = _payload(self.invoke("service", "list", "--json"))
        assert "systemd" in data and "available" in data["systemd"]
        assert data["units"] and data["units"][0]["unit"] == "maintenance"
        assert data["units"][0]["rationale"]

    def test_install_without_systemd_fails_with_alternatives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unknown_agent.host import systemd

        monkeypatch.setattr(systemd, "systemd_available", lambda: (False, "no user bus"))
        out = self.invoke("service", "install", "maintenance", "--json", expect=1)
        data = _payload(out)
        assert data["installed"] is False
        assert "cron" in data["alternatives"] and "windows" in data["alternatives"]

    def test_install_writes_units_when_systemd_is_available(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unknown_agent.host import systemd

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(systemd, "systemd_available", lambda: (True, "running"))
        monkeypatch.setattr(systemd, "systemctl_user", lambda *a: (0, "ok"))
        data = _payload(self.invoke("service", "install", "maintenance", "--json"))
        assert data["installed"] is True
        assert (tmp_path / "cfg" / "systemd" / "user" / "ua-maintenance.timer").is_file()

    def test_remove_when_not_installed_is_clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        data = _payload(self.invoke("service", "remove", "maintenance", "--json"))
        assert data["removed"] is False

    def test_unknown_unit_lists_the_valid_ones(self) -> None:
        out = self.invoke("service", "status", "ghost-unit", expect=None)
        assert "maintenance" in out  # the error hint names valid units

    def test_run_maintenance_is_idempotent(self) -> None:
        first = _payload(self.invoke("service", "run-maintenance", "--json"))
        second = _payload(self.invoke("service", "run-maintenance", "--json"))
        for report in (first, second):
            assert "sessions_pruned" in report
            assert "log_rotated" in report
            assert "cache_files_removed" in report
        # update-check stamp was written where the CLI can find it
        # (state dir is redirected into the test home by conftest)

    def test_run_maintenance_rotates_oversized_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unknown_agent.config.paths import resolve_paths

        paths = resolve_paths(Path.cwd())
        log_file = paths.log_file
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_bytes(b"x" * (6 * 1024 * 1024))  # over the 5 MiB cap
        report = _payload(self.invoke("service", "run-maintenance", "--json"))
        assert report["log_rotated"] is True
        assert log_file.with_suffix(".log.1").exists()


# --------------------------------------------------------------------------- #
# doctor integration
# --------------------------------------------------------------------------- #
class TestDoctorInstallGroup:
    @pytest.fixture(autouse=True)
    def _cwd(self, cli_env: None) -> None:
        pass

    def test_doctor_reports_the_install_group(self) -> None:
        result = runner.invoke(app, ["--json", "doctor"])
        data = _payload(result.output)
        groups = {check["group"] for check in data["checks"]}
        assert "install" in groups
        install_checks = [c for c in data["checks"] if c["group"] == "install"]
        names = {c["name"] for c in install_checks}
        assert "install mode" in names
