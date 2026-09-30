from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from unknown_agent.extensions.hooks import HookBus
from unknown_agent.extensions.loader import PluginManager
from unknown_agent.extensions.manifest import (
    CapabilityDeclaration,
    CapabilityManifest,
    TrustStore,
    build_manifest,
    default_manifest_path,
    generate_keypair,
    sign_manifest,
    verify_manifest,
)
from unknown_agent.observability.events import Event
from unknown_agent.tools.registry import ToolRegistry


PLUGIN_TEMPLATE = '''
from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import Tool, ToolContext

class DemoTool(Tool):
    name = "demo_read"
    description = "Read a demo value."
    category = "custom"
    risk = RiskLevel.READ
    async def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult.success("demo")

class Plugin:
    name = "demo-plugin"
    version = "1.2.3"
    def register(self, context):
        context.add_tool(DemoTool())

PLUGIN = Plugin()
'''


def keypair(tmp_path: Path) -> tuple[Path, Path, str]:
    private = tmp_path / "publisher.key"
    public = tmp_path / "publisher.pub"
    key_id, _ = generate_keypair(private, public)
    return private, public, key_id


def signed_plugin(
    tmp_path: Path,
    *,
    code: str = PLUGIN_TEMPLATE,
    capabilities: list[CapabilityDeclaration] | None = None,
    permissions: list[str] | None = None,
    runtime_api: str = "1",
    trust: bool = True,
) -> tuple[Path, TrustStore, CapabilityManifest]:
    plugins = tmp_path / "plugins"
    plugins.mkdir(exist_ok=True)
    source = plugins / "demo_plugin.py"
    source.write_text(code, encoding="utf-8")
    private, public, _key_id = keypair(tmp_path)
    store = TrustStore(tmp_path / "trusted_publishers.json")
    if trust:
        store.trust(public.read_text().strip(), publisher="Test Publisher")
    manifest = build_manifest(
        source,
        name="demo-plugin",
        version="1.2.3",
        entrypoint=source.name,
        publisher="Test Publisher",
        public_key=public.read_text().strip(),
        permissions=permissions or ["tools.register", "tools.read"],
        capabilities=capabilities
        or [CapabilityDeclaration(kind="tool", name="demo_read", risk="read")],
        runtime_api=runtime_api,
    )
    signed = sign_manifest(manifest, private)
    default_manifest_path(source).write_text(signed.to_json(), encoding="utf-8")
    return source, store, signed


def manager(
    source: Path,
    store: TrustStore,
    *,
    require_signed: bool = True,
    hook_bus: HookBus | None = None,
) -> tuple[PluginManager, ToolRegistry]:
    registry = ToolRegistry()
    item = PluginManager(
        allow_entry_points=False,
        user_dir=source.parent,
        require_signed=require_signed,
        trust_store_path=store.path,
    )
    item.load(registry=registry, settings=object(), hook_bus=hook_bus)
    return item, registry


def test_trusted_signed_plugin_loads_and_registers_declared_tool(tmp_path: Path) -> None:
    source, store, signed = signed_plugin(tmp_path)
    loaded, registry = manager(source, store)
    assert registry.has("demo_read")
    assert len(loaded.loaded) == 1
    record = loaded.loaded[0]
    assert record.manifest_verified is True
    assert record.publisher == "Test Publisher"
    assert record.key_id == signed.key_id
    assert record.permissions == ["tools.read", "tools.register"]


