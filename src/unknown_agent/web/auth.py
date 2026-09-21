"""Authentication state for the web panel: owner token + device pairing.

The web server and the CLI are **separate processes**, so they cooperate through a
single JSON state file in the config directory (``web_auth.json``, mode 0600):

* ``ua web`` creates the owner token and serves the panel.
* A browser without the token requests access and shows a 6-digit code.
* ``ua approve device <code>`` (terminal) moves that code from ``pending`` to
  ``devices``; the running server notices because it reloads the file whenever its
  mtime changes, so the browser's next poll is approved.  No restart, no socket,
  no daemon — the file *is* the IPC channel.

Everything sensitive is compared in constant time and written atomically.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

MAX_PENDING = 5
SESSION_TTL_S = 60 * 60 * 24 * 30  # 30 days


def _now() -> float:
    return time.time()


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _new_code(existing: set[str]) -> str:
    """A 6-digit pairing code that is easy to type on a phone and unambiguous."""
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if code not in existing:
            return code


class WebAuthStore:
    """Reads/writes ``web_auth.json``; reloads when another process changes it."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._load()

    # -- persistence ------------------------------------------------------- #
    def _load(self) -> dict[str, Any]:
        """Re-read the file on every call.

        The file is a few KB, so an unconditional read is cheaper than any cache
        scheme — and it is the *only* scheme that is correct across processes on
        filesystems with coarse mtime granularity.  This is what lets
        ``ua approve device`` take effect in a running server instantly.
        """
        data: dict[str, Any] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        self._data = data if isinstance(data, dict) else {}
        return self._data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    # -- owner token -------------------------------------------------------- #
    def ensure_owner_token(self) -> str:
        data = self._load()
        token = str(data.get("owner_token") or "")
        if not token:
            token = _new_token()
            data["owner_token"] = token
            self._save()
        return token

    def verify_owner(self, token: str) -> bool:
        expected = str(self._load().get("owner_token") or "")
        if not expected or not token:
            return False
        return secrets.compare_digest(expected, token)

    def rotate_owner_token(self) -> str:
        data = self._load()
        token = _new_token()
        data["owner_token"] = token
        # A rotated token must invalidate existing owner sessions.
        data["sessions"] = {
            sid: s
            for sid, s in (data.get("sessions") or {}).items()
            if (s or {}).get("role") != "owner"
        }
        self._save()
        return token

    # -- sessions ----------------------------------------------------------- #
    def new_session(self, role: str, *, device: str | None = None, label: str = "") -> str:
        data = self._load()
        sid = _new_token()
        sessions = data.setdefault("sessions", {})
        sessions[sid] = {
            "role": role,
            "device": device,
            "label": label,
            "created": _now(),
        }
        self._save()
        return sid

    def session(self, sid: str) -> dict[str, Any] | None:
        if not sid:
            return None
        sess = (self._load().get("sessions") or {}).get(sid)
        if not isinstance(sess, dict):
            return None
        created = float(sess.get("created") or 0)
        if _now() - created > SESSION_TTL_S:
            self.delete_session(sid)
            return None
        return sess

    def delete_session(self, sid: str) -> None:
        data = self._load()
        sessions = data.get("sessions") or {}
        if sid in sessions:
            del sessions[sid]
            data["sessions"] = sessions
            self._save()

    # -- device pairing ------------------------------------------------------ #
    def request_device(self, *, label: str = "") -> str | None:
        data = self._load()
        pending = data.setdefault("pending", {})
        if len(pending) >= MAX_PENDING:
            return None
        used = set(pending) | set(data.get("devices") or {})
        code = _new_code(used)
        pending[code] = {"created": _now(), "label": label}
        self._save()
        return code

    def pending(self) -> dict[str, Any]:
        return dict(self._load().get("pending") or {})

    def approve_device(self, code: str, *, name: str = "") -> bool:
        """Terminal side of the handshake.  Idempotent for already-approved codes."""
        code = (code or "").strip()
        if not code.isdigit() or len(code) != 6:
            return False
        data = self._load()
        devices = data.setdefault("devices", {})
        if code in devices:
            return True
        pending = data.get("pending") or {}
        entry = pending.pop(code, None)
        if entry is None:
            return False
        devices[code] = {
            "approved_at": _now(),
            "label": (entry or {}).get("label", ""),
            "name": name or f"device {code}",
        }
        data["pending"] = pending
        data["devices"] = devices
        self._save()
        return True

    def device_approved(self, code: str) -> bool:
        return code in (self._load().get("devices") or {})

    def devices(self) -> dict[str, Any]:
        return dict(self._load().get("devices") or {})

    def revoke_device(self, code: str) -> bool:
        data = self._load()
        devices = data.get("devices") or {}
        if code not in devices:
            return False
        del devices[code]
        data["devices"] = devices
        # Kill live sessions that were bound to the revoked device.
        data["sessions"] = {
            sid: s
            for sid, s in (data.get("sessions") or {}).items()
            if (s or {}).get("device") != code
        }
        self._save()
        return True


__all__ = ["MAX_PENDING", "SESSION_TTL_S", "WebAuthStore"]
