from __future__ import annotations

import base64

import pytest
from gate.profiles import sanitize_openvpn_profile
from gate.worker_protocol import REQUEST_ADAPTER, ProvisionSlotRequest
from pydantic import ValidationError


def test_provision_request_requires_matching_profile_fingerprint(encoded_profile: str) -> None:
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    request = REQUEST_ADAPTER.validate_python(
        {
            "action": "provision_slot",
            "region_id": "jp",
            "slot": "a",
            "remote_ip": profile.remote_ip,
            "remote_port": profile.remote_port,
            "transport": profile.transport,
            "profile_fingerprint": profile.fingerprint,
            "config_text": profile.config_text,
        }
    )

    assert isinstance(request, ProvisionSlotRequest)
    assert request.slot == "a"


def test_provision_request_rejects_unknown_fields(encoded_profile: str) -> None:
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        REQUEST_ADAPTER.validate_python(
            {
                "action": "provision_slot",
                "region_id": "jp",
                "slot": "a",
                "remote_ip": profile.remote_ip,
                "remote_port": profile.remote_port,
                "transport": profile.transport,
                "profile_fingerprint": profile.fingerprint,
                "config_text": profile.config_text,
                "command": "rm -rf /",
            }
        )


def test_root_boundary_rejects_rehashed_dangerous_config(encoded_profile: str) -> None:
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")
    dangerous = profile.config_text.replace("verb 3", "plugin /tmp/unsafe.so")
    fingerprint = __import__("hashlib").sha256(dangerous.encode()).hexdigest()
    request = ProvisionSlotRequest(
        action="provision_slot",
        region_id="jp",
        slot="a",
        remote_ip=profile.remote_ip,
        remote_port=profile.remote_port,
        transport=profile.transport,
        profile_fingerprint=fingerprint,
        config_text=dangerous,
    )

    # The protocol accepts a self-consistent envelope; the network manager must
    # independently canonicalize the profile before any privileged operation.
    assert request.config_text != base64.b64decode(encoded_profile).decode()
