"""Vision + capability-resolution + computer-use tests.

Everything here is offline: providers are faked, images are synthesized
byte-by-byte, and the computer-use driver is a scripted stub.  The live
provider round-trip was verified manually against OpenRouter; these tests pin
the *behaviour contracts* - routing order, gating ladders, encoding dialects -
so refactors cannot silently break them.
"""

from __future__ import annotations

import asyncio
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from unknown_agent.config.models import ProviderConfig, Settings
from unknown_agent.errors import ToolError

# --------------------------------------------------------------------------- #
# image fixtures (hand-built, no Pillow required)
# --------------------------------------------------------------------------- #


def make_png(width: int = 1, height: int = 1) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = zlib.compress(b"\x00" + b"\xff\x00\x00" * width)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


GIF = b"GIF89a" + struct.pack("<HH", 3, 5) + b"\x00" * 20
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 20


# --------------------------------------------------------------------------- #
# capability resolution
# --------------------------------------------------------------------------- #
class TestCapabilities:
    def _caps(self, **kwargs: Any):
        from unknown_agent.providers.capabilities import resolve_capabilities

        return resolve_capabilities(ProviderConfig(**kwargs), provider_name="test")

    def test_vision_heuristic_recognises_multimodal_ids(self) -> None:
        for model in ("gpt-4o", "gpt-4o-mini", "claude-sonnet-4-20250514", "gemini-2.0-flash", "llava:13b", "qwen2.5-vl-7b"):
            caps = self._caps(model=model)
            assert caps.supports_vision, model
            assert caps.vision.source == "heuristic"

    def test_vision_defaults_to_false_for_unknown_models(self) -> None:
        caps = self._caps(model="mystery-model-3000")
        assert not caps.supports_vision
        assert caps.vision.source == "default"

    def test_profile_override_beats_everything(self) -> None:
        yes = self._caps(model="mystery-model-3000", supports_vision=True)
        assert yes.supports_vision and yes.vision.source == "profile"
        no = self._caps(model="gpt-4o", supports_vision=False)
        assert not no.supports_vision and no.vision.source == "profile"

    def test_model_metadata_beats_heuristic(self) -> None:
        from unknown_agent.providers.capabilities import resolve_capabilities
        from unknown_agent.providers.discovery import ModelInfo

        info = ModelInfo(id="mystery-model-3000", supports_vision=True)
        caps = resolve_capabilities(ProviderConfig(model="mystery-model-3000"), provider_name="t", model_info=info)
        assert caps.supports_vision
        assert caps.vision.source == "model-metadata"

    def test_computer_use_hints(self) -> None:
        assert self._caps(model="claude-sonnet-4-20250514", type="anthropic").supports_computer_use
        assert not self._caps(model="gpt-4o").supports_computer_use

    def test_to_dict_carries_value_and_source(self) -> None:
        payload = self._caps(model="gpt-4o").to_dict()
        assert payload["vision"] == {"value": True, "source": "heuristic"}
        assert set(payload) >= {"tools", "vision", "reasoning", "streaming", "computer_use"}


