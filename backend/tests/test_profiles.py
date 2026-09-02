from __future__ import annotations

import base64

import pytest
from gate.domain import Transport
from gate.errors import ProfileRejectedError
from gate.profiles import sanitize_openvpn_profile


def test_sanitizes_valid_profile(encoded_profile: str) -> None:
    profile = sanitize_openvpn_profile(encoded_profile, expected_ip="128.211.249.131")

    assert profile.remote_ip == "128.211.249.131"
    assert profile.remote_port == 1195
    assert profile.transport is Transport.UDP
    assert len(profile.fingerprint) == 64
    assert "route-nopull" in profile.config_text
    assert 'pull-filter ignore "dhcp-option"' in profile.config_text
    assert "resolv-retry infinite" not in profile.config_text
    assert "data-ciphers-fallback AES-128-CBC" in profile.config_text


def test_rejects_remote_ip_mismatch(encoded_profile: str) -> None:
    with pytest.raises(ProfileRejectedError, match="does not match"):
        sanitize_openvpn_profile(encoded_profile, expected_ip="219.100.37.24")


def test_rejects_executable_directive(encoded_profile: str) -> None:
    decoded = base64.b64decode(encoded_profile).decode()
    malicious = decoded.replace("verb 3", "plugin /tmp/untrusted.so")
    encoded = base64.b64encode(malicious.encode()).decode()

    with pytest.raises(ProfileRejectedError, match="not allowed: plugin"):
        sanitize_openvpn_profile(encoded, expected_ip="128.211.249.131")


def test_rejects_invalid_base64() -> None:
    with pytest.raises(ProfileRejectedError, match="Base64"):
        sanitize_openvpn_profile("not-base64!", expected_ip="128.211.249.131")


def test_rejects_mismatched_private_key(encoded_profile: str, pem_pair: tuple[str, str]) -> None:
    del pem_pair
    decoded = base64.b64decode(encoded_profile).decode()
    start = decoded.index("<key>") + len("<key>")
    end = decoded.index("</key>")
    tampered = decoded[:start] + "\ninvalid key\n" + decoded[end:]

    with pytest.raises(ProfileRejectedError, match="invalid PEM"):
        sanitize_openvpn_profile(
            base64.b64encode(tampered.encode()).decode(),
            expected_ip="128.211.249.131",
        )
