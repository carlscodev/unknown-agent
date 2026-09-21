"""Configuration loading with explicit, explainable precedence.

Precedence (lowest to highest)::

    built-in defaults
    < user config        (~/.config/unknown-agent/config.toml)
    < project config     (<repo>/.unknown/config.toml)
    < environment        (UNKNOWN_AGENT_*)
    < CLI flags

The loader records which source produced every value, which is what powers
``ua config show --explain``: users should never have to guess why a setting is
what it is.  Deep merge is recursive for tables (``[agent]``, ``[policy]``, ...)
so a project only overrides the keys it cares about.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from unknown_agent.config.models import Settings
from unknown_agent.config.paths import AppPaths, find_project_root, resolve_paths
from unknown_agent.config.secrets import is_reference, looks_like_credential, resolve_secret
from unknown_agent.errors import ConfigError

ENV_PREFIX = "UNKNOWN_AGENT_"

#: Documented environment variables.  Values are dotted paths into the schema.
ENV_MAP: dict[str, str] = {
    "UNKNOWN_AGENT_PROVIDER": "provider",
    "UNKNOWN_AGENT_MODEL": "providers.default.model",
    "UNKNOWN_AGENT_BASE_URL": "providers.default.base_url",
    "UNKNOWN_AGENT_API_KEY": "providers.default.api_key",
    "UNKNOWN_AGENT_LOG_LEVEL": "logging.level",
    "UNKNOWN_AGENT_LOG_FORMAT": "logging.format",
    "UNKNOWN_AGENT_OUTPUT": "ui.output",
    "UNKNOWN_AGENT_MAX_STEPS": "agent.max_steps",
    "UNKNOWN_AGENT_MAX_TOKENS": "agent.max_tokens",
    "UNKNOWN_AGENT_MODE": "agent.mode",
    "UNKNOWN_AGENT_READ_ONLY": "policy.read_only",
    "UNKNOWN_AGENT_ALLOW_NETWORK": "policy.allow_network",
    "UNKNOWN_AGENT_ALLOW_OUTSIDE_WORKSPACE": "policy.allow_outside_workspace",
    "UNKNOWN_AGENT_BROWSER_HEADLESS": "browser.headless",
    "UNKNOWN_AGENT_BROWSER_BASE_URL": "browser.base_url",
    "UNKNOWN_AGENT_TELEMETRY_ENABLED": "telemetry.enabled",
    "UNKNOWN_AGENT_DATA_DIR": "_paths.data_dir",
    "UNKNOWN_AGENT_STATE_DIR": "_paths.state_dir",
    "UNKNOWN_AGENT_CONFIG_DIR": "_paths.config_dir",
}

_BOOL_TRUE = {"1", "true", "yes", "on", "y"}
_BOOL_FALSE = {"0", "false", "no", "off", "n"}


@dataclass
class LoadedSettings:
    settings: Settings
    paths: AppPaths
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    project_root: Path | None = None

    def source_of(self, dotted: str) -> str:
        return self.sources.get(dotted, "default")

    def explain(self, dotted: str) -> str:
        src = self.source_of(dotted)
        if src.startswith("env:"):
            return f"environment variable {src[4:]}"
        if src.startswith("file:"):
            return f"config file {src[5:]}"
        if src.startswith("flag:"):
            return f"command line ({src[5:]})"
        return "built-in default"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def read_toml(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ConfigError(f"Configuration file not found: {path}", hint="Run `ua config init`.")
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Invalid TOML in {path}: {exc}",
            hint="Run `ua config validate` after fixing the syntax.",
            context={"path": str(path)},
        ) from exc
    if not isinstance(data, dict):  # pragma: no cover - defensive
        raise ConfigError(f"Configuration file {path} must contain a TOML table.")
    return data


def deep_merge(base: dict[str, Any], overlay: dict[str, Any], *, prefix: str = "", sources: dict[str, str] | None = None, source_label: str = "") -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``, recording provenance."""

    for key, value in overlay.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value, prefix=f"{dotted}.", sources=sources, source_label=source_label)
            continue
        base[key] = value
        if sources is not None and value is not None:
            # Record both the exact key and, for tables, the parent path.
            sources[dotted] = source_label
    return base


