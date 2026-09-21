"""The provider catalog: one entry per supported vendor, with real behaviour.

Every provider in this catalog is *usable*, not decorative.  An entry declares

* how to authenticate (env var, header style, extra headers),
* where the API lives and how to override it (``base_url``),
* whether the vendor can list its models over HTTP, and where that endpoint is,
* the request/response dialect the adapter must speak (``protocol``),
* capability flags that the rest of the product reads instead of guessing,
* a curated fallback list for vendors that cannot list models.

Adding a provider is a data entry in :data:`CATALOG` plus, only when the vendor
speaks a different dialect, one adapter class - never a change spread across the
codebase.  ``ua setup`` and ``ua providers`` are generated from this file, so
catalog and CLI can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Wire protocol an adapter must implement.
Protocol = Literal["openai", "anthropic", "ollama", "offline"]

#: How this vendor exposes its model list.
ModelListing = Literal["openai", "openrouter", "anthropic", "ollama", "none"]


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """Everything the product knows about one vendor."""

    key: str
    label: str
    protocol: Protocol
    base_url: str
    env_var: str
    docs_url: str
    #: ``store:<key>`` / ``env:<VAR>`` reference written into the config.
    credential_hint: str = ""
    api_key_required: bool = True
    model_listing: ModelListing = "openai"
    models_path: str | None = None
    default_model: str = ""
    known_models: tuple[str, ...] = ()
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_json_mode: bool = True
    supports_reasoning: bool = False
    supports_vision: bool = False
    local: bool = False
    notes: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Query parameters every request needs (Azure's ``api-version``).
    query_params: dict[str, str] = field(default_factory=dict)
    #: ``base_url`` must be supplied by the user (Azure resource, self-hosting).
    requires_base_url: bool = False
    #: Local providers only make sense when the user actually runs them.
    optional: bool = False

    @property
    def listing_path(self) -> str | None:
        if self.model_listing == "none":
            return None
        if self.models_path:
            return self.models_path
        defaults = {
            "openai": "/models",
            "openrouter": "/models",
            "anthropic": "/models",
            "ollama": "/api/tags",
        }
        return defaults[self.model_listing]

    @property
    def profile_defaults(self) -> dict[str, object]:
        """Values written into ``[providers.<name>]`` by onboarding."""

        payload: dict[str, object] = {
            # The vendor key is stored as the profile type: it is more useful than the
            # dialect (list/test/doctor show "OpenRouter", not "openai-compatible"), and
            # the registry maps vendor -> adapter protocol when it builds the provider.
            "type": self.key,
            "model": self.default_model or (self.known_models[0] if self.known_models else ""),
            "base_url": self.base_url or None,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "json_mode": self.supports_json_mode,
        }
        if self.requires_base_url or self.base_url == "":
            payload["base_url"] = None
        if self.api_key_required and self.credential_hint:
            payload["api_key"] = self.credential_hint
        if self.extra_headers:
            payload["headers"] = dict(self.extra_headers)
        if self.query_params:
            payload["extra"] = {"query": dict(self.query_params)}
        return {key: value for key, value in payload.items() if value not in (None, "", {}, [])}

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "env_var": self.env_var,
            "docs_url": self.docs_url,
            "local": self.local,
            "api_key_required": self.api_key_required,
            "model_listing": self.model_listing,
            "default_model": self.default_model,
            "known_models": list(self.known_models),
            "capabilities": {
                "tools": self.supports_tools,
                "streaming": self.supports_streaming,
                "json_mode": self.supports_json_mode,
                "reasoning": self.supports_reasoning,
                "vision": self.supports_vision,
                "model_listing": self.model_listing != "none",
            },
            "notes": self.notes,
        }


def _entry(**kwargs: object) -> ProviderEntry:
    return ProviderEntry(**kwargs)  # type: ignore[arg-type]


#: Ordered so the most common first-run choices appear first.
CATALOG: tuple[ProviderEntry, ...] = (
    _entry(
        key="openrouter",
        label="OpenRouter",
        protocol="openai",
        base_url="https://openrouter.ai/api/v1",
        env_var="OPENROUTER_API_KEY",
        credential_hint="store:openrouter",
        model_listing="openrouter",
        default_model="anthropic/claude-sonnet-4",
        docs_url="https://openrouter.ai/docs",
        supports_reasoning=True,
        supports_vision=True,
        notes="One key, hundreds of models across vendors. Prices and context windows come from /models.",
    ),
    _entry(
        key="openai",
        label="OpenAI",
        protocol="openai",
        base_url="https://api.openai.com/v1",
        env_var="OPENAI_API_KEY",
        credential_hint="store:openai",
        default_model="gpt-4.1-mini",
        docs_url="https://platform.openai.com/docs",
        supports_reasoning=True,
        supports_vision=True,
    ),
    _entry(
        key="anthropic",
        label="Anthropic",
        protocol="anthropic",
        base_url="https://api.anthropic.com/v1",
        env_var="ANTHROPIC_API_KEY",
        credential_hint="store:anthropic",
        model_listing="anthropic",
        default_model="claude-sonnet-4-5",
        docs_url="https://docs.anthropic.com",
        supports_reasoning=True,
        supports_vision=True,
    ),
    _entry(
        key="mistral",
        label="Mistral AI",
        protocol="openai",
        base_url="https://api.mistral.ai/v1",
        env_var="MISTRAL_API_KEY",
        credential_hint="store:mistral",
        default_model="mistral-large-latest",
        docs_url="https://docs.mistral.ai",
        notes="Native function calling on the large/medium/codestral families.",
    ),
    _entry(
        key="deepseek",
        label="DeepSeek",
        protocol="openai",
        base_url="https://api.deepseek.com/v1",
        env_var="DEEPSEEK_API_KEY",
        credential_hint="store:deepseek",
        default_model="deepseek-chat",
        docs_url="https://api-docs.deepseek.com",
        known_models=("deepseek-chat", "deepseek-reasoner"),
        supports_reasoning=True,
    ),
    _entry(
        key="xai",
        label="xAI (Grok)",
        protocol="openai",
        base_url="https://api.x.ai/v1",
        env_var="XAI_API_KEY",
        credential_hint="store:xai",
        default_model="grok-3-mini",
        docs_url="https://docs.x.ai",
        known_models=("grok-3", "grok-3-mini", "grok-2-1212", "grok-2-vision-1212"),
        supports_reasoning=True,
        supports_vision=True,
    ),
    _entry(
        key="groq",
        label="Groq",
        protocol="openai",
        base_url="https://api.groq.com/openai/v1",
        env_var="GROQ_API_KEY",
        credential_hint="store:groq",
        default_model="llama-3.3-70b-versatile",
        docs_url="https://console.groq.com/docs",
        notes="Extremely fast inference for open models; tool calling supported on Llama/Mixtral families.",
    ),
    _entry(
        key="google",
        label="Google Gemini",
        protocol="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_var="GEMINI_API_KEY",
        credential_hint="store:google",
        default_model="gemini-2.0-flash",
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
        supports_reasoning=True,
        supports_vision=True,
        notes="Uses Google's OpenAI-compatible endpoint, so tools/streaming work unchanged.",
    ),
    _entry(
        key="together",
        label="Together AI",
        protocol="openai",
        base_url="https://api.together.xyz/v1",
        env_var="TOGETHER_API_KEY",
        credential_hint="store:together",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        docs_url="https://docs.together.ai",
    ),
    _entry(
        key="fireworks",
        label="Fireworks AI",
        protocol="openai",
        base_url="https://api.fireworks.ai/inference/v1",
        env_var="FIREWORKS_API_KEY",
        credential_hint="store:fireworks",
        model_listing="none",
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        docs_url="https://docs.fireworks.ai",
        known_models=(
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/deepseek-v3",
            "accounts/fireworks/models/qwen2p5-coder-32b-instruct",
        ),
        notes="Model ids are account-scoped paths; the catalog suggests defaults, the API is model-in, model-out.",
    ),
    _entry(
        key="cerebras",
        label="Cerebras",
        protocol="openai",
        base_url="https://api.cerebras.ai/v1",
        env_var="CEREBRAS_API_KEY",
        credential_hint="store:cerebras",
        default_model="llama-3.3-70b",
        docs_url="https://inference-docs.cerebras.ai",
    ),
    _entry(
        key="nvidia",
        label="NVIDIA NIM",
        protocol="openai",
        base_url="https://integrate.api.nvidia.com/v1",
        env_var="NVIDIA_API_KEY",
        credential_hint="store:nvidia",
        default_model="meta/llama-3.3-70b-instruct",
        docs_url="https://docs.nvidia.com/nim",
    ),
    _entry(
        key="moonshot",
        label="Moonshot (Kimi)",
        protocol="openai",
        base_url="https://api.moonshot.ai/v1",
        env_var="MOONSHOT_API_KEY",
        credential_hint="store:moonshot",
        default_model="moonshot-v1-32k",
        docs_url="https://platform.moonshot.ai/docs",
        known_models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k", "kimi-k2-0711-preview"),
    ),
    _entry(
        key="qwen",
        label="Alibaba Qwen (DashScope)",
        protocol="openai",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env_var="DASHSCOPE_API_KEY",
        credential_hint="store:qwen",
        default_model="qwen-max",
        docs_url="https://www.alibabacloud.com/help/en/model-studio",
        known_models=("qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-coder-32b-instruct"),
    ),
    _entry(
        key="zhipu",
        label="Zhipu GLM",
        protocol="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        env_var="ZHIPU_API_KEY",
        credential_hint="store:zhipu",
        default_model="glm-4-plus",
        docs_url="https://open.bigmodel.cn/dev/api",
        known_models=("glm-4-plus", "glm-4-air", "glm-4-flash"),
    ),
    _entry(
        key="cohere",
        label="Cohere",
        protocol="openai",
        base_url="https://api.cohere.ai/compatibility/v1",
        env_var="COHERE_API_KEY",
        credential_hint="store:cohere",
        default_model="command-r-plus",
        docs_url="https://docs.cohere.com/docs/compatibility-api",
        known_models=("command-r-plus", "command-r", "command-a-03-2025"),
    ),
    _entry(
        key="perplexity",
        label="Perplexity",
        protocol="openai",
        base_url="https://api.perplexity.ai",
        env_var="PERPLEXITY_API_KEY",
        credential_hint="store:perplexity",
        model_listing="none",
        default_model="sonar-pro",
        docs_url="https://docs.perplexity.ai",
        known_models=("sonar", "sonar-pro", "sonar-reasoning"),
        supports_tools=False,
        notes="Search-answer models: great for research answers, not for agentic file editing.",
        optional=True,
    ),
    _entry(
        key="azure-openai",
        label="Azure OpenAI",
        protocol="openai",
        base_url="",
        env_var="AZURE_OPENAI_API_KEY",
        credential_hint="store:azure-openai",
        model_listing="none",
        docs_url="https://learn.microsoft.com/azure/ai-services/openai",
        requires_base_url=True,
        notes="Supply your resource endpoint, e.g. https://<resource>.openai.azure.com/openai/v1, and a deployment name as the model.",
        query_params={"api-version": "2024-10-21"},
    ),
    _entry(
        key="ollama",
        label="Ollama (local)",
        protocol="ollama",
        base_url="http://127.0.0.1:11434",
        env_var="OLLAMA_API_KEY",
        model_listing="ollama",
        default_model="qwen2.5-coder:7b",
        docs_url="https://ollama.com/docs",
        api_key_required=False,
        local=True,
        supports_vision=False,
        notes="Runs on your machine: no API key, no cost, no data leaving the box.",
    ),
    _entry(
        key="lmstudio",
        label="LM Studio (local)",
        protocol="openai",
        base_url="http://127.0.0.1:1234/v1",
        env_var="LMSTUDIO_API_KEY",
        model_listing="openai",
        default_model="",
        docs_url="https://lmstudio.ai/docs",
        api_key_required=False,
        local=True,
        supports_tools=False,
        notes="OpenAI-compatible local server; start it from the LM Studio app first.",
    ),
    _entry(
        key="offline",
        label="Offline (no model)",
        protocol="offline",
        base_url="",
        env_var="",
        model_listing="none",
        default_model="offline-deterministic",
        docs_url="https://github.com/unknown-agent/unknown-agent",
        api_key_required=False,
        supports_tools=True,
        notes="Deterministic provider used for CI and evaluation: exercises the whole loop without a model.",
    ),
)

CATALOG_BY_KEY: dict[str, ProviderEntry] = {entry.key: entry for entry in CATALOG}

#: Aliases so users are not punished for guessing a vendor's name.
ALIASES: dict[str, str] = {
    "grok": "xai",
    "gemini": "google",
    "google-gemini": "google",
    "kimi": "moonshot",
    "zai": "zhipu",
    "glm": "zhipu",
    "dashscope": "qwen",
    "alibaba": "qwen",
    "azure": "azure-openai",
    "openai-compatible": "openai-compatible",
    "harness": "offline",
    "local": "ollama",
    "lm-studio": "lmstudio",
}

#: Types the registry knows how to build, mapped to their protocol adapter.
PROTOCOL_BY_TYPE: dict[str, Protocol] = {
    "openai": "openai",
    "openai-compatible": "openai",
    "anthropic": "anthropic",
    "ollama": "ollama",
    "offline": "offline",
}


def resolve_entry(name: str) -> ProviderEntry | None:
    """Look up a catalog entry by key, alias, or label (case-insensitive)."""

    if not name:
        return None
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key in CATALOG_BY_KEY:
        return CATALOG_BY_KEY[key]
    for entry in CATALOG:
        if entry.label.lower() == key or entry.key.replace("-", "") == key.replace("-", ""):
            return entry
    return None


def known_model_ids(entry: ProviderEntry) -> list[str]:
    """Fallback model list for vendors that cannot enumerate their models."""

    models = list(entry.known_models)
    if entry.default_model and entry.default_model not in models:
        models.insert(0, entry.default_model)
    return models


def catalog_payload() -> list[dict[str, object]]:
    """Machine-readable catalog for ``ua providers catalog --json``."""

    return [entry.to_dict() for entry in CATALOG]


def profile_name_for(entry: ProviderEntry, existing: dict[str, object]) -> str:
    """Pick a free profile name: ``openrouter``, then ``openrouter-2``, ..."""

    if entry.key not in existing:
        return entry.key
    index = 2
    while f"{entry.key}-{index}" in existing:
        index += 1
    return f"{entry.key}-{index}"


__all__ = [
    "ALIASES",
    "CATALOG",
    "CATALOG_BY_KEY",
    "PROTOCOL_BY_TYPE",
    "ProviderEntry",
    "catalog_payload",
    "known_model_ids",
    "profile_name_for",
    "resolve_entry",
]
