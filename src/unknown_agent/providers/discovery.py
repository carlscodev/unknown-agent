"""Credential validation and model discovery, per provider.

``ua setup`` pastes a key and immediately expects two things to come back:

1. "is this key real?" - :func:`validate_credential`
2. "what can I run with it?" - :func:`fetch_models`

Both are answered *by the provider*, not by a hardcoded list, whenever the vendor
exposes an endpoint (OpenRouter's ``GET /api/v1/models``, OpenAI's ``/v1/models``,
Anthropic's ``/v1/models``, Ollama's ``/api/tags``...).  When a vendor cannot list
models, we say so and fall back to the curated list in the catalog, and validation
uses a one-token request instead.  Nothing here is guessed silently: every failure
comes back as a normalised :class:`ProviderProbeError` with a human hint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from unknown_agent.config.secrets import REDACTOR, looks_like_credential
from unknown_agent.errors import ConfigError, ProviderError
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.catalog import ProviderEntry, known_model_ids

log = get_logger("providers.discovery")

DEFAULT_TIMEOUT = 20.0


@dataclass(slots=True)
class ModelInfo:
    """One model, normalised across every vendor's vocabulary."""

    id: str
    name: str = ""
    provider: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool = False
    supports_vision: bool = False
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    created: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "supports_vision": self.supports_vision,
            "input_cost_per_mtok": self.input_cost_per_mtok,
            "output_cost_per_mtok": self.output_cost_per_mtok,
        }


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    message: str
    hint: str = ""
    models_available: bool = False
    model_count: int = 0
    status_code: int | None = None


class ProviderProbeError(ProviderError):
    """A discovery/validation failure that already knows how to explain itself."""


# --------------------------------------------------------------------------- #
# request building
# --------------------------------------------------------------------------- #
def build_client(entry: ProviderEntry, *, api_key: str | None, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """Create an HTTP client with the provider's auth and headers applied."""

    headers: dict[str, str] = {"Accept": "application/json", "User-Agent": "unknown-agent"}
    headers.update(entry.extra_headers)
    if api_key:
        if entry.protocol == "anthropic":
            headers["x-api-key"] = api_key
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    root = (base_url or entry.base_url or "").rstrip("/")
    if not root:
        raise ConfigError(
            f"{entry.label} needs a base URL.",
            hint="Pass --base-url (for Azure: https://<resource>.openai.azure.com/openai/v1).",
        )
    return httpx.AsyncClient(base_url=root, headers=headers, timeout=timeout)


def _listing_url(entry: ProviderEntry) -> str:
    path = entry.listing_path or "/models"
    if path.startswith("http"):
        return path
    return path


# --------------------------------------------------------------------------- #
# model parsing
# --------------------------------------------------------------------------- #
def _parse_openai_style(entry: ProviderEntry, payload: dict[str, Any]) -> list[ModelInfo]:
    items = payload.get("data") or payload.get("models") or []
    models: list[ModelInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("name") or "").strip()
        if not model_id:
            continue
        models.append(
            ModelInfo(
                id=model_id,
                name=str(item.get("name") or item.get("display_name") or model_id),
                provider=entry.key,
                context_window=_int(item.get("context_window") or item.get("context_length") or item.get("max_context_tokens")),
                max_output_tokens=_int(item.get("max_output_tokens") or item.get("max_completion_tokens")),
                created=_float(item.get("created") or item.get("created_at")),
                supports_tools=_tools_supported(item),
                raw=item,
            )
        )
    return models


def _parse_openrouter(entry: ProviderEntry, payload: dict[str, Any]) -> list[ModelInfo]:
    models: list[ModelInfo] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        architecture = item.get("architecture") or {}
        params = item.get("supported_parameters") or []
        modality = str(architecture.get("input_modalities") or architecture.get("modality") or "")
        pricing = item.get("pricing") or {}
        models.append(
            ModelInfo(
                id=model_id,
                name=str(item.get("name") or model_id),
                provider=entry.key,
                context_window=_int(item.get("context_length") or item.get("context_window")),
                max_output_tokens=_int(item.get("top_provider", {}).get("max_completion_tokens") if isinstance(item.get("top_provider"), dict) else None),
                supports_tools=("tools" in params) if params else None,
                supports_reasoning=any(
                    marker in model_id.lower() or marker in str(item.get("name", "")).lower()
                    for marker in ("reason", "thinking", "o1", "o3", "r1")
                ),
                supports_vision="image" in modality,
                input_cost_per_mtok=_per_mtok(pricing.get("prompt")),
                output_cost_per_mtok=_per_mtok(pricing.get("completion")),
                created=_float(item.get("created")),
                raw=item,
            )
        )
    return models


def _parse_anthropic(entry: ProviderEntry, payload: dict[str, Any]) -> list[ModelInfo]:
    models: list[ModelInfo] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        models.append(
            ModelInfo(
                id=model_id,
                name=str(item.get("display_name") or model_id),
                provider=entry.key,
                created=_float(item.get("created_at")),
                supports_tools=True,
                supports_vision=any(marker in model_id for marker in ("claude-3", "claude-4", "sonnet", "opus", "haiku")),
                supports_reasoning="thinking" in model_id or "4" in model_id,
                raw=item,
            )
        )
    return models


def _parse_ollama(entry: ProviderEntry, payload: dict[str, Any]) -> list[ModelInfo]:
    models: list[ModelInfo] = []
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("model") or item.get("name") or "").strip()
        if not model_id:
            continue
        details = item.get("details") or {}
        models.append(
            ModelInfo(
                id=model_id,
                name=model_id,
                provider=entry.key,
                context_window=_int(details.get("context_length")),
                supports_tools=None,
                raw=item,
            )
        )
    return models