def test_signed_plugin_registers_declared_durable_hook(tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    code = f'''
from pathlib import Path
from unknown_agent.extensions.hooks import HookSpec

async def on_event(context):
    Path({str(marker)!r}).write_text(context.event_type)

class Plugin:
    name = "demo-plugin"
    version = "1.2.3"
    def register(self, context):
        context.add_hook(HookSpec(name="demo.audit", event_patterns=["gate.*"]), on_event)
PLUGIN = Plugin()
'''
    source, store, _signed = signed_plugin(
        tmp_path,
        code=code,
        permissions=["hooks.register"],
        capabilities=[CapabilityDeclaration(kind="hook", name="demo.audit")],
    )
    hook_bus = HookBus(tmp_path / "hooks.db")
    loaded, registry = manager(source, store, hook_bus=hook_bus)
    assert not registry.names()
    assert loaded.loaded[0].hooks == ["demo.audit"]
    hook_bus.publish(Event(type="gate.finished"))
    import asyncio

    asyncio.run(hook_bus.drain())
    assert marker.read_text() == "gate.finished"
    asyncio.run(hook_bus.close())


def test_unsigned_plugin_is_rejected_before_import(tmp_path: Path) -> None:
    marker = tmp_path / "imported"
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    source = plugins / "danger.py"
    source.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    loaded, registry = manager(source, TrustStore(tmp_path / "trust.json"))
    assert not marker.exists()
    assert not registry.names()
    assert "signed capability manifest is required" in (loaded.failed[0].error or "")


def test_tampered_source_is_rejected_without_execution(tmp_path: Path) -> None:
    marker = tmp_path / "imported"
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n" + PLUGIN_TEMPLATE
    source, store, _signed = signed_plugin(tmp_path, code=code)
    source.write_text(source.read_text() + "\n# malicious change\n", encoding="utf-8")
    loaded, registry = manager(source, store)
    assert not marker.exists()
    assert not registry.names()
    assert "content hash" in (loaded.failed[0].error or "")


def test_untrusted_publisher_is_rejected(tmp_path: Path) -> None:
    source, store, _signed = signed_plugin(tmp_path, trust=False)
    loaded, registry = manager(source, store)
    assert not registry.names()
    assert "is not trusted" in (loaded.failed[0].error or "")


def test_signature_tampering_is_detected(tmp_path: Path) -> None:
    source, store, _signed = signed_plugin(tmp_path)
    path = default_manifest_path(source)
    payload = json.loads(path.read_text())
    payload["signature"] = "A" * 88
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify_manifest(path, source, store, expected_entrypoint=source.name)
    assert report.ok is False
    assert "signature verification failed" in " ".join(report.errors)


def test_incompatible_runtime_api_is_rejected(tmp_path: Path) -> None:
    source, store, _signed = signed_plugin(tmp_path, runtime_api=">=2,<3")
    loaded, _registry = manager(source, store)
    assert "incompatible" in (loaded.failed[0].error or "")


def test_undeclared_second_capability_rolls_back_first(tmp_path: Path) -> None:
    code = PLUGIN_TEMPLATE.replace(
        "class Plugin:",
        '''class HiddenTool(DemoTool):
    name = "hidden_write"
    risk = RiskLevel.WRITE

class Plugin:''',
    ).replace("context.add_tool(DemoTool())", "context.add_tool(DemoTool())\n        context.add_tool(HiddenTool())")
    source, store, _signed = signed_plugin(tmp_path, code=code)
    loaded, registry = manager(source, store)
    assert not registry.has("demo_read")
    assert not registry.has("hidden_write")
    assert "not declared" in (loaded.failed[0].error or "")


def test_declared_risk_must_match_runtime_tool(tmp_path: Path) -> None:
    source, store, _signed = signed_plugin(
        tmp_path,
        capabilities=[CapabilityDeclaration(kind="tool", name="demo_read", risk="write")],
        permissions=["tools.register", "tools.write"],
    )
    loaded, registry = manager(source, store)
    assert not registry.names()
    assert "declares risk" in (loaded.failed[0].error or "")


def test_missing_risk_permission_is_rejected(tmp_path: Path) -> None:
    source, store, _signed = signed_plugin(tmp_path, permissions=["tools.register"])
    loaded, registry = manager(source, store)
    assert not registry.names()
    assert "tools.read" in (loaded.failed[0].error or "")


def test_present_but_invalid_manifest_cannot_use_compatibility_mode(tmp_path: Path) -> None:
    source, store, _signed = signed_plugin(tmp_path)
    source.write_text(source.read_text() + "# changed", encoding="utf-8")
    loaded, registry = manager(source, store, require_signed=False)
    assert not registry.names()
    assert "content hash" in (loaded.failed[0].error or "")


def test_unsigned_compatibility_mode_is_explicit(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    source = plugins / "demo_plugin.py"
    source.write_text(PLUGIN_TEMPLATE, encoding="utf-8")
    loaded, registry = manager(source, TrustStore(tmp_path / "trust.json"), require_signed=False)
    assert registry.has("demo_read")
    assert loaded.loaded[0].manifest_verified is False


def test_trust_store_persists_restrictive_permissions_and_revocation(tmp_path: Path) -> None:
    _private, public, key_id = keypair(tmp_path)
    path = tmp_path / "trusted.json"
    store = TrustStore(path)
    entry = store.trust(public.read_text().strip(), publisher="Publisher")
    assert entry.key_id == key_id
    assert TrustStore(path).get(key_id) is not None
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert store.revoke(key_id) is True
    assert store.get(key_id) is None
    assert store.revoke(key_id) is False


def test_key_generation_refuses_overwrite(tmp_path: Path) -> None:
    private = tmp_path / "key"
    public = tmp_path / "key.pub"
    generate_keypair(private, public)
    with pytest.raises(FileExistsError, match="overwrite"):
        generate_keypair(private, public)
    assert os.stat(private).st_mode & 0o777 == 0o600


def test_malformed_manifest_is_contained(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    source = plugins / "demo.py"
    source.write_text(PLUGIN_TEMPLATE, encoding="utf-8")
    default_manifest_path(source).write_text("{not-json", encoding="utf-8")
    loaded, registry = manager(source, TrustStore(tmp_path / "trust.json"))
    assert not registry.names()
    assert "invalid manifest" in (loaded.failed[0].error or "")
