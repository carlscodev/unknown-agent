"""Secret references and redaction.

Unknown Agent never stores raw API keys in configuration files.  A configuration
value may be a *reference*:

* ``env:OPENAI_API_KEY``          - read from the process environment
* ``file:~/.secrets/openai.key``  - read the first non-empty line of a file
* ``keyring:unknown-agent/openai`` - read from the OS keyring (optional extra)

Plain strings are accepted for local, non-sensitive settings only; the loader
warns when a value looks like a live credential so that leaked keys are caught
early instead of being committed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from unknown_agent.errors import ConfigError

_PREFIXES = ("env:", "file:", "keyring:", "store:")
_CREDENTIAL_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,})"
)
SECRET_MASK = "********"


def is_reference(value: str | None) -> bool:
    return bool(value) and value.startswith(_PREFIXES)


def looks_like_credential(value: str | None) -> bool:
    """Strict check used when *scanning* text: known vendor prefixes only.

    Deliberately narrow - it drives the "a raw credential was committed to a config
    file" warning, so a false positive would be noise on every long token.
    """

    return bool(value) and bool(_CREDENTIAL_RE.search(value))


#: A pasted API key: one token, no whitespace, long enough to be a credential.
#: Vendors disagree wildly on format (Azure: 32 hex chars, Cohere: 40 opaque chars,
#: Groq: ``gsk_...``, Zhipu: ``id.secret``), so onboarding validates *shape* and lets
#: the provider itself be the authority - that is what the immediate live check is for.
_PASTED_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/=\-]{15,}$")
_PLACEHOLDERS = frozenset({"your-api-key", "your_api_key", "changeme", "apikey", "api-key", "test", "placeholder", "none", "null", "undefined"})


def looks_like_api_key(value: str | None) -> bool:
    candidate = (value or "").strip().strip("\"'")
    if not candidate or any(character.isspace() for character in candidate):
        return False
    if _CREDENTIAL_RE.search(candidate):
        return True
    if candidate.lower() in _PLACEHOLDERS or candidate.lower().replace("-", "").replace("_", "") in _PLACEHOLDERS:
        return False
    return bool(_PASTED_KEY_RE.match(candidate))


def resolve_secret(reference: str | None, *, key: str = "secret") -> str | None:
    """Resolve a secret reference to its plaintext value, or ``None``."""

    if not reference:
        return None
    if not is_reference(reference):
        return reference or None
    kind, _, target = reference.partition(":")
    target = target.strip()
    if not target:
        raise ConfigError(f"Invalid secret reference for {key}: {reference!r}")
    if kind == "env":
        value = os.environ.get(target)
        if not value:
            raise ConfigError(
                f"Environment variable {target!r} referenced by {key} is not set.",
                hint=f"export {target}=... or run `ua config init` to store it in the OS keyring.",
                context={"variable": target, "key": key},
            )
        return value.strip()
    if kind == "file":
        path = Path(target).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"Secret file {path} referenced by {key} does not exist.",
                hint="Create the file with the secret on the first line, mode 0600.",
                context={"path": str(path)},
            )
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
        raise ConfigError(f"Secret file {path} is empty.", context={"path": str(path)})
    if kind == "store":
        # Credentials written by `ua setup` live in the user config directory with
        # 0600 permissions (see unknown_agent.config.credentials).
        from unknown_agent.config.credentials import CredentialStore

        value = CredentialStore().get(target)
        if not value:
            raise ConfigError(
                f"No stored credential named {target!r}.",
                hint="Run `ua setup` to store one, or use env:NAME / file:PATH instead.",
            )
        return value

    if kind == "keyring":
        try:  # pragma: no cover - depends on optional dependency / OS keyring
            import keyring
        except ImportError as exc:
            raise ConfigError(
                "keyring support is not installed.",
                hint="Install it with `pipx install 'unknown-agent[keyring]'`.",
            ) from exc
        service, _, account = target.partition("/")
        value = keyring.get_password(service, account or service)
        if not value:
            raise ConfigError(
                f"No keyring entry for service={service!r} account={account!r}.",
                hint="Store one with `ua config set-secret <name> --keyring`.",
            )
        return value
    raise ConfigError(f"Unsupported secret reference scheme: {reference!r}")


class Redactor:
    """Registered-secret redactor used by logging and event journaling."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        if value and len(value) >= 6:
            self._values.add(value)

    def redact(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, SECRET_MASK)
        return _CREDENTIAL_RE.sub(SECRET_MASK, text)


#: Process-wide redactor.  Providers and the config loader register every secret
#: they touch here so that no log line, event, or run journal can leak it.
REDACTOR = Redactor()


def redact_text(text: str) -> str:
    return REDACTOR.redact(text)
