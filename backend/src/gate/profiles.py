from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import shlex
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from gate.domain import SanitizedProfile, Transport
from gate.errors import ProfileRejectedError

MAX_DECODED_BYTES = 256 * 1024
MAX_INLINE_BLOCK_BYTES = 96 * 1024
INLINE_TAG = re.compile(r"^<([a-z][a-z0-9-]*)>$")
CLOSING_TAG = re.compile(r"^</([a-z][a-z0-9-]*)>$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:+-]+$")

ALLOWED_BLOCKS = frozenset({"ca", "cert", "key", "tls-auth", "tls-crypt"})
IGNORED_CONTROLLED_DIRECTIVES = frozenset(
    {
        "auth-nocache",
        "client",
        "connect-retry",
        "connect-retry-max",
        "dev",
        "nobind",
        "ping",
        "ping-restart",
        "persist-key",
        "persist-tun",
        "pull-filter",
        "route-nopull",
        "resolv-retry",
        "verb",
    }
)
ALLOWED_CIPHERS = frozenset(
    {
        "AES-128-CBC",
        "AES-192-CBC",
        "AES-256-CBC",
        "AES-128-GCM",
        "AES-192-GCM",
        "AES-256-GCM",
        "CHACHA20-POLY1305",
    }
)
ALLOWED_AUTH = frozenset({"SHA1", "SHA256", "SHA384", "SHA512"})


@dataclass(slots=True)
class _ParsedProfile:
    remote_ip: str | None = None
    remote_port: int | None = None
    transport: Transport | None = None
    cipher: str | None = None
    data_ciphers: str | None = None
    auth: str | None = None
    remote_cert_tls: bool = False
    key_direction: str | None = None


def _reject(message: str) -> ProfileRejectedError:
    return ProfileRejectedError(message)


def _decode_profile(encoded: str) -> str:
    try:
        compact = "".join(encoded.split())
        decoded = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _reject("OpenVPN config is not valid Base64") from exc
    if not decoded or len(decoded) > MAX_DECODED_BYTES:
        raise _reject("OpenVPN config has an invalid decoded size")
    try:
        return decoded.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _reject("OpenVPN config must be UTF-8 text") from exc


def _parse_transport(value: str) -> Transport:
    normalized = value.lower()
    if normalized in {"udp", "udp4"}:
        return Transport.UDP
    if normalized in {"tcp", "tcp4", "tcp-client", "tcp4-client"}:
        return Transport.TCP
    raise _reject(f"unsupported OpenVPN transport: {value}")


def _validate_cipher(value: str) -> str:
    value = value.upper()
    if value not in ALLOWED_CIPHERS:
        raise _reject(f"unsupported OpenVPN cipher: {value}")
    return value


def _validate_data_ciphers(value: str) -> str:
    if not TOKEN.fullmatch(value):
        raise _reject("data-ciphers contains invalid characters")
    ciphers = value.upper().split(":")
    if not ciphers or any(cipher not in ALLOWED_CIPHERS for cipher in ciphers):
        raise _reject("data-ciphers contains an unsupported cipher")
    return ":".join(ciphers)