_PARSERS = {
    "openai": _parse_openai_style,
    "openrouter": _parse_openrouter,
    "anthropic": _parse_anthropic,
    "ollama": _parse_ollama,
}


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
async def fetch_models(
    entry: ProviderEntry,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int = 500,
) -> list[ModelInfo]:
    """Ask the provider for its models; fall back to the catalog when it cannot.

    Raises :class:`ProviderProbeError` when the request fails for a reason the user
    must fix (bad key, unreachable host) - the caller decides whether to offer the
    fallback list instead.
    """

    if entry.model_listing == "none":
        return [ModelInfo(id=model_id, name=model_id, provider=entry.key) for model_id in known_model_ids(entry)]

    client = build_client(entry, api_key=api_key, base_url=base_url, timeout=timeout)
    try:
        response = await client.get(_listing_url(entry))
        _raise_for_status(entry, response)
        payload = response.json()
    except httpx.HTTPError as exc:  # network-level failure
        raise ProviderProbeError(
            f"Could not reach {entry.label}: {type(exc).__name__}: {exc}",
            hint="Check your network connection, proxy settings, or run the provider locally.",
        ) from exc
    finally:
        await client.aclose()

    parser = _PARSERS.get(entry.model_listing, _parse_openai_style)
    models = parser(entry, payload if isinstance(payload, dict) else {})
    models.sort(key=lambda item: item.id)
    if not models:
        raise ProviderProbeError(
            f"{entry.label} returned an empty model list.",
            hint="The key may be valid but not enabled for any model, or the account needs billing set up.",
        )
    return models[:limit]