# --------------------------------------------------------------------------- #
# image ingestion
# --------------------------------------------------------------------------- #
class TestImages:
    def test_sniff_and_dimensions(self) -> None:
        from unknown_agent.vision.images import image_dimensions, sniff_format

        assert sniff_format(make_png(7, 3)) == ("png", "image/png")
        assert image_dimensions(make_png(7, 3)) == (7, 3)
        assert sniff_format(GIF) == ("gif", "image/gif")
        assert image_dimensions(GIF) == (3, 5)
        assert sniff_format(WEBP) == ("webp", "image/webp")
        assert sniff_format(b"not an image") is None

    def test_load_file_produces_data_url(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image

        target = tmp_path / "shot.png"
        target.write_bytes(make_png(4, 2))
        att = load_image(str(target), Settings().vision, project_root=tmp_path)
        assert att.kind == "file"
        assert att.mime == "image/png"
        assert (att.width, att.height) == (4, 2)
        assert att.url.startswith("data:image/png;base64,")

    def test_relative_paths_resolve_against_project_root(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image

        (tmp_path / "img").mkdir()
        (tmp_path / "img" / "a.png").write_bytes(make_png())
        att = load_image("img/a.png", Settings().vision, project_root=tmp_path)
        assert att.kind == "file"

    def test_missing_file_and_non_image_are_refused(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image

        with pytest.raises(ToolError, match="not found"):
            load_image(str(tmp_path / "ghost.png"), Settings().vision, project_root=tmp_path)
        bad = tmp_path / "fake.png"
        bad.write_bytes(b"plain text pretending")
        with pytest.raises(ToolError, match="not a supported image"):
            load_image(str(bad), Settings().vision, project_root=tmp_path)

    def test_allowed_sources_gate_each_kind(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image

        config = Settings().vision.model_copy(update={"allowed_sources": ["file"]})
        (tmp_path / "ok.png").write_bytes(make_png())
        assert load_image(str(tmp_path / "ok.png"), config, project_root=tmp_path).kind == "file"
        with pytest.raises(ToolError, match="disabled"):
            load_image("https://example.com/x.png", config, project_root=tmp_path)
        with pytest.raises(ToolError, match="disabled"):
            load_image("data:image/png;base64,AAAA", config, project_root=tmp_path)

    def test_tool_initiated_remote_urls_are_blocked_by_default(self, tmp_path: Path) -> None:
        """The exfiltration guard: models cannot embed arbitrary URLs into requests."""

        from unknown_agent.vision.images import load_image

        config = Settings().vision
        assert not config.allow_remote_urls_in_tools
        # user-initiated: fine
        assert load_image("https://example.com/x.png", config, project_root=tmp_path).kind == "url"
        # tool-initiated: refused
        with pytest.raises(ToolError, match="not allowed from tools"):
            load_image("https://example.com/x.png", config, project_root=tmp_path, from_tool=True)
        # unless explicitly enabled
        open_config = config.model_copy(update={"allow_remote_urls_in_tools": True})
        assert load_image("https://example.com/x.png", open_config, project_root=tmp_path, from_tool=True).kind == "url"

    def test_oversized_data_url_is_refused(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image

        config = Settings().vision.model_copy(update={"max_image_bytes": 100_000})
        huge = "data:image/png;base64," + "A" * 200_000
        with pytest.raises(ToolError, match="limit"):
            load_image(huge, config, project_root=tmp_path)

    def test_artifact_copy_is_kept_when_configured(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image

        (tmp_path / "s.png").write_bytes(make_png())
        artifacts = tmp_path / "artifacts"
        att = load_image(str(tmp_path / "s.png"), Settings().vision, project_root=tmp_path, artifacts_dir=artifacts)
        assert att.artifact and Path(att.artifact).is_file()


# --------------------------------------------------------------------------- #
# vision service routing
# --------------------------------------------------------------------------- #
class FakeProvider:
    """Records the request it receives and answers with a canned response."""

    def __init__(self, model: str = "fake-vision") -> None:
        self.model = model
        self.requests: list[Any] = []
        self.closed = False

    async def complete(self, request: Any) -> Any:
        self.requests.append(request)

        class _Response:
            content = "I can see it."
            error = None
            usage = None

        return _Response()

    async def aclose(self) -> None:
        self.closed = True


def _vision_settings(active_model: str, *, fallback_model: str = "", fallback_vision: bool = True) -> Settings:
    settings = Settings()
    settings.providers["default"] = ProviderConfig(model=active_model)
    if fallback_model:
        settings.providers["eyes"] = ProviderConfig(model=fallback_model, supports_vision=fallback_vision)
        settings.vision.fallback_profile = "eyes"
    return settings


class TestVisionService:
    def test_active_vision_model_routes_to_itself(self) -> None:
        from unknown_agent.vision.service import VisionService

        service = VisionService(_vision_settings("gpt-4o"))
        assert service.route() == ("default", False)

    def test_blind_active_routes_to_fallback(self) -> None:
        from unknown_agent.vision.service import VisionService

        service = VisionService(_vision_settings("text-only-9000", fallback_model="gpt-4o"))
        assert service.route() == ("eyes", True)

    def test_no_fallback_raises_with_actionable_message(self) -> None:
        from unknown_agent.vision.service import VisionService, VisionUnavailable

        service = VisionService(_vision_settings("text-only-9000"))
        with pytest.raises(VisionUnavailable, match="fallback_profile"):
            service.route()

    def test_blind_fallback_is_rejected_not_used(self) -> None:
        from unknown_agent.vision.service import VisionService, VisionUnavailable

        settings = _vision_settings("text-only-9000", fallback_model="also-blind", fallback_vision=False)
        with pytest.raises(VisionUnavailable, match="is not either"):
            VisionService(settings).route()

    def test_analyze_attaches_images_and_uses_active_provider(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image
        from unknown_agent.vision.service import VisionService

        settings = _vision_settings("gpt-4o")
        provider = FakeProvider()
        service = VisionService(settings, active_provider=provider)
        (tmp_path / "x.png").write_bytes(make_png())
        att = load_image(str(tmp_path / "x.png"), settings.vision, project_root=tmp_path)

        answer = asyncio.run(service.analyze([att], "what is it?"))
        assert answer.text == "I can see it."
        assert not answer.routed
        request = provider.requests[0]
        user = [m for m in request.messages if m.role == "user"][0]
        assert user.images and user.images[0].startswith("data:image/png;base64,")

    def test_analyze_enforces_max_images(self, tmp_path: Path) -> None:
        from unknown_agent.vision.images import load_image
        from unknown_agent.vision.service import VisionService, VisionUnavailable

        settings = _vision_settings("gpt-4o")
        settings.vision.max_images_per_request = 1
        (tmp_path / "x.png").write_bytes(make_png())
        att = load_image(str(tmp_path / "x.png"), settings.vision, project_root=tmp_path)
        service = VisionService(settings, active_provider=FakeProvider())
        with pytest.raises(VisionUnavailable, match="max_images_per_request"):
            asyncio.run(service.analyze([att, att], "?"))

    def test_disabled_vision_refuses(self) -> None:
        from unknown_agent.vision.service import VisionService, VisionUnavailable

        settings = _vision_settings("gpt-4o")
        settings.vision.enabled = False
        with pytest.raises(VisionUnavailable, match="disabled"):
            asyncio.run(VisionService(settings, active_provider=FakeProvider()).analyze([object()], "?"))  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# provider encoding dialects
# --------------------------------------------------------------------------- #
class TestProviderEncoding:
    def _request(self) -> Any:
        from unknown_agent.core.models import Message
        from unknown_agent.providers.base import ModelRequest

        message = Message.user("look at this")
        message.images = ["data:image/png;base64,QUJD", "https://example.com/y.jpg"]
        return ModelRequest(messages=[Message.system("sys"), message])

    def test_openai_dialect_uses_image_url_parts(self) -> None:
        from unknown_agent.providers.openai_compatible import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider(model="gpt-4o", api_key="x")
        payload = provider._payload(self._request(), stream=False)
        user = [m for m in payload["messages"] if m["role"] == "user"][0]
        kinds = [part["type"] for part in user["content"]]
        assert kinds == ["text", "image_url", "image_url"]
        assert user["content"][1]["image_url"]["url"].startswith("data:image/png")

    def test_anthropic_dialect_uses_source_blocks(self) -> None:
        from unknown_agent.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(model="claude-sonnet-4", api_key="x")
        payload = provider._payload(self._request(), stream=False)
        user = [m for m in payload["messages"] if m["role"] == "user"][0]
        kinds = [part["type"] for part in user["content"]]
        assert kinds == ["text", "image", "image"]
        assert user["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
        assert user["content"][2]["source"]["type"] == "url"

    def test_ollama_dialect_uses_raw_base64_array(self) -> None:
        from unknown_agent.providers.ollama import OllamaProvider

        provider = OllamaProvider(model="llava")
        payload = provider._payload(self._request(), stream=False)
        user = [m for m in payload["messages"] if m["role"] == "user"][0]
        assert user["images"] == ["QUJD"]  # data URLs stripped; remote URLs skipped


# --------------------------------------------------------------------------- #
# computer use
# --------------------------------------------------------------------------- #
class ScriptedDriver:
    """A driver that succeeds every action and reports a fixed host."""

    name = "scripted"

    def __init__(self, host: str = "localhost") -> None:
        self.host = host
        self.performed: list[Any] = []

    async def observe(self, *, screenshot_dir: Path, tag: str) -> Any:
        from unknown_agent.computer_use.actions import Observation

        return Observation(url=f"http://{self.host}/", title="page")

    async def perform(self, action: Any) -> None:
        self.performed.append(action)

    async def close(self) -> None:
        pass

    def current_host(self) -> str:
        return self.host


def _cu_session(tmp_path: Path, **overrides: Any):
    from unknown_agent.computer_use.session import ComputerUseSession

    config = Settings().computer_use.model_copy(update={"enabled": True, "confirm_actions": "never", **overrides})
    driver = ScriptedDriver()
    return ComputerUseSession(config, driver, journal_dir=tmp_path / "journal", artifacts_dir=tmp_path / "shots"), driver


class TestComputerUse:
    def test_disabled_config_refuses_everything(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.session import ComputerUseError, ComputerUseSession

        session = ComputerUseSession(
            Settings().computer_use, ScriptedDriver(), journal_dir=tmp_path, artifacts_dir=tmp_path
        )
        with pytest.raises(ComputerUseError, match="disabled"):
            asyncio.run(session.observe())

    def test_vocabulary_is_closed(self) -> None:
        from unknown_agent.computer_use.actions import parse_action

        with pytest.raises(ValueError, match="unknown action kind"):
            parse_action({"kind": "format_disk"})
        with pytest.raises(ValueError, match="unknown action fields"):
            parse_action({"kind": "click", "x": 1, "y": 1, "shell": "rm -rf"})
        with pytest.raises(ValueError, match="needs x and y"):
            parse_action({"kind": "click"})
        with pytest.raises(ValueError, match="needs a URL"):
            parse_action({"kind": "navigate"})

    def test_action_budget_is_enforced(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.actions import parse_action

        session, driver = _cu_session(tmp_path, max_actions_per_task=2)
        action = parse_action({"kind": "key", "text": "Enter"})
        assert asyncio.run(session.perform(action)).ok
        assert asyncio.run(session.perform(action)).ok
        third = asyncio.run(session.perform(action))
        assert third.denied and "budget" in third.error
        assert len(driver.performed) == 2  # the denied action never reached the driver

    def test_host_allowlist_blocks_foreign_hosts(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.actions import parse_action

        session, driver = _cu_session(tmp_path, allowed_hosts=["allowed.test"])
        result = asyncio.run(session.perform(parse_action({"kind": "click", "x": 1, "y": 1})))
        assert result.denied and "allowed_hosts" in result.error
        nav = asyncio.run(session.perform(parse_action({"kind": "navigate", "text": "https://evil.test/"})))
        assert nav.denied
        assert driver.performed == []

    def test_denied_keys_never_reach_the_driver(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.actions import parse_action

        session, driver = _cu_session(tmp_path, denied_keys=["Meta"])
        result = asyncio.run(session.perform(parse_action({"kind": "key", "text": "Meta+Q"})))
        assert result.denied and "denied_keys" in result.error
        assert driver.performed == []

    def test_confirmation_fails_closed_without_a_prompter(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.actions import parse_action

        session, driver = _cu_session(tmp_path, confirm_actions="mutating")
        result = asyncio.run(session.perform(parse_action({"kind": "type", "text": "hello"})))
        assert result.denied and "did not approve" in result.error
        assert driver.performed == []

    def test_confirmation_approval_lets_the_action_through(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.actions import parse_action
        from unknown_agent.computer_use.session import ComputerUseSession

        class YesPrompter:
            def confirm(self, message: str, default: bool = False) -> bool:
                return True

        config = Settings().computer_use.model_copy(update={"enabled": True, "confirm_actions": "mutating"})
        driver = ScriptedDriver()
        session = ComputerUseSession(
            config, driver, journal_dir=tmp_path / "j", artifacts_dir=tmp_path / "s", prompter=YesPrompter()
        )
        result = asyncio.run(session.perform(parse_action({"kind": "type", "text": "hello"})))
        assert result.ok and driver.performed

    def test_journal_records_every_event(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.actions import parse_action

        session, _ = _cu_session(tmp_path, allowed_hosts=["nowhere.test"])
        asyncio.run(session.perform(parse_action({"kind": "key", "text": "Enter"})))  # denied
        asyncio.run(session.close())
        journal_files = list((tmp_path / "journal").glob("*.jsonl"))
        assert journal_files
        events = [json.loads(line)["event"] for line in journal_files[0].read_text().splitlines()]
        assert "denied" in events and "closed" in events

    def test_read_only_actions_skip_confirmation(self, tmp_path: Path) -> None:
        from unknown_agent.computer_use.actions import parse_action

        session, driver = _cu_session(tmp_path, confirm_actions="mutating")
        result = asyncio.run(session.perform(parse_action({"kind": "screenshot"})))
        assert result.ok  # observation-only actions need no approval


# --------------------------------------------------------------------------- #
# tool registration gating
# --------------------------------------------------------------------------- #
class TestFactoryGating:
    def test_vision_tools_registered_when_enabled(self) -> None:
        from unknown_agent.tools.factory import build_default_registry

        registry = build_default_registry(Settings())
        names = {tool.name for tool in registry}
        assert {"analyze_image", "compare_images", "screenshot_analyze"} <= names

    def test_vision_tools_absent_when_disabled(self) -> None:
        from unknown_agent.tools.factory import build_default_registry

        settings = Settings()
        settings.vision.enabled = False
        names = {tool.name for tool in build_default_registry(settings)}
        assert "analyze_image" not in names

    def test_computer_use_tools_withheld_by_default(self) -> None:
        from unknown_agent.tools.factory import build_default_registry

        registry = build_default_registry(Settings())
        names = {tool.name for tool in registry}
        assert "computer_act" not in names  # disabled config withholds them

    def test_computer_use_tools_appear_when_enabled(self) -> None:
        from unknown_agent.tools.factory import build_default_registry

        settings = Settings()
        settings.computer_use.enabled = True
        names = {tool.name for tool in build_default_registry(settings)}
        assert {"computer_observe", "computer_act"} <= names

    def test_advanced_tools_are_registered(self) -> None:
        from unknown_agent.tools.factory import build_default_registry

        names = {tool.name for tool in build_default_registry(Settings())}
        assert {
            "architecture_map", "dependency_audit", "repo_health", "app_scaffold",
            "doc_query", "plan_review", "mcp_discover", "skill_distill", "ui_validate",
        } <= names
