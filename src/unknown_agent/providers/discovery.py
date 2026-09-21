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
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from unknown_agent.config.secrets import REDACTOR, looks_like_credential
from unknown_agent.errors import ConfigError, ProviderError
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers import resilience
from unknown_agent.providers.catalog import ProviderEntry, known_model_ids
from unknown_agent.providers.compatibility import normalize_compatibility

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
def build_client(entry: ProviderEntry, *, api_key: str | None, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT, compatibility: str | None = None) -> httpx.AsyncClient:
    """Create an HTTP client with the provider's auth and headers applied."""

    headers: dict[str, str] = {"Accept": "application/json", "User-Agent": "unknown-agent"}
    headers.update(entry.extra_headers)
    # Compatibility override determines header style: Anthropic uses x-api-key,
    # OpenAI-compatible uses Bearer.
    effective_protocol = normalize_effective_protocol(entry, compatibility)
    if api_key:
        if effective_protocol == "anthropic":
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


def normalize_effective_protocol(entry: ProviderEntry, compatibility: str | None) -> str:
    if compatibility:
        comp = normalize_compatibility(compatibility)
        if comp == "anthropic":
            return "anthropic"
        return "openai"
    return entry.protocol


def effective_entry_for_compatibility(entry: ProviderEntry, compatibility: str | None) -> ProviderEntry:
    """Return an entry that behaves as if it had the requested compatibility.

    This is how a generic ``openai-compatible`` profile can be asked to speak
    Anthropic without creating a second catalog entry.
    """

    if not compatibility or compatibility in {"", "auto"}:
        return entry
    comp = normalize_compatibility(compatibility)
    if comp == "anthropic" and entry.protocol != "anthropic":
        # Build a shallow copy with anthropic protocol and appropriate listing.
        return replace(entry, protocol="anthropic", model_listing="anthropic")
    if comp == "openai" and entry.protocol == "anthropic":
        return replace(entry, protocol="openai", model_listing="openai")
    return entry