async def validate_credential(
    entry: ProviderEntry,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ValidationResult:
    """Check that a key works, preferring the cheapest possible call.

    For vendors with a model-listing endpoint that *is* the check (a 401 is
    unambiguous).  For the rest we send a one-token completion, which is the only
    honest way to know the key and the model actually work together.
    """

    if entry.api_key_required and not api_key:
        return ValidationResult(False, f"{entry.label} requires an API key.", hint=f"Set {entry.env_var} or paste the key here.")

    if entry.model_listing != "none":
        try:
            models = await fetch_models(entry, api_key=api_key, base_url=base_url, timeout=timeout)
        except ProviderProbeError as exc:
            return ValidationResult(False, exc.message, hint=exc.hint)
        return ValidationResult(
            True,
            f"{entry.label} accepted the key and listed {len(models)} model(s).",
            models_available=True,
            model_count=len(models),
        )

    try:
        await _probe_completion(entry, api_key=api_key, base_url=base_url, timeout=timeout)
    except ProviderProbeError as exc:
        return ValidationResult(False, exc.message, hint=exc.hint)
    return ValidationResult(
        True,
        f"{entry.label} accepted the key (verified with a minimal request; this vendor does not list models).",
        models_available=False,
        model_count=len(known_model_ids(entry)),
    )


async def validate_profile(settings: Any, name: str, *, model: str | None = None) -> ValidationResult:
    """Validate a configured profile end to end: key, endpoint, model, tool support."""

    profile = settings.providers.get(name)
    if profile is None:
        return ValidationResult(False, f"No provider profile named {name!r}.", hint="Run `ua providers list`.")

    from unknown_agent.config.secrets import resolve_secret

    try:
        api_key = resolve_secret(profile.api_key, key=f"providers.{name}.api_key") if profile.api_key else None
    except ConfigError as exc:
        return ValidationResult(False, exc.message, hint=exc.hint or "")
    if api_key:
        REDACTOR.register(api_key)

    entry = resolve_entry_for(profile)
    target_model = model or profile.model
    try:
        models = await fetch_models(entry, api_key=api_key, base_url=profile.base_url or entry.base_url)
    except ProviderProbeError as exc:
        if not isinstance(exc, ProviderProbeError):  # pragma: no cover - defensive
            raise
        return ValidationResult(False, exc.message, hint=exc.hint)
    ids = {item.id for item in models}
    if target_model and ids and target_model not in ids:
        close = _closest_model(target_model, ids)
        return ValidationResult(
            False,
            f"{entry.label} does not offer a model called {target_model!r}.",
            hint=f"Did you mean {close!r}? Run `ua models list` to see all {len(ids)} options.",
        )
    return ValidationResult(True, f"{entry.label} is healthy: {len(models)} model(s), using {target_model!r}.", model_count=len(models))


def resolve_entry_for(profile: Any) -> ProviderEntry:
    """Map a configured profile back to its catalog entry (best effort)."""

    from unknown_agent.providers.catalog import CATALOG, resolve_entry

    direct = resolve_entry(getattr(profile, "type", ""))
    if direct is not None:
        return direct
    base_url = (profile.base_url or "").rstrip("/")
    for entry in CATALOG:
        if entry.protocol == profile.type and entry.base_url.rstrip("/") == base_url:
            return entry
    for entry in CATALOG:
        if entry.protocol == profile.type and entry.key == profile.type:
            return entry
    from unknown_agent.providers.catalog import ProviderEntry as _Entry

    return _Entry(
        key=profile.type,
        label=profile.type,
        protocol=profile.type if profile.type in {"anthropic", "ollama", "offline"} else "openai",
        base_url=base_url,
        env_var="",
        docs_url="",
        model_listing="openai" if base_url else "none",
        supports_tools=getattr(profile, "supports_tools", True),
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _probe_completion(entry: ProviderEntry, *, api_key: str | None, base_url: str | None, timeout: float) -> None:
    client = build_client(entry, api_key=api_key, base_url=base_url, timeout=timeout)
    model = entry.default_model or (known_model_ids(entry)[0] if known_model_ids(entry) else "")
    if not model:
        raise ProviderProbeError(f"{entry.label} has no default model to test with.", hint="Pass --model explicitly.")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        response = await client.post("/chat/completions", json=body)
        _raise_for_status(entry, response)
    except httpx.HTTPError as exc:
        raise ProviderProbeError(f"Could not reach {entry.label}: {exc}", hint="Check network access and the base URL.") from exc
    finally:
        await client.aclose()


def _raise_for_status(entry: ProviderEntry, response: httpx.Response) -> None:
    """Turn an HTTP failure into a provider error a human can act on."""

    if response.status_code < 400:
        return
    body = _short_body(response)
    status = response.status_code
    if status == 401:
        raise ProviderProbeError(
            f"{entry.label} rejected the API key (401 Unauthorized).",
            hint=f"Re-enter the key with `ua setup`, or check {entry.env_var} for typos or trailing spaces.",
        )
    if status == 403:
        raise ProviderProbeError(
            f"{entry.label} refused the request (403 Forbidden).",
            hint="The key may lack the required scope, or your account is not enabled for this model.",
        )
    if status == 404:
        raise ProviderProbeError(
            f"{entry.label} returned 404 for {response.request.url.path}.",
            hint="Check the base URL (some vendors need a region or account prefix) and the model id.",
        )
    if status == 429:
        retry_after = response.headers.get("retry-after", "")
        raise ProviderProbeError(
            f"{entry.label} rate-limited the request (429).",
            hint=f"Wait {retry_after or 'a few seconds'} and retry, or use a different model.",
        )
    if status >= 500:
        raise ProviderProbeError(f"{entry.label} is having trouble (HTTP {status}).", hint="Retry shortly; this is upstream.")
    raise ProviderProbeError(f"{entry.label} returned HTTP {status}: {body[:200]}", hint="Check the key, base URL and model id.")


def _short_body(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        if data.get("message"):
            return str(data["message"])
    return str(data)[:300]


def _tools_supported(item: dict[str, Any]) -> bool | None:
    params = item.get("supported_parameters")
    if isinstance(params, list) and params:
        return "tools" in params
    features = item.get("features")
    if isinstance(features, list) and features:
        return any(str(feature).lower() in {"tools", "function_calling", "tool_choice"} for feature in features)
    return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _per_mtok(value: Any) -> float | None:
    """OpenRouter prices are USD *per token*; show USD per million tokens."""

    number = _float(value)
    if number is None or number < 0:
        # OpenRouter reports "-1" for variable-priced models (auto-routing): that is
        # "no fixed price", not "free" - treating it as cheap would rank it first.
        return None
    return number * 1_000_000 if number < 1 else number


def _closest_model(target: str, candidates: set[str]) -> str:
    import difflib

    matches = difflib.get_close_matches(target, sorted(candidates), n=1, cutoff=0.4)
    return matches[0] if matches else sorted(candidates)[0]


def looks_like_a_key(value: str) -> bool:
    """Quick sanity check before spending a network round trip on a typo."""

    from unknown_agent.config.secrets import looks_like_api_key

    return looks_like_api_key(value)


def probe_all(entries: list[ProviderEntry], *, api_key: str | None = None) -> ValidationResult:
    """Synchronous helper used by tests and the doctor command."""

    if not entries:
        return ValidationResult(False, "No providers to probe.")
    entry = entries[0]
    return asyncio.run(validate_credential(entry, api_key=api_key))


__all__ = [
    "ModelInfo",
    "ProviderProbeError",
    "ValidationResult",
    "build_client",
    "fetch_models",
    "looks_like_a_key",
    "probe_all",
    "resolve_entry_for",
    "validate_credential",
    "validate_profile",
]
