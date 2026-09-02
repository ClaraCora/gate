from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture(scope="session")
def pem_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gate test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


@pytest.fixture
def encoded_profile(pem_pair: tuple[str, str]) -> str:
    cert_pem, key_pem = pem_pair
    profile = f"""\
client
dev tun
proto udp
remote 128.211.249.131 1195
resolv-retry infinite
nobind
persist-key
persist-tun
cipher AES-128-CBC
data-ciphers AES-128-CBC
auth SHA1
verb 3
<ca>
{cert_pem}</ca>
<cert>
{cert_pem}</cert>
<key>
{key_pem}</key>
"""
    return base64.b64encode(profile.encode()).decode()
