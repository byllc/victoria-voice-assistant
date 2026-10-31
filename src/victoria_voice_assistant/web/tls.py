"""HTTPS for the web interface from a certificate Bill supplies (see docs/https.md)."""

from __future__ import annotations

import logging
import os
import ssl
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"
EXPIRY_WARNING_DAYS = 30


@dataclass
class TlsFiles:
    cert: Path
    key: Path


@dataclass
class CertInfo:
    subject: str
    names: list[str]
    not_after: datetime | None

    def days_left(self, now: datetime | None = None) -> int | None:
        if self.not_after is None:
            return None
        return (self.not_after - (now or datetime.now(timezone.utc))).days


class TlsError(ValueError):
    pass


def tls_dir(config_dir: Path) -> Path:
    return config_dir / "tls"


def find_tls_files(config_dir: Path) -> TlsFiles | None:
    """The certificate and key to serve, or None for plain HTTP.

    ``ASSISTANT_TLS_CERT`` and ``ASSISTANT_TLS_KEY`` override the default
    ``<config>/tls/cert.pem`` and ``<config>/tls/key.pem``. Having only one of the
    two is an error, so a half-finished update never silently falls back to HTTP.
    """
    cert = Path(os.environ.get("ASSISTANT_TLS_CERT") or tls_dir(config_dir) / CERT_NAME).expanduser()
    key = Path(os.environ.get("ASSISTANT_TLS_KEY") or tls_dir(config_dir) / KEY_NAME).expanduser()
    if not cert.exists() and not key.exists():
        return None
    if not cert.is_file():
        raise TlsError(f"certificate not found: {cert} (the key {key} exists)")
    if not key.is_file():
        raise TlsError(f"private key not found: {key} (the certificate {cert} exists)")
    return TlsFiles(cert=cert, key=key)


def validate(files: TlsFiles) -> CertInfo:
    """Check the key matches the certificate (and the chain parses); return what the certificate says."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(files.cert, files.key)
    except ssl.SSLError as exc:
        raise TlsError(f"certificate and key don't load together: {exc.reason or exc}") from exc
    except OSError as exc:
        raise TlsError(f"cannot read certificate or key: {exc}") from exc
    return inspect_cert(files.cert)


def inspect_cert(cert: Path) -> CertInfo:
    """Subject, DNS/IP names, and expiry, read with the system `openssl`."""
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-enddate", "-ext", "subjectAltName"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not read certificate details", extra={"event": "tls_inspect_failed", "error": str(exc)})
        return CertInfo(subject="unknown", names=[], not_after=None)
    subject, names, not_after = "unknown", [], None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("subject="):
            subject = line.removeprefix("subject=").strip()
        elif line.startswith("notAfter="):
            try:
                not_after = datetime.strptime(line.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                not_after = None
        elif line.startswith(("DNS:", "IP Address:")):
            names.extend(part.strip().replace("IP Address:", "IP:") for part in line.split(","))
    return CertInfo(subject=subject, names=names, not_after=not_after)


def log_cert(files: TlsFiles, info: CertInfo) -> None:
    days = info.days_left()
    extra = {
        "event": "tls_enabled",
        "cert": str(files.cert),
        "names": info.names,
        "expires": info.not_after.isoformat() if info.not_after else None,
        "days_left": days,
    }
    if days is not None and days < 0:
        logger.error("TLS certificate has expired; browsers will refuse it", extra={**extra, "event": "tls_expired"})
    elif days is not None and days < EXPIRY_WARNING_DAYS:
        logger.warning("TLS certificate expires soon; see docs/https.md", extra={**extra, "event": "tls_expiring"})
    else:
        logger.info("Serving HTTPS", extra=extra)


def describe(files: TlsFiles | None, info: CertInfo | None) -> str:
    """Text for `assistant web tls`."""
    if files is None:
        return "No certificate installed: the web interface serves plain HTTP (see docs/https.md)."
    assert info is not None
    days = info.days_left()
    lines = [
        f"Certificate: {files.cert}",
        f"Key:         {files.key} (matches)",
        f"Subject:     {info.subject}",
        f"Names:       {', '.join(info.names) or 'none listed (browsers need a subjectAltName)'}",
        f"Expires:     {info.not_after:%Y-%m-%d} ({days} days)" if info.not_after else "Expires:     unknown",
    ]
    if days is not None and days < EXPIRY_WARNING_DAYS:
        lines.append("Warning: renew soon; see docs/https.md.")
    return "\n".join(lines)
