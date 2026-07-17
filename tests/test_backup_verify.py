import tarfile

import pytest

from runtipi_companion.backup import BackupVerificationError, verify_archive
from runtipi_companion.retention import parse_backup_filename, select_prunable


def _make_archive(path, content=b"x" * 4096):
    payload = path.parent / "payload.txt"
    payload.write_bytes(content)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(payload, arcname="app/payload.txt")
    return path


def test_verify_archive_passes_on_good_archive(tmp_path):
    archive = _make_archive(tmp_path / "app-daily-2026-07-17.tar.gz")
    verify_archive(archive)  # must not raise


def test_verify_archive_fails_on_truncation(tmp_path):
    archive = _make_archive(tmp_path / "app-daily-2026-07-17.tar.gz")
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2])
    with pytest.raises(BackupVerificationError):
        verify_archive(archive)


def test_verify_archive_fails_on_corruption(tmp_path):
    archive = _make_archive(tmp_path / "app-daily-2026-07-17.tar.gz")
    data = bytearray(archive.read_bytes())
    # Flip bits in the middle of the compressed stream, keeping the gzip
    # header intact so the failure comes from decompression, not open().
    mid = len(data) // 2
    for i in range(mid, mid + 16):
        data[i] ^= 0xFF
    archive.write_bytes(bytes(data))
    with pytest.raises(BackupVerificationError):
        verify_archive(archive)


def test_verify_archive_fails_on_non_archive(tmp_path):
    bogus = tmp_path / "app-daily-2026-07-17.tar.gz"
    bogus.write_bytes(b"this is not a tarball")
    with pytest.raises(BackupVerificationError):
        verify_archive(bogus)


def test_pre_update_schedule_parses():
    parsed = parse_backup_filename("jellyfin-pre-update-2026-07-17.tar.gz")
    assert parsed == {
        "app": "jellyfin",
        "schedule": "pre-update",
        "date": "2026-07-17",
        "seq": None,
    }


def test_pre_update_retention_prunes_oldest():
    names = [
        "jellyfin-pre-update-2026-07-15.tar.gz",
        "jellyfin-pre-update-2026-07-16.tar.gz",
        "jellyfin-pre-update-2026-07-17.tar.gz",
        "jellyfin-daily-2026-07-17.tar.gz",  # different schedule, untouched
    ]
    prunable = select_prunable(names, "jellyfin", "pre-update", keep=2)
    assert prunable == ["jellyfin-pre-update-2026-07-15.tar.gz"]
