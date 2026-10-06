"""Create a private, per-machine loopback development TLS identity."""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import ssl
import uuid
from pathlib import Path


def ensure_identity(runtime: Path) -> tuple[Path, Path]:
    folder = Path(runtime)/'tls'
    folder.mkdir(parents=True, exist_ok=True)
    cert, key = folder/'localhost.pem', folder/'localhost.key'
    if cert.is_file() and key.is_file():
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
            return cert, key
        except ssl.SSLError:
            # Repair a mismatched/interrupted generated pair, never a game cert.
            pass
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FIFA19 Local Backend Preview')])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(private.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now-dt.timedelta(days=1)).not_valid_after(now+dt.timedelta(days=3650))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                                                              x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                   .sign(private, hashes.SHA256()))
    suffix = uuid.uuid4().hex
    temporary_key, temporary_cert = folder/f'{suffix}.key', folder/f'{suffix}.pem'
    try:
        temporary_key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                                        serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))
        temporary_cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        os.replace(temporary_key, key)
        os.replace(temporary_cert, cert)
    finally:
        temporary_key.unlink(missing_ok=True)
        temporary_cert.unlink(missing_ok=True)
    return cert, key