def _coerce_scalar(text: str, field_path: str) -> Any:
    lowered = text.strip().lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    if lowered in {"none", "null", ""}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if field_path.endswith(("denied_paths", "stack", "conventions", "ignore", "include")):
        return [item.strip() for item in text.split(",") if item.strip()]
    return text


def _set_dotted(target: dict[str, Any], dotted: str, value: Any, *, sources: dict[str, str], label: str) -> None:
    node = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):  # pragma: no cover - guarded by schema validation
            raise ConfigError(f"Configuration conflict at {dotted}: {part} is already a value.")
    node[parts[-1]] = value
    sources[dotted] = label


def env_overrides(env: dict[str, str] | None = None, *, sources: dict[str, str] | None = None) -> dict[str, Any]:
    """Turn ``UNKNOWN_AGENT_*`` variables into a nested override dict."""

    env = env if env is not None else dict(os.environ)
    out: dict[str, Any] = {}
    for var, raw in env.items():
        if not var.startswith(ENV_PREFIX) or raw is None:
            continue
        label = f"env:{var}"
        if var in ENV_MAP:
            _set_dotted(out, ENV_MAP[var], _coerce_scalar(raw, ENV_MAP[var]), sources=sources or {}, label=label)
            continue
        remainder = var[len(ENV_PREFIX) :]
        if "__" in remainder:  # generic escape hatch: UNKNOWN_AGENT_AGENT__MAX_STEPS=12
            dotted = ".".join(part.lower() for part in remainder.split("__"))
            _set_dotted(out, dotted, _coerce_scalar(raw, dotted), sources=sources or {}, label=label)
    if sources is not None:
        for var in env:
            if var in ENV_MAP:
                sources[ENV_MAP[var]] = f"env:{var}"
    return out


