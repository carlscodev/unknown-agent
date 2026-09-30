from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from unknown_agent.gateway.devices import DeviceController, DeviceError


def enroll(controller: DeviceController) -> tuple[str, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    device_id = str(uuid.uuid4())
    capabilities = ["camera.capture", "notification.send"]
    enrollment = controller.issue_enrollment()
    proof = controller.enrollment_proof(enrollment.token, device_id, public, capabilities)
    device = controller.enroll(
        enrollment.token,
        device_id=device_id,
        name="phone",
        public_key=public,
        capabilities=capabilities,
        signature=private.sign(proof).hex(),
    )
    assert device.state == "active"
    return device_id, private


def test_enrollment_requires_key_possession_and_is_one_time(tmp_path: Path) -> None:
    controller = DeviceController(tmp_path / "gateway.db")
    private = Ed25519PrivateKey.generate()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    device_id = str(uuid.uuid4())
    enrollment = controller.issue_enrollment()
    with pytest.raises(DeviceError, match="signature"):
        controller.enroll(
            enrollment.token,
            device_id=device_id,
            name="phone",
            public_key=public,
            capabilities=["camera.capture"],
            signature="00" * 64,
        )
    proof = controller.enrollment_proof(enrollment.token, device_id, public, ["camera.capture"])
    controller.enroll(
        enrollment.token,
        device_id=device_id,
        name="phone",
        public_key=public,
        capabilities=["camera.capture"],
        signature=private.sign(proof).hex(),
    )
    second_id = str(uuid.uuid4())
    second_proof = controller.enrollment_proof(
        enrollment.token, second_id, public, ["camera.capture"]
    )
    with pytest.raises(DeviceError, match="token"):
        controller.enroll(
            enrollment.token,
            device_id=second_id,
            name="other",
            public_key=public,
            capabilities=["camera.capture"],
            signature=private.sign(second_proof).hex(),
        )


def test_signed_requests_reject_replay_and_expiry(tmp_path: Path) -> None:
    controller = DeviceController(tmp_path / "gateway.db")
    device_id, private = enroll(controller)
    body = json.dumps({"cursor": "1"}).encode()
    timestamp = int(time.time())
    nonce = "0123456789abcdef"
    signature = private.sign(controller.request_proof(timestamp, nonce, body)).hex()
    verified = controller.verify_request(
        device_id,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
        signature=signature,
    )
    assert verified.last_seen_at is not None
    with pytest.raises(DeviceError, match="replayed"):
        controller.verify_request(
            device_id,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
            signature=signature,
        )
    with pytest.raises(DeviceError, match="replay window"):
        controller.verify_request(
            device_id,
            timestamp=1,
            nonce="fedcba9876543210",
            body=body,
            signature=signature,
        )


def test_command_leases_idempotency_completion_and_revocation(tmp_path: Path) -> None:
    controller = DeviceController(tmp_path / "gateway.db")
    device_id, _ = enroll(controller)
    command = controller.enqueue(
        device_id,
        "camera.capture",
        {"facing": "rear"},
        idempotency_key="capture-1",
    )
    duplicate = controller.enqueue(
        device_id,
        "camera.capture",
        {"facing": "rear"},
        idempotency_key="capture-1",
    )
    assert duplicate.id == command.id
    claimed = controller.claim(device_id)
    assert claimed and claimed.state == "running" and claimed.lease_token
    with pytest.raises(DeviceError, match="lease"):
        controller.complete(claimed.id, "wrong", result={"ok": True})
    done = controller.complete(claimed.id, claimed.lease_token, result={"media": "cas:abc"})
    assert done.state == "succeeded" and done.result == {"media": "cas:abc"}
    pending = controller.enqueue(device_id, "notification.send", {"text": "hello"})
    assert controller.revoke(device_id).state == "revoked"
    assert controller.get_command(pending.id).state == "cancelled"
    with pytest.raises(DeviceError, match="capability"):
        controller.enqueue(device_id, "location.read", {})


def test_device_bounds(tmp_path: Path) -> None:
    controller = DeviceController(tmp_path / "gateway.db")
    with pytest.raises(ValueError, match="300"):
        controller.issue_enrollment(ttl_seconds=301)
    device_id, _ = enroll(controller)
    with pytest.raises(ValueError, match="300"):
        controller.enqueue(device_id, "camera.capture", {}, timeout_seconds=301)
    with pytest.raises(DeviceError, match="1 MiB"):
        controller.enqueue(device_id, "camera.capture", {"value": "x" * 1_048_577})
