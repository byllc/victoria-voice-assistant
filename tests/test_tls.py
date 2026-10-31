from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from victoria_voice_assistant.cli import main
from victoria_voice_assistant.web.tls import TlsError, find_tls_files, validate


def _cert(directory: Path, name: str, days: int = 400) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", str(days),
            "-subj", f"/CN={name}", "-addext", f"subjectAltName=DNS:{name}",
            "-keyout", str(directory / "key.pem"), "-out", str(directory / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def no_tls_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASSISTANT_TLS_CERT", raising=False)
    monkeypatch.delenv("ASSISTANT_TLS_KEY", raising=False)


def test_no_files_means_plain_http(tmp_path: Path) -> None:
    assert find_tls_files(tmp_path) is None


def test_cert_in_config_tls_is_found_and_described(tmp_path: Path) -> None:
    _cert(tmp_path / "tls", "victoria.test")
    files = find_tls_files(tmp_path)
    assert files is not None and files.cert == tmp_path / "tls" / "cert.pem"
    info = validate(files)
    assert info.names == ["DNS:victoria.test"]
    assert info.days_left() is not None and info.days_left() >= 398


def test_half_installed_or_mismatched_files_are_errors(tmp_path: Path) -> None:
    _cert(tmp_path / "tls", "a.test")
    _cert(tmp_path / "other", "b.test")
    (tmp_path / "tls" / "key.pem").unlink()
    with pytest.raises(TlsError, match="private key not found"):
        find_tls_files(tmp_path)
    (tmp_path / "other" / "key.pem").rename(tmp_path / "tls" / "key.pem")
    with pytest.raises(TlsError, match="don't load together"):
        validate(find_tls_files(tmp_path))  # type: ignore[arg-type]


def test_tls_status_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _cert(tmp_path / "tls", "victoria.test", days=10)
    assert main(["tls", "status", "--config-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "(matches)" in out and "DNS:victoria.test" in out and "renew soon" in out
