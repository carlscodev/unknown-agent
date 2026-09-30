from __future__ import annotations

from pathlib import Path

import pytest

from unknown_agent.gateway import (
    AccessController,
    AccessDeniedError,
    ChannelAddress,
    ContentPart,
    Gateway,
    InboundEnvelope,
)


def test_pair_claim_authorize_revoke(tmp_path: Path) -> None:
    access = AccessController(tmp_path / "g.db")
    pair = access.issue("telegram", "bot", role="operator")
    assert len(pair.code) == 6 and pair.code.isdigit()
    identity = access.claim("telegram", "bot", "user-1", pair.code)
    assert access.authorize("telegram", "bot", "user-1").id == identity.id
    with pytest.raises(AccessDeniedError):
        access.claim("telegram", "bot", "user-2", pair.code)
    assert access.revoke(identity.id)
    with pytest.raises(AccessDeniedError):
        access.authorize("telegram", "bot", "user-1")


def test_roles_and_ttl_are_enforced(tmp_path: Path) -> None:
    access = AccessController(tmp_path / "g.db")
    viewer = access.issue("test", "bot", role="viewer")
    access.claim("test", "bot", "v", viewer.code)
    with pytest.raises(AccessDeniedError, match="permission"):
        access.authorize("test", "bot", "v", "agent.run")
    with pytest.raises(ValueError, match="300"):
        access.issue("test", "bot", ttl_seconds=301)


def test_bad_codes_are_bounded(tmp_path: Path) -> None:
    access = AccessController(tmp_path / "g.db")
    access.issue("test", "bot")
    for _ in range(5):
        with pytest.raises(AccessDeniedError):
            access.claim("test", "bot", "u", "000000")
    with pytest.raises(AccessDeniedError):
        access.claim("test", "bot", "u", "not-six")


def test_gateway_enforces_pairing_before_persistence(tmp_path: Path) -> None:
    access = AccessController(tmp_path / "g.db")
    gateway = Gateway(tmp_path / "g.db", access=access)
    envelope = InboundEnvelope(
        external_id="1",
        source=ChannelAddress(channel="test", account="bot", conversation="c"),
        sender_id="u",
        content=[ContentPart(kind="text", text="hi")],
    )
    with pytest.raises(AccessDeniedError):
        gateway.receive(envelope)
    pair = access.issue("test", "bot")
    access.claim("test", "bot", "u", pair.code)
    assert gateway.receive(envelope)
