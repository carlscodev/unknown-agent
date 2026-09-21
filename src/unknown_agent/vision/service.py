"""The vision service: capability-aware image analysis.

One operation - ``analyze(images, question)`` - with the routing rules the
product promises:

1. If the *active* model is vision-capable, use it.
2. Otherwise, if ``[vision] fallback_profile`` names a vision-capable profile,
   build that provider for this one request and use it.
3. Otherwise raise :class:`VisionUnavailable` whose message says exactly which
   model cannot see and which knobs exist - the "explain the limitation
   cleanly" path.

The service is deliberately provider-shaped, not loop-shaped: it issues a
single ``ModelRequest`` with ``Message.images`` attached and returns the text.
Multi-turn visual debugging lives in the agent loop (the loop attaches images
to observations); this service is what the tools and the CLI call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.core.models import Message
from unknown_agent.errors import UnknownAgentError
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.base import ModelRequest
from unknown_agent.providers.capabilities import resolve_capabilities
from unknown_agent.vision.images import ImageAttachment

log = get_logger("vision")

__all__ = ["VisionService", "VisionUnavailable", "VisionAnswer"]


class VisionUnavailable(UnknownAgentError):
    code = "vision_unavailable"
    default_hint = (
        "Pick a vision-capable model (`ua models list` marks them), set "
        "[vision] fallback_profile to one, or override with "
        "[providers.<name>] supports_vision = true if you know better."
    )


@dataclass(slots=True)
class VisionAnswer:
    text: str
    model: str
    provider_profile: str
    routed: bool  # True when the fallback profile answered instead of the active model
    images: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "provider_profile": self.provider_profile,
            "routed": self.routed,
            "images": self.images,
            "usage": self.usage,
        }


_SYSTEM = (
    "You are the vision analyst of a software-engineering agent. Answer precisely "
    "and concretely about the supplied image(s): visible text, layout structure, "
    "components, states, defects, differences. When asked to compare, be explicit "
    "about what matches and what does not. Never invent content that is not visible."
)


class VisionService:
    """Route image analysis to a model that can actually see."""

    def __init__(self, settings: Settings, *, active_provider: Any = None) -> None:
        self.settings = settings
        #: The session's already-built provider (reused when it can see).
        self.active_provider = active_provider
        self._fallback_provider: Any = None
        self._owns_fallback = False

    # -- capability routing ---------------------------------------------------
    def route(self) -> tuple[str, bool]:
        """Return ``(profile_name, is_fallback)`` or raise :class:`VisionUnavailable`."""

        active_name = self.settings.provider
        active_caps = resolve_capabilities(self.settings.active_provider, provider_name=active_name)
        if active_caps.supports_vision:
            return active_name, False
        fallback = (self.settings.vision.fallback_profile or "").strip()
        if fallback and fallback in self.settings.providers:
            fb_caps = resolve_capabilities(self.settings.providers[fallback], provider_name=fallback)
            if fb_caps.supports_vision:
                return fallback, True
            raise VisionUnavailable(
                f"The active model ({active_caps.model}) is not vision-capable, and the configured "
                f"fallback profile {fallback!r} ({fb_caps.model}) is not either."
            )
        raise VisionUnavailable(
            f"The active model ({active_caps.model or 'unknown'}) is not vision-capable and no "
            "[vision] fallback_profile is configured."
        )

    async def _provider_for(self, profile: str, *, is_fallback: bool) -> Any:
        if not is_fallback and self.active_provider is not None:
            return self.active_provider
        if self._fallback_provider is not None:
            return self._fallback_provider
        from unknown_agent.providers.registry import build_provider

        config = self.settings.providers[profile]
        provider = build_provider(config, name=profile)
        if is_fallback or self.active_provider is None:
            self._fallback_provider = provider
            self._owns_fallback = True
        return provider

    # -- the operation --------------------------------------------------------
    async def analyze(
        self,
        images: list[ImageAttachment],
        question: str,
        *,
        system: str | None = None,
        max_output_tokens: int = 2000,
    ) -> VisionAnswer:
        if not self.settings.vision.enabled:
            raise VisionUnavailable("[vision] is disabled.")
        if not images:
            raise VisionUnavailable("No images were attached.")
        limit = self.settings.vision.max_images_per_request
        if len(images) > limit:
            raise VisionUnavailable(f"{len(images)} images exceed [vision] max_images_per_request = {limit}.")

        profile, routed = self.route()
        provider = await self._provider_for(profile, is_fallback=routed)
        message = Message.user(question or "Describe the image(s) precisely.")
        message.images = [image.url for image in images]
        request = ModelRequest(
            messages=[Message.system(system or _SYSTEM), message],
            max_output_tokens=max_output_tokens,
            metadata={"surface": "vision"},
        )
        response = await provider.complete(request)
        if getattr(response, "error", None):
            raise VisionUnavailable(f"The vision model reported an error: {response.error}")
        usage = getattr(response, "usage", None)
        return VisionAnswer(
            text=response.content or "",
            model=getattr(provider, "model", ""),
            provider_profile=profile,
            routed=routed,
            images=[image.to_dict() for image in images],
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
            },
        )

    async def aclose(self) -> None:
        if self._owns_fallback and self._fallback_provider is not None and hasattr(self._fallback_provider, "aclose"):
            try:
                await self._fallback_provider.aclose()
            except Exception:  # noqa: BLE001 - closing is best-effort
                log.debug("closing the vision fallback provider failed", exc_info=True)
        self._fallback_provider = None