def load_dotenv(path: Path) -> None:
    """Load a ``.env`` file into ``os.environ`` without overwriting real env vars."""

    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _offending_value(error: Any) -> str:
    """Quote the value the user actually wrote.

    Pydantic's own message says what is *allowed* ("Input should be 'auto',
    'dark', …") but never what was supplied — and the supplied half is the one
    that locates the typo.  Only scalars are quoted: echoing an entire rejected
    section back inside a one-line error is noise, not help.
    """

    value = error.get("input") if isinstance(error, dict) else None
    if value is None or isinstance(value, (dict, list, tuple, set, bytes)):
        return ""
    text = str(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return f" (got {text!r})"


def _validation_report(exc: ValidationError) -> tuple[str, list[str]]:
    problems: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        problems.append(f"{location}: {error['msg']}{_offending_value(error)}")
    headline = f"Configuration is invalid ({len(problems)} problem{'s' if len(problems) != 1 else ''})."
    return headline, problems


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def load_settings(
    cwd: Path | None = None,
    *,
    config_file: Path | None = None,
    use_project_config: bool = True,
    overrides: dict[str, Any] | None = None,
    flag_sources: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    paths: AppPaths | None = None,
) -> LoadedSettings:
    """Assemble :class:`Settings` from every configuration layer."""

    resolved = paths or resolve_paths(cwd)
    resolved.ensure_user_dirs()
    warnings: list[str] = []
    sources: dict[str, str] = {}

    load_dotenv(resolved.cwd / ".env")
    load_dotenv(resolved.config_dir / ".env")

    merged: dict[str, Any] = Settings().model_dump(mode="python")
    warnings.extend(_collect_reference_warnings(merged))

    user_file = Path(config_file).expanduser() if config_file else resolved.user_config_file
    if config_file and not Path(config_file).expanduser().is_file():
        raise ConfigError(f"--config file does not exist: {user_file}")
    user_data = read_toml(user_file)
    if user_data:
        deep_merge(merged, user_data, sources=sources, source_label=f"file:{user_file}")

    project_root: Path | None = None
    if use_project_config:
        candidate = resolved.cwd if (resolved.cwd / ".unknown").is_dir() else find_project_root(resolved.cwd)
        if candidate and (candidate / ".unknown" / "config.toml").is_file():
            project_root = candidate
            project_data = read_toml(candidate / ".unknown" / "config.toml")
            if project_data:
                # Project files may only override the project-scoped and policy-ish
                # sections; credentials stay user-level by design.
                stripped = {k: v for k, v in project_data.items() if k not in {"providers"}}
                dropped = sorted(set(project_data) - set(stripped))
                if dropped:
                    warnings.append(
                        f"Project config ignored user-scoped section(s): {', '.join(dropped)} "
                        "(define providers in the user config)."
                    )
                deep_merge(merged, stripped, sources=sources, source_label=f"file:{candidate / '.unknown' / 'config.toml'}")

    env_layer = env_overrides(env, sources=sources)
    if env_layer:
        deep_merge(merged, env_layer, sources=sources, source_label="env")

    if overrides:
        clean = {key: value for key, value in overrides.items() if value is not None}
        deep_merge(merged, clean, sources=sources, source_label="flag")
        for dotted in flag_sources or {}:
            sources[dotted] = f"flag:{flag_sources[dotted]}"

    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        headline, problems = _validation_report(exc)
        detail = "; ".join(problems[:4]) + ("; ..." if len(problems) > 4 else "")
        raise ConfigError(
            f"{headline} {detail}".strip(),
            hint="Run `ua config validate` to list every problem, or `ua config init --force` to regenerate.",
            context={"problems": problems, "file": str(user_file)},
        ) from exc

    provider = settings.active_provider
    if provider.api_key:
        resolved_key = None
        try:
            resolved_key = resolve_secret(provider.api_key, key=f"providers.{settings.provider}.api_key")
        except ConfigError as exc:
            if is_reference(provider.api_key):
                warnings.append(str(exc))
        if resolved_key:
            from unknown_agent.config.secrets import REDACTOR

            REDACTOR.register(resolved_key)
    if provider.api_key and looks_like_credential(provider.api_key):
        warnings.append(
            f"providers.{settings.provider}.api_key looks like a live credential stored in a config file. "
            "Move it to an environment variable and reference it as env:YOUR_VAR instead."
        )

    if settings.agent.mode == "react" and settings.agent.max_steps < 5:
        warnings.append("agent.max_steps is very low for react mode; the agent may stop before verifying.")

    return LoadedSettings(
        settings=settings,
        paths=resolved,
        sources=sources,
        warnings=warnings,
        project_root=project_root if project_root else (resolved.cwd if (resolved.cwd / ".unknown").is_dir() else None),
    )


def _collect_reference_warnings(settings_dict: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    providers = settings_dict.get("providers", {})
    for name, provider in providers.items():
        api_key = provider.get("api_key")
        if api_key and looks_like_credential(api_key):
            warnings.append(f"providers.{name}.api_key appears to be a raw credential.")
    return warnings


def dump_settings(settings: Settings, path: Path) -> None:
    """Write settings to ``path`` as formatted TOML."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = settings.model_dump(mode="python", exclude_none=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def default_config_path() -> Path:
    """The user-level config path (``$UNKNOWN_AGENT_CONFIG`` or XDG default)."""

    override = os.environ.get("UNKNOWN_AGENT_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "unknown-agent" / "config.toml"


def prune_defaults(data: Any, reference: Any = None, *, _root: bool = True) -> Any:
    """Strip values that are identical to the pristine defaults.

    A config file that repeats 400 default keys is unreadable, and unreadable
    config is config nobody audits — which matters when some of it governs what
    the agent is allowed to delete.  What gets written is therefore only what
    *differs*: the choices, not the boilerplate.

    The result still round-trips: ``load_settings`` starts from the same defaults
    and merges the file over them, so pruning loses nothing but noise.
    """

    if _root:
        reference = Settings().model_dump(mode="python")
        pruned = prune_defaults(data, reference, _root=False)
        return pruned or {}
    if isinstance(data, dict) and isinstance(reference, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            default = reference.get(key, _MISSING)
            nested = prune_defaults(value, None if default is _MISSING else default, _root=False)
            if default is _MISSING or nested != default:
                # An empty dict means "nothing here differs"; writing it would be
                # noise.  An empty *list* is not noise: it is a deliberate clearing
                # of a default that has entries (e.g. an emptied denylist).
                if nested == {} and isinstance(default, dict) and default:
                    continue
                out[key] = nested
        return out
    if isinstance(data, list) and isinstance(reference, list):
        # Compare the whole list: order and membership both matter for denylists
        # and gate lists, so partial pruning would change their meaning.
        return data if data != reference else reference
    return data


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()


def save_settings(settings: Settings, path: Path | None = None, *, prune: bool = True) -> Path:
    """Write the settings to disk and return the path.

    ``prune=True`` (the default) writes only the differences from the shipped
    defaults, which is what setup should produce: a small file a human can read
    top to bottom.  ``prune=False`` writes the complete effective configuration,
    which is what ``ua config show --toml`` and migrations want.
    """

    target = Path(path) if path is not None else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = settings.model_dump(mode="python", exclude_none=True)
    if prune:
        data = prune_defaults(data)
    target.write_text(tomli_w.dumps(data), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - Windows has no POSIX modes
        pass
    return target


def write_default_config(path: Path, *, force: bool = False) -> Path:
    """Create a commented starter configuration file."""

    if path.exists() and not force:
        raise ConfigError(f"Refusing to overwrite existing config at {path}.", hint="Pass --force to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path


DEFAULT_CONFIG_TEMPLATE = """\
# Unknown Agent user configuration
# Docs: https://github.com/unknown-agent/unknown-agent/blob/main/docs/configuration.md
# Precedence: defaults < this file < <repo>/.unknown/config.toml < UNKNOWN_AGENT_* env < CLI flags
# Secrets: use references such as env:OPENAI_API_KEY or keyring:unknown-agent/openai. Never paste raw keys.

schema_version = 1
provider = "default"            # which profile below is active

[providers.default]
type = "openai-compatible"      # openai-compatible | anthropic | ollama | offline
model = "gpt-4.1-mini"
base_url = "https://api.openai.com/v1"
api_key = "env:OPENAI_API_KEY"
temperature = 0.2
context_window = 128000
max_output_tokens = 8000

# Local, keyless option for air-gapped machines:
# [providers.local]
# type = "ollama"
# model = "qwen2.5-coder:14b"
# base_url = "http://127.0.0.1:11434"

# Deterministic planner used by `--provider offline`, CI and tests:
# [providers.offline]
# type = "offline"
# model = "offline-deterministic"

[agent]
mode = "auto"                   # auto | plan | react
max_steps = 60
max_tokens = 900000
max_wall_clock_minutes = 45
max_self_heal_attempts = 4
definition_of_done = "balanced" # strict | balanced | advisory
include_repo_map = true
streaming = true

[policy]
allow_network = true
allow_outside_workspace = false
confirm_destructive = true
read_only = false

[validation]
auto_detect = true
fail_fast = false

[validation.gates.lint]
enabled = true
[validation.gates.tests]
enabled = true
[validation.gates.build]
enabled = true
[validation.gates.browser]
enabled = true
required = false

[browser]
enabled = true
browser = "chromium"
headless = true
trace = false
screenshots = true

[memory]
auto_remember = true
write_markdown_mirror = true
retention_days = 180

[logging]
level = "info"
format = "text"

[telemetry]
enabled = false                 # local-only counters; nothing leaves the machine unless you opt in
"""


__all__ = [
    "LoadedSettings",
    "DEFAULT_CONFIG_TEMPLATE",
    "default_config_path",
    "deep_merge",
    "dump_settings",
    "env_overrides",
    "load_dotenv",
    "load_settings",
    "prune_defaults",
    "read_toml",
    "save_settings",
    "write_default_config",
]
