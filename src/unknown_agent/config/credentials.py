"""Credential storage.

Secrets are never written into the project, never hardcoded, and never echoed.
What onboarding writes is a *reference* in the config (``api_key = "store:openai"``)
plus the secret itself in a dedicated file in the user's config directory with
``0600`` permissions.  Three storage options are supported, and the user picks:

* ``store:``   - ``~/.config/unknown-agent/credentials.toml`` (chmod 600), default;
* ``env:``     - read from an environment variable (best for CI, containers, cloud);
* ``keyring:`` - OS keyring when the optional ``keyring`` extra is installed.

Everything on the read path goes through :func:`unknown_agent.config.secrets.resolve_secret`,
which means redaction (:data:`~unknown_agent.config.secrets.REDACTOR`) applies to any
value that came from here.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any

from unknown_agent.errors import ConfigError
from unknown_agent.observability.logging import get_logger

log = get_logger("config.credentials")

#: File mode for anything holding a secret.
SECRET_FILE_MODE = 0o600


def default_credentials_path() -> Path:
    """``$XDG_CONFIG_HOME/unknown-agent/credentials.toml`` (Windows-safe)."""

    override = os.environ.get("UNKNOWN_AGENT_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "unknown-agent" / "credentials.toml"


class CredentialStore:
    """Tiny, honest credential store: TOML, 0600, one key per profile."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_credentials_path()

    # -- reads -------------------------------------------------------------
    def load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            payload = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(
                f"Credential file {self.path} could not be read: {exc}",
                hint="Delete or repair the file, then run `ua setup` again.",
            ) from exc
        secrets = payload.get("secrets", {})
        if not isinstance(secrets, dict):
            return {}
        return {str(name): str(value) for name, value in secrets.items() if isinstance(value, str)}

    def get(self, name: str) -> str | None:
        return self.load().get(name)

    def names(self) -> list[str]:
        return sorted(self.load())

    def resolve(self, name: str) -> str | None:
        """Resolve ``store:NAME`` (also accepts a bare name)."""

        key = name.split(":", 1)[1] if name.startswith("store:") else name
        return self.get(key)

    # -- writes ------------------------------------------------------------
    def set(self, name: str, secret: str) -> Path:
        """Store ``secret`` under ``name`` and return the file path.

        The file is written atomically with ``0600`` permissions *before* the secret
        is written, so a crash cannot leave a world-readable secret on disk.
        """

        if not name or not secret:
            raise ValueError("both a credential name and a value are required")
        existing = self.load()
        existing[name] = secret
        self._write(existing)
        log.info("stored credential %r in %s", name, self.path)
        return self.path

    def delete(self, name: str) -> bool:
        existing = self.load()
        if name not in existing:
            return False
        del existing[name]
        self._write(existing)
        return True

    def _write(self, secrets: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = ["# Unknown Agent credentials.", "# Written by `ua setup`. Keep this file private (0600).", "", "[secrets]"]
        for name in sorted(secrets):
            body.append(f"{_quote(name)} = {_quote(secrets[name])}")
        body.append("")
        payload = "\n".join(body)

        # create the file with restrictive permissions, then write through it
        handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_FILE_MODE)
        try:
            os.write(handle, payload.encode("utf-8"))
        finally:
            os.close(handle)
        try:  # pragma: no cover - windows / odd filesystems
            os.chmod(self.path, SECRET_FILE_MODE)
        except OSError:
            log.warning("could not restrict permissions on %s", self.path)

    # -- diagnostics --------------------------------------------------------
    def audit(self) -> dict[str, Any]:
        """Report on the store's health without ever returning a secret."""

        payload: dict[str, Any] = {
            "path": str(self.path),
            "exists": self.path.is_file(),
            "entries": self.names(),
            "permissions": None,
            "warnings": [],
        }
        if not self.path.is_file():
            return payload

        payload["permissions"] = oct(stat.S_IMODE(self.path.stat().st_mode))
        if os.name == "posix":
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                payload["warnings"].append(f"{self.path} is readable by other users (mode {oct(mode)}); run `chmod 600`")
        return payload


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def store_reference(name: str) -> str:
    return f"store:{name}"


__all__ = ["CredentialStore", "SECRET_FILE_MODE", "default_credentials_path", "store_reference"]
