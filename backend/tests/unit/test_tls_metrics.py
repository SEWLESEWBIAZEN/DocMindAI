"""TLS certificate expiry for /metrics (renewal failures must be visible before expiry)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from documind.ops.metrics import tls_certificate_state


def _cert(path: Path, *, issuer: str, days: int) -> datetime:
    key = ec.generate_private_key(ec.SECP256R1())
    end = datetime.now(UTC).replace(microsecond=0) + timedelta(days=days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "docs.example.com")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return end


def test_reports_expiry_and_whether_self_signed(tmp_path: Path) -> None:
    issued = tmp_path / "le.pem"
    end = _cert(issued, issuer="R11", days=60)
    assert tls_certificate_state(str(issued)) == (end.timestamp(), False)

    temporary = tmp_path / "self.pem"
    _cert(temporary, issuer="docs.example.com", days=2)
    assert tls_certificate_state(str(temporary))[1] is True  # type: ignore[index]


def test_unset_or_unreadable(tmp_path: Path) -> None:
    assert tls_certificate_state(None) is None
    assert tls_certificate_state(str(tmp_path / "missing.pem")) == (0.0, False)
    garbage = tmp_path / "bad.pem"
    garbage.write_text("not a certificate")
    assert tls_certificate_state(str(garbage)) == (0.0, False)
