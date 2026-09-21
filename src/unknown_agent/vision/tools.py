"""Agent-facing vision tools.

Three tools, all capability-gated through :class:`VisionService`:

* ``analyze_image``      one or more images + a question
* ``compare_images``     reference vs candidate (design QA, regression checks)
* ``screenshot_analyze`` capture the running app with the browser, then analyse
                         - the bridge between browser validation and vision

When no vision-capable model is reachable the tools return a *failure result
with the routing explanation*, so the model learns the limitation instead of
retrying blindly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.errors import ToolError
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext
from unknown_agent.vision.images import load_image
from unknown_agent.vision.service import VisionService, VisionUnavailable

__all__ = ["AnalyzeImageTool", "CompareImagesTool", "ScreenshotAnalyzeTool", "build_tools"]


def _service(ctx: ToolContext) -> VisionService:
    existing = (ctx.extra or {}).get("vision_service")
    if existing is not None:
        return existing
    return VisionService(ctx.settings)


def _load_many(ctx: ToolContext, sources: list[str]) -> list[Any]:
    return [
        load_image(
            source,
            ctx.settings.vision,
            project_root=ctx.project_root,
            from_tool=True,
            artifacts_dir=ctx.artifacts_dir,
        )
        for source in sources
    ]


class AnalyzeImageTool(Tool):
    name = "analyze_image"
    description = (
        "Analyze one or more images (screenshots, mockups, diagrams) with a vision-capable model. "
        "Ask a specific question for a specific answer: visible text, layout, states, defects."
    )
    category = "vision"
    risk = RiskLevel.NETWORK
    tags = ("vision",)
    timeout_s = 120.0
    parameters = (
        ParameterSpec("paths", type="array", description="Image files (project-relative or absolute).", required=True),
        ParameterSpec("question", description="What to determine from the image(s).", required=True),
    )
    examples = (
        'analyze_image(paths=[".unknown/artifacts/home.png"], question="Is the nav menu overlapping the logo?")',
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        paths: list[str] = kwargs.get("paths") or []
        question: str = kwargs.get("question") or ""
        if not paths:
            return ToolResult.failure("No image paths given.")
        try:
            images = _load_many(ctx, paths)
        except ToolError as exc:
            return ToolResult.failure(str(exc))
        service = _service(ctx)
        try:
            answer = await service.analyze(images, question)
        except VisionUnavailable as exc:
            return ToolResult.failure(f"{exc.message} Hint: {exc.hint}")
        finally:
            if (ctx.extra or {}).get("vision_service") is None:
                await service.aclose()
        return ToolResult.success(
            answer.text,
            metadata={"model": answer.model, "routed": answer.routed},
            images=answer.images,
        )


class CompareImagesTool(Tool):
    name = "compare_images"
    description = (
        "Compare a reference image (design mockup, previous screenshot) against a candidate "
        "(current render). Reports concrete matches and mismatches for design QA."
    )
    category = "vision"
    risk = RiskLevel.NETWORK
    tags = ("vision", "design")
    timeout_s = 120.0
    parameters = (
        ParameterSpec("reference", description="The reference image path.", required=True),
        ParameterSpec("candidate", description="The candidate image path.", required=True),
        ParameterSpec("focus", description="Optional aspect to focus on (spacing, colors, typography, content)."),
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            images = _load_many(ctx, [kwargs["reference"], kwargs["candidate"]])
        except ToolError as exc:
            return ToolResult.failure(str(exc))
        focus = kwargs.get("focus") or "overall fidelity"
        question = (
            "Image 1 is the REFERENCE, image 2 is the CANDIDATE. Compare them with focus on "
            f"{focus}. List (a) what matches, (b) concrete mismatches with locations, "
            "(c) a verdict: acceptable or needs changes."
        )
        service = _service(ctx)
        try:
            answer = await service.analyze(images, question)
        except VisionUnavailable as exc:
            return ToolResult.failure(f"{exc.message} Hint: {exc.hint}")
        finally:
            if (ctx.extra or {}).get("vision_service") is None:
                await service.aclose()
        return ToolResult.success(answer.text, metadata={"model": answer.model, "routed": answer.routed})


class ScreenshotAnalyzeTool(Tool):
    name = "screenshot_analyze"
    description = (
        "Take a screenshot of a URL with the managed browser, then analyze it with a "
        "vision-capable model. The visual half of browser validation."
    )
    category = "vision"
    risk = RiskLevel.NETWORK
    tags = ("vision", "browser")
    requires_extra = "browser"
    timeout_s = 180.0
    concurrency_safe = False
    parameters = (
        ParameterSpec("url", description="URL to open (uses [browser] base_url when relative).", required=True),
        ParameterSpec("question", description="What to check in the rendered page.", required=True),
        ParameterSpec("full_page", type="boolean", description="Capture the whole page, not just the viewport.", default=False),
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        if ctx.browser is None:
            return ToolResult.failure(
                "No browser session available.",
                metadata={"hint": "browser tools need Playwright: `ua doctor --install-browsers`"},
            )
        url: str = kwargs["url"]
        question: str = kwargs["question"]
        try:
            shot = await ctx.browser.screenshot(url, full_page=bool(kwargs.get("full_page")))
        except Exception as exc:  # noqa: BLE001 - browser failures are domain failures
            return ToolResult.failure(f"screenshot failed: {exc}")
        shot_path = Path(getattr(shot, "path", None) or shot)
        try:
            images = [
                load_image(
                    str(shot_path),
                    ctx.settings.vision,
                    project_root=ctx.project_root,
                    from_tool=False,  # we made this file ourselves
                    artifacts_dir=ctx.artifacts_dir,
                )
            ]
        except ToolError as exc:
            return ToolResult.failure(str(exc))
        service = _service(ctx)
        try:
            answer = await service.analyze(images, f"This is a screenshot of {url}. {question}")
        except VisionUnavailable as exc:
            return ToolResult.failure(f"{exc.message} Hint: {exc.hint}")
        finally:
            if (ctx.extra or {}).get("vision_service") is None:
                await service.aclose()
        result = ToolResult.success(answer.text, metadata={"model": answer.model, "url": url})
        result.artifacts = [str(shot_path)]
        return result


def build_tools() -> list[Tool]:
    return [AnalyzeImageTool(), CompareImagesTool(), ScreenshotAnalyzeTool()]
