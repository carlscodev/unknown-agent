"""Vision: images as first-class inputs to the agent.

Three layers, each usable alone:

``images``   ingestion - files/URLs/data-URLs -> validated, size-capped,
             provider-ready attachments with an audit copy in artifacts
``service``  the product operation - "analyse these images with a
             vision-capable model", including capability-aware routing to the
             [vision] fallback profile and honest failures when nothing can see
``tools``    the agent-facing tools (analyze_image, compare_images,
             screenshot_analyze) built on the service

Providers already carry ``Message.images`` (data URLs / https URLs); the
OpenAI-dialect adapter encodes them as ``image_url`` parts and the Anthropic
adapter as base64 ``image`` blocks - see ``providers/``.
"""

from unknown_agent.vision.images import ImageAttachment, load_image, data_url_for
from unknown_agent.vision.service import VisionService, VisionUnavailable

__all__ = ["ImageAttachment", "load_image", "data_url_for", "VisionService", "VisionUnavailable"]
