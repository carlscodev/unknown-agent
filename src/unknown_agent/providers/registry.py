"""Provider registry and construction.

Providers are keyed by their config ``type``.  Third-party plugins can register
new ones with :func:`register_provider`; the CLI and the agent core only ever ask
the registry for a construction function, which keeps vendor knowledge in one
place and makes ``--provider offline`` work in tests and CI.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from unknown_agent.config.models import ProviderConfig
from unknown_agent.config.secrets import REDACTOR, resolve_secret
from unknown_agent.errors import ConfigError, ProviderError
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import ModelProvider

log = get_logger("providers.registry")

ProviderFactory = Callable[..., ModelProvider]

_FACTORIES: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory, *, overwrite: bool = False) -> None:
    key = name.strip().lower()
    if key in _FACTORIES and not overwrite:
        raise ConfigError(f"Provider {key!r} is already registered.")
    _FACTORIES[key] = factory
    log.debug("registered provider %s", key)


def available_providers() -> list[str]:
    return sorted(_FACTORIES)


def protocol_for(name: str) -> str:
    """Map a vendor key (``groq``) or dialect (``anthropic``) to an adapter protocol."""

    from unknown_agent.providers.catalog import PROTOCOL_BY_TYPE, resolve_entry

    key = (name or "").strip().lower()
    if key in _FACTORIES:
        return key
    protocol = ""
    entry = resolve_entry(key)
    if entry is not None:
        protocol = PROTOCOL_BY_TYPE.get(entry.key, entry.protocol)
    if not protocol:
        protocol = PROTOCOL_BY_TYPE.get(key, key)
    # `openai` is the dialect name in the catalog; the registry key is the config
    # type `openai-compatible`.
    return "openai-compatible" if protocol == "openai" else protocol


def provider_factory(name: str) -> ProviderFactory:
    key = protocol_for(name)
    if key not in _FACTORIES:
        raise ProviderError(
            f"Unknown provider type {name!r}.",
            hint=f"Available types: {', '.join(available_providers())}.",
        )
    return _FACTORIES[key]


#: Defaults used when a bare provider *type* is requested (``--provider offline``).
PROVIDER_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "offline": {"type": "offline", "model": "offline-deterministic"},
    "ollama": {"type": "ollama", "model": "qwen2.5-coder:7b", "base_url": "http://127.0.0.1:11434"},
    "anthropic": {"type": "anthropic", "model": "claude-3-5-sonnet-latest", "api_key": "env:ANTHROPIC_API_KEY"},
    "openai-compatible": {"type": "openai-compatible", "model": "gpt-4.1-mini", "api_key": "env:OPENAI_API_KEY"},
}


def entry_for_type(name: str) -> Any:
    """The catalog entry a profile's ``type`` points at, if any."""

    from unknown_agent.providers.catalog import resolve_entry

    return resolve_entry(name)


def ensure_provider_profile(settings: Any, name: str | None) -> bool:
    """Make sure ``settings.providers`` contains a usable profile called ``name``.

    A user may point the CLI at a provider *type* (``--provider offline``) instead of
    a configured profile.  Rather than failing, the type is materialised into a
    profile with documented defaults, so the product works before any setup:

    * ``offline``     - deterministic, no network, ideal for CI and evaluation;
    * ``ollama``      - local models on ``127.0.0.1:11434``;
    * ``anthropic`` / ``openai-compatible`` - read the API key from the environment.

    Returns ``True`` when a profile exists (already present or just created).
    """

    if not name:
        return True
    if name in getattr(settings, "providers", {}):
        return True
    if name not in _FACTORIES:
        return False
    template = dict(PROVIDER_PROFILE_DEFAULTS.get(name, {"type": name}))
    settings.providers[name] = ProviderConfig(**template)
    log.info("materialised provider profile %r from provider type %r", name, template.get("type", name))
    return True


def build_provider(config: ProviderConfig, *, name: str = "default") -> ModelProvider:
    """Instantiate a provider from configuration, resolving secret references."""

    api_key = None
    if config.api_key:
        api_key = resolve_secret(config.api_key, key=f"providers.{name}.api_key")
        if api_key:
            REDACTOR.register(api_key)
    entry = entry_for_type(config.type)
    if entry is not None and config.base_url is None and entry.base_url:
        config.base_url = entry.base_url
    if entry is not None and not api_key and entry.api_key_required:
        raise ConfigError(
            f"Provider {name!r} ({config.type}) has no API key configured.",
            hint=(
                'Set [providers.' + name + '] api_key = "env:OPENAI_API_KEY" in config, '
                "export the variable, or use --provider offline / type = \"ollama\" for local models."
            ),
            context={"provider": name, "type": config.type},
        )
    factory = provider_factory(config.type)
    return factory(
        model=config.model,
        base_url=config.base_url,
        vendor=entry.key if entry is not None else config.type,
        api_key=api_key,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
        context_window=config.context_window,
        request_timeout_s=config.request_timeout_s,
        connect_timeout_s=config.connect_timeout_s,
        max_retries=config.max_retries,
        headers=config.headers,
        extra=config.extra,
    )


def estimate_cost(config: ProviderConfig, *, prompt_tokens: int, completion_tokens: int) -> float | None:
    if not config.input_cost_per_mtok and not config.output_cost_per_mtok:
        return None
    return (prompt_tokens / 1_000_000) * config.input_cost_per_mtok + (completion_tokens / 1_000_000) * config.output_cost_per_mtok


def _register_builtins() -> None:
    from unknown_agent.providers import anthropic, offline, ollama, openai_compatible

    _FACTORIES.setdefault("openai-compatible", openai_compatible.build)
    _FACTORIES.setdefault("anthropic", anthropic.build)
    _FACTORIES.setdefault("ollama", ollama.build)
    _FACTORIES.setdefault("offline", offline.build)


_register_builtins()

__all__ = [
    "register_provider",
    "available_providers",
    "provider_factory",
    "build_provider",
    "estimate_cost",
    "ProviderFactory",
]