def _validate_pem_blocks(blocks: dict[str, str]) -> None:
    missing = {"ca", "cert", "key"} - blocks.keys()
    if missing:
        raise _reject(f"OpenVPN config is missing inline blocks: {', '.join(sorted(missing))}")
    try:
        ca_certificates = x509.load_pem_x509_certificates(blocks["ca"].encode("ascii"))
        client_certificates = x509.load_pem_x509_certificates(blocks["cert"].encode("ascii"))
        private_key = serialization.load_pem_private_key(
            blocks["key"].encode("ascii"), password=None
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _reject("OpenVPN config contains invalid PEM material") from exc
    if not ca_certificates or not client_certificates:
        raise _reject("OpenVPN config contains an empty certificate block")

    certificate_key = (
        client_certificates[0]
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    private_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if certificate_key != private_public_key:
        raise _reject("OpenVPN client certificate and private key do not match")

    for tag in ("tls-auth", "tls-crypt"):
        if tag in blocks and "BEGIN OpenVPN Static key V1" not in blocks[tag]:
            raise _reject(f"{tag} does not contain an OpenVPN static key")


def _parse_directive(tokens: list[str], profile: _ParsedProfile) -> None:
    directive = tokens[0].lower()
    arguments = tokens[1:]
    if directive in IGNORED_CONTROLLED_DIRECTIVES:
        if directive == "dev" and arguments != ["tun"]:
            raise _reject("only a tun device is allowed")
        return
    if directive == "proto" and len(arguments) == 1:
        profile.transport = _parse_transport(arguments[0])
        return
    if directive == "remote" and len(arguments) in {2, 3}:
        remote = ipaddress.ip_address(arguments[0])
        if remote.version != 4 or not remote.is_global:
            raise _reject("OpenVPN remote must be a global IPv4 address")
        port = int(arguments[1])
        if not 1 <= port <= 65535:
            raise _reject("OpenVPN remote port is outside 1..65535")
        if profile.remote_ip is not None:
            raise _reject("multiple OpenVPN remote directives are not allowed")
        profile.remote_ip = str(remote)
        profile.remote_port = port
        if len(arguments) == 3:
            remote_transport = _parse_transport(arguments[2])
            if profile.transport is not None and remote_transport != profile.transport:
                raise _reject("remote transport conflicts with proto")
            profile.transport = remote_transport
        return
    if directive == "cipher" and len(arguments) == 1:
        profile.cipher = _validate_cipher(arguments[0])
        return
    if directive == "data-ciphers-fallback" and len(arguments) == 1:
        profile.cipher = _validate_cipher(arguments[0])
        return
    if directive == "data-ciphers" and len(arguments) == 1:
        profile.data_ciphers = _validate_data_ciphers(arguments[0])
        return
    if directive == "auth" and len(arguments) == 1:
        value = arguments[0].upper()
        if value not in ALLOWED_AUTH:
            raise _reject(f"unsupported OpenVPN auth digest: {value}")
        profile.auth = value
        return
    if directive == "remote-cert-tls" and arguments == ["server"]:
        profile.remote_cert_tls = True
        return
    if directive == "key-direction" and arguments in (["0"], ["1"]):
        profile.key_direction = arguments[0]
        return
    raise _reject(f"OpenVPN directive is not allowed: {directive}")


def _parse_profile(text: str) -> tuple[_ParsedProfile, dict[str, str]]:
    profile = _ParsedProfile()
    blocks: dict[str, str] = {}
    active_tag: str | None = None
    active_lines: list[str] = []

    for line_number, raw_line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw_line.strip()
        if active_tag is not None:
            closing = CLOSING_TAG.fullmatch(line)
            if closing:
                if closing.group(1) != active_tag:
                    raise _reject(f"mismatched inline block at line {line_number}")
                block = "\n".join(active_lines).strip() + "\n"
                if len(block.encode("utf-8")) > MAX_INLINE_BLOCK_BYTES:
                    raise _reject(f"inline block {active_tag} is too large")
                blocks[active_tag] = block
                active_tag = None
                active_lines = []
                continue
            if INLINE_TAG.fullmatch(line) or CLOSING_TAG.fullmatch(line):
                raise _reject(f"nested inline block at line {line_number}")
            active_lines.append(raw_line)
            continue

        if not line or line.startswith(("#", ";")):
            continue
        opening = INLINE_TAG.fullmatch(line)
        if opening:
            tag = opening.group(1)
            if tag not in ALLOWED_BLOCKS:
                raise _reject(f"inline block is not allowed: {tag}")
            if tag in blocks:
                raise _reject(f"duplicate inline block: {tag}")
            active_tag = tag
            continue
        if CLOSING_TAG.fullmatch(line):
            raise _reject(f"unexpected closing inline block at line {line_number}")
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise _reject(f"invalid OpenVPN syntax at line {line_number}") from exc
        if tokens:
            try:
                _parse_directive(tokens, profile)
            except (ValueError, ipaddress.AddressValueError) as exc:
                if isinstance(exc, ProfileRejectedError):
                    raise
                raise _reject(f"invalid OpenVPN directive at line {line_number}") from exc

    if active_tag is not None:
        raise _reject(f"unterminated inline block: {active_tag}")
    return profile, blocks


def _render_profile(profile: _ParsedProfile, blocks: dict[str, str]) -> str:
    assert profile.remote_ip is not None
    assert profile.remote_port is not None
    assert profile.transport is not None

    protocol = "udp4" if profile.transport is Transport.UDP else "tcp4-client"
    lines = [
        "client",
        "dev tun",
        f"proto {protocol}",
        f"remote {profile.remote_ip} {profile.remote_port}",
        "nobind",
        "persist-key",
        "persist-tun",
        "resolv-retry 2",
        "connect-retry 2 5",
        "connect-retry-max 2",
        "route-nopull",
        'pull-filter ignore "dhcp-option"',
        "auth-nocache",
        "ping 10",
        "ping-restart 30",
        "verb 3",
    ]
    if profile.data_ciphers:
        lines.append(f"data-ciphers {profile.data_ciphers}")
    elif profile.cipher:
        lines.append(f"data-ciphers {profile.cipher}")
    if profile.cipher:
        lines.append(f"data-ciphers-fallback {profile.cipher}")
    if profile.auth:
        lines.append(f"auth {profile.auth}")
    if profile.remote_cert_tls:
        lines.append("remote-cert-tls server")
    if profile.key_direction:
        lines.append(f"key-direction {profile.key_direction}")

    for tag in ("ca", "cert", "key", "tls-auth", "tls-crypt"):
        if tag in blocks:
            lines.extend((f"<{tag}>", blocks[tag].rstrip("\n"), f"</{tag}>"))
    return "\n".join(lines) + "\n"


def sanitize_openvpn_profile(encoded: str, *, expected_ip: str) -> SanitizedProfile:
    """Rebuild an untrusted VPN Gate profile from a narrow allowlist."""

    expected = ipaddress.ip_address(expected_ip)
    if expected.version != 4 or not expected.is_global:
        raise _reject("expected node IP must be a global IPv4 address")

    profile, blocks = _parse_profile(_decode_profile(encoded))
    if profile.remote_ip is None or profile.remote_port is None:
        raise _reject("OpenVPN config is missing a remote endpoint")
    if profile.transport is None:
        raise _reject("OpenVPN config is missing a transport")
    if profile.remote_ip != str(expected):
        raise _reject("OpenVPN remote does not match the VPN Gate node IP")
    _validate_pem_blocks(blocks)
    rendered = _render_profile(profile, blocks)
    fingerprint = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return SanitizedProfile(
        remote_ip=profile.remote_ip,
        remote_port=profile.remote_port,
        transport=profile.transport,
        config_text=rendered,
        fingerprint=fingerprint,
    )


def validate_sanitized_profile(
    config_text: str,
    *,
    expected_ip: str,
    expected_port: int,
    expected_transport: Transport,
    expected_fingerprint: str,
) -> SanitizedProfile:
    """Independently validate the controller's rendered profile at the root boundary."""

    encoded = base64.b64encode(config_text.encode("utf-8")).decode("ascii")
    profile = sanitize_openvpn_profile(encoded, expected_ip=expected_ip)
    if profile.config_text != config_text:
        raise _reject("rendered OpenVPN profile is not in canonical form")
    if profile.remote_port != expected_port or profile.transport is not expected_transport:
        raise _reject("rendered OpenVPN endpoint does not match the worker request")
    if profile.fingerprint != expected_fingerprint:
        raise _reject("rendered OpenVPN profile fingerprint does not match")
    return profile