def _safe_build_client(entry: ProviderEntry, *, api_key: str | None, base_url: str | None, timeout: float, compatibility: str | None) -> httpx.AsyncClient:
    """Call :func:`build_client` compatibly with monkeypatched test doubles.

    Tests replace ``build_client`` with a ``lambda`` that does not accept the new
    ``compatibility`` kwarg.  When that happens we retry without the kwarg so the
    existing suite keeps passing while real code still benefits from the header
    override.
    """

    try:
        return build_client(entry, api_key=api_key, base_url=base_url, timeout=timeout, compatibility=compatibility)
    except TypeError as exc:
        if "compatibility" in str(exc):
            return build_client(entry, api_key=api_key, base_url=base_url, timeout=timeout)  # type: ignore[call-arg]
        raise


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
        # Prefer the helper, but also probe both nesting levels explicitly
        # so a plain dict without calling the helper twice is handled.
        caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        limits_supports = (caps.get("limits") or {}).get("supports") if isinstance(caps.get("limits"), dict) else {}
        direct_supports = caps.get("supports") if isinstance(caps.get("supports"), dict) else {}
        tool_val = _tools_supported(item)
        if tool_val is None:
            if "tool_calls" in direct_supports:
                tool_val = bool(direct_supports["tool_calls"])
            elif "tool_calls" in limits_supports:
                tool_val = bool(limits_supports["tool_calls"])
        models.append(
            ModelInfo(
                id=model_id,
                name=str(item.get("name") or item.get("display_name") or model_id),
                provider=entry.key,
                context_window=_int(item.get("context_window") or item.get("context_length") or item.get("max_context_tokens") or caps.get("limits", {}).get("max_context_window_tokens")),
                max_output_tokens=_int(item.get("max_output_tokens") or item.get("max_completion_tokens") or caps.get("limits", {}).get("max_output_tokens")),
                created=_float(item.get("created") or item.get("created_at")),
                supports_tools=tool_val,
                supports_vision=_vision_supported(item),
                supports_reasoning=_reasoning_supported(item),
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
    compatibility: str | None = None,
) -> list[ModelInfo]:
    """Ask the provider for its models; fall back to the catalog when it cannot.

    Raises :class:`ProviderProbeError` when the request fails for a reason the user
    must fix (bad key, unreachable host) - the caller decides whether to offer the
    fallback list instead.
    """

    # ``auto`` probes OpenAI first, then Anthropic if the first attempt hints at
    # a protocol mismatch (404 on /models, 405, or auth header mismatch).
    if compatibility and normalize_compatibility(compatibility) == "auto":
        # For auto we try openai first; if that fails with a clear protocol hint
        # we retry as anthropic exactly once before surfacing the final error.
        last_error: ProviderProbeError | None = None
        for attempt_comp in ("openai", "anthropic"):
            try:
                return await _fetch_models_once(entry, api_key=api_key, base_url=base_url, timeout=timeout, limit=limit, compatibility=attempt_comp)
            except ProviderProbeError as exc:  # noqa: BLE001
                last_error = exc
                # Auth failures (401/403) are not a compatibility hint; surface immediately.
                msg = (exc.message or "").lower()
                if "401" in msg or "403" in msg or "unauthorized" in msg or "forbidden" in msg:
                    raise
                # If we are on openai and got 404/405 or method not allowed, retry as anthropic.
                if attempt_comp == "openai" and any(code in msg for code in ("404", "405", "not found", "method not allowed")):
                    continue
                # Otherwise surface the first error after trying both? For now raise the last.
                if attempt_comp == "anthropic":
                    raise
        if last_error is not None:
            raise last_error
        return []

    return await _fetch_models_once(entry, api_key=api_key, base_url=base_url, timeout=timeout, limit=limit, compatibility=compatibility)


async def _fetch_models_once(
    entry: ProviderEntry,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int = 500,
    compatibility: str | None = None,
) -> list[ModelInfo]:
    if entry.model_listing == "none":
        return [ModelInfo(id=model_id, name=model_id, provider=entry.key) for model_id in known_model_ids(entry)]

    eff_entry = effective_entry_for_compatibility(entry, compatibility)

    # A gateway with no free capacity answers a model listing with the same 429 it
    # answers a completion with.  One bounded retry is worth it — capacity blips are
    # usually seconds — but this is the *setup* path, so the policy is short: a user
    # staring at a panel must get an answer, not a 20s silence.
    policy = resilience.SETUP_POLICY
    attempt = 0
    while True:
        attempt += 1
        client = _safe_build_client(
            eff_entry, api_key=api_key, base_url=base_url, timeout=timeout, compatibility=compatibility
        )
        try:
            response = await client.get(_listing_url(eff_entry))
            _raise_for_status(eff_entry, response)
            payload = response.json()
        except ProviderProbeError as exc:
            failure = resilience.classify(exc, provider=eff_entry.label)
            if failure.retryable and attempt <= policy.max_retries:
                delay = policy.delay(attempt, failure)
                log.info(
                    "model listing from %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                    eff_entry.label,
                    failure.kind,
                    delay,
                    attempt,
                    policy.attempts(),
                )
                await asyncio.sleep(delay)
                continue
            raise
        except httpx.HTTPError as exc:  # network-level failure
            failure = resilience.classify(exc, provider=eff_entry.label)
            if failure.retryable and attempt <= policy.max_retries:
                await asyncio.sleep(policy.delay(attempt, failure))
                continue
            raise ProviderProbeError(
                f"Could not reach {eff_entry.label}: {type(exc).__name__}: {exc}",
                hint="Check your network connection, proxy settings, or run the provider locally.",
            ) from exc
        finally:
            await client.aclose()
        break

    parser = _PARSERS.get(eff_entry.model_listing, _parse_openai_style)
    models = parser(eff_entry, payload if isinstance(payload, dict) else {})
    models.sort(key=lambda item: item.id)
    if not models:
        raise ProviderProbeError(
            f"{eff_entry.label} returned an empty model list.",
            hint="The key may be valid but not enabled for any model, or the account needs billing set up.",
        )
    return models[:limit]


async def validate_credential(
    entry: ProviderEntry,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    compatibility: str | None = None,
) -> ValidationResult:
    """Check that a key works, preferring the cheapest possible call.

    For vendors with a model-listing endpoint that *is* the check (a 401 is
    unambiguous).  For the rest we send a one-token completion, which is the only
    honest way to know the key and the model actually work together.
    """

    if entry.api_key_required and not api_key:
        return ValidationResult(False, f"{entry.label} requires an API key.", hint=f"Set {entry.env_var} or paste the key here.")

    # Auto-detect compatibility before validating, so the check uses the right
    # header style and endpoint shape.
    if compatibility and normalize_compatibility(compatibility) == "auto":
        # Try openai first; only retry as anthropic when the failure suggests
        # a protocol mismatch, not an auth failure.
        last_result: ValidationResult | None = None
        for attempt in ("openai", "anthropic"):
            res = await validate_credential(entry, api_key=api_key, base_url=base_url, timeout=timeout, compatibility=attempt)
            if res.ok:
                return res
            last_result = res
            txt = (res.message or "").lower()
            if "401" in txt or "403" in txt or "unauthorized" in txt or "invalid api key" in txt:
                return res
            # If openai returned 404 style, let anthropic have a chance.
            if attempt == "openai" and ("404" in txt or "not found" in txt or "method not allowed" in txt):
                continue
            return res
        return last_result or ValidationResult(False, "Could not validate the credential.")

    eff_entry = effective_entry_for_compatibility(entry, compatibility)
    if eff_entry.model_listing != "none":
        try:
            models = await fetch_models(eff_entry, api_key=api_key, base_url=base_url, timeout=timeout, compatibility=compatibility)
        except ProviderProbeError as exc:
            return ValidationResult(False, exc.message, hint=exc.hint)
        return ValidationResult(
            True,
            f"{eff_entry.label} accepted the key and listed {len(models)} model(s).",
            models_available=True,
            model_count=len(models),
        )

    try:
        await _probe_completion(eff_entry, api_key=api_key, base_url=base_url, timeout=timeout, compatibility=compatibility)
    except ProviderProbeError as exc:
        return ValidationResult(False, exc.message, hint=exc.hint)
    return ValidationResult(
        True,
        f"{eff_entry.label} accepted the key (verified with a minimal request; this vendor does not list models).",
        models_available=False,
        model_count=len(known_model_ids(eff_entry)),
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
    # Compatibility is inferred from the stored profile type / extra.
    compat = _compatibility_from_profile(profile, entry)
    try:
        models = await fetch_models(entry, api_key=api_key, base_url=profile.base_url or entry.base_url, compatibility=compat)
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


def _compatibility_from_profile(profile: Any, entry: ProviderEntry) -> str | None:
    # Explicit extra field wins (added by the improved TUI).
    extra = getattr(profile, "extra", {}) or {}
    if isinstance(extra, dict) and extra.get("compatibility"):
        return str(extra["compatibility"])
    # Otherwise infer from the stored type / protocol.
    ptype = (getattr(profile, "type", "") or "").lower()
    if ptype in {"anthropic"}:
        return "anthropic"
    if ptype in {"openai-compatible", "openai"}:
        return "openai"
    # Fall back to the entry's own protocol when it is a catalog vendor.
    if entry.protocol == "anthropic":
        return "anthropic"
    if entry.protocol == "openai":
        return "openai"
    return None


def resolve_entry_for(profile: Any) -> ProviderEntry:
    """Map a configured profile back to its catalog entry (best effort)."""

    from unknown_agent.providers.catalog import CATALOG, resolve_entry

    direct = resolve_entry(getattr(profile, "type", ""))
    if direct is not None:
        return direct
    base_url = (profile.base_url or "").rstrip("/")
    # Compatibility may be stored in extra; use it to pick the right protocol
    # rather than assuming generic openai-compatible always means openai protocol.
    compat = None
    extra = getattr(profile, "extra", {}) or {}
    if isinstance(extra, dict):
        compat = extra.get("compatibility") or extra.get("compat")
    ptype = getattr(profile, "type", "")
    wanted_protocol = "openai"
    if compat:
        wanted_protocol = "anthropic" if normalize_compatibility(str(compat)) == "anthropic" else "openai"
    elif ptype == "anthropic":
        wanted_protocol = "anthropic"
    for entry in CATALOG:
        if entry.protocol == wanted_protocol and entry.base_url.rstrip("/") == base_url:
            return entry
    for entry in CATALOG:
        if entry.protocol == wanted_protocol and entry.key == ptype:
            return entry
    from unknown_agent.providers.catalog import ProviderEntry as _Entry

    # Determine model listing based on compatibility
    if wanted_protocol == "anthropic":
        listing: str = "anthropic"
    else:
        listing = "openai" if base_url else "none"
    return _Entry(
        key=ptype or "custom",
        label=ptype or "custom",
        protocol=wanted_protocol if wanted_protocol in {"anthropic", "ollama", "offline"} else "openai",
        base_url=base_url,
        env_var="",
        docs_url="",
        model_listing=listing,  # type: ignore[arg-type]
        supports_tools=getattr(profile, "supports_tools", True),
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _probe_completion(entry: ProviderEntry, *, api_key: str | None, base_url: str | None, timeout: float, compatibility: str | None = None) -> None:
    eff_entry = effective_entry_for_compatibility(entry, compatibility)
    client = _safe_build_client(eff_entry, api_key=api_key, base_url=base_url, timeout=timeout, compatibility=compatibility)
    model = eff_entry.default_model or (known_model_ids(eff_entry)[0] if known_model_ids(eff_entry) else "")
    if not model:
        raise ProviderProbeError(f"{eff_entry.label} has no default model to test with.", hint="Pass --model explicitly.")
    # Anthropic and OpenAI use different probe shapes.
    if eff_entry.protocol == "anthropic" or normalize_compatibility(compatibility or "") == "anthropic":
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        path = "/messages"
    else:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        path = "/chat/completions"
    try:
        response = await client.post(path, json=body)
        _raise_for_status(eff_entry, response)
    except httpx.HTTPError as exc:
        raise ProviderProbeError(f"Could not reach {eff_entry.label}: {exc}", hint="Check network access and the base URL.") from exc
    finally:
        await client.aclose()


def _raise_for_status(entry: ProviderEntry, response: httpx.Response) -> None:
    """Turn an HTTP failure into a provider error a human can act on.

    The classified failure is attached to the exception (``status_code``,
    ``retry_after``, ``kind``) so callers can make a real decision instead of
    re-parsing prose.  This is what was missing when a setup 429 reached the panel:
    the panel could not tell "wait and retry" from "your key is wrong".
    """

    if response.status_code < 400:
        return
    body = _short_body(response)
    failure = resilience.classify(
        f"HTTP {response.status_code}: {body[:300]}",
        status_code=response.status_code,
        headers=response.headers,
        provider=entry.label,
    )
    # Keep the endpoint-specific hints that used to live here: they name the exact
    # thing to fix, which the generic taxonomy cannot know.
    hint = failure.hint
    if response.status_code == 401:
        hint = f"Re-enter the key with `ua setup`, or check {entry.env_var} for typos or trailing spaces."
    elif response.status_code == 404:
        hint = "Check the base URL (some vendors need a region or account prefix) and the model id."
    error = ProviderProbeError(failure.summary(), hint=hint, context=failure.to_dict())
    error.status_code = response.status_code
    error.retry_after = failure.retry_after
    error.kind = failure.kind
    raise error


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
    # Direct OpenAI-style supported_parameters list
    params = item.get("supported_parameters")
    if isinstance(params, list) and params:
        return "tools" in params
    features = item.get("features")
    if isinstance(features, list) and features:
        return any(str(feature).lower() in {"tools", "function_calling", "tool_choice"} for feature in features)
    # UNLI-style capabilities.supports.tool_calls or capabilities.limits.supports.tool_calls
    caps = item.get("capabilities")
    if isinstance(caps, dict):
        for source in (caps.get("supports"), (caps.get("limits") or {}).get("supports") if isinstance(caps.get("limits"), dict) else None):
            if isinstance(source, dict) and "tool_calls" in source:
                return bool(source["tool_calls"])
        # Also check limits direct booleans
        limits = caps.get("limits")
        if isinstance(limits, dict) and isinstance(limits.get("supports"), dict):
            pass
    return None


def _vision_supported(item: dict[str, Any]) -> bool:
    caps = item.get("capabilities")
    if isinstance(caps, dict):
        for source in (caps.get("supports"), (caps.get("limits") or {}).get("supports") if isinstance(caps.get("limits"), dict) else None):
            if isinstance(source, dict) and "vision" in source:
                return bool(source["vision"])
        limits = caps.get("limits")
        if isinstance(limits, dict) and "vision" in limits:
            return True
        # UNLI nests supports under limits.supports already handled above
    # Fallback to name heuristics
    name = str(item.get("name") or item.get("id") or "").lower()
    return "vision" in name or "vl" in name


def _reasoning_supported(item: dict[str, Any]) -> bool:
    caps = item.get("capabilities")
    if isinstance(caps, dict):
        # UNLI exposes reasoning as part of capabilities but not explicit flag;
        # treat presence of limits.max_output_tokens large as hint
        return False
    return False


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
