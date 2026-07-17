import tarfile

import pytest

from runtipi_companion.backup import BackupVerificationError, verify_archive
from runtipi_companion.backup.retention import parse_backup_filename, select_prunable


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


def test_latest_per_app_groups_and_reduces():
    from runtipi_companion.backup.restore import latest_per_app

    files = [
        "boxa/migrated/hello/hello-daily-2026-07-01.tar.gz",
        "boxa/migrated/hello/hello-daily-2026-07-02.tar.gz",
        "boxa/migrated/world/world-weekly-2026-07-01.tar.gz",
        "stray.txt",
    ]
    assert latest_per_app(files) == [
        ("migrated", "hello", "hello-daily-2026-07-02.tar.gz"),
        ("migrated", "world", "world-weekly-2026-07-01.tar.gz"),
    ]


def test_run_backup_continues_past_failing_app(tmp_path, monkeypatch):
    """One app failing to stop (e.g. runtipi-cli erroring) must not cancel
    the other apps' backups; the run still fails at the end."""
    import pytest as _pytest

    from runtipi_companion.backup import runner
    from runtipi_companion.backup.runner import BackupRunError, run_backup
    from runtipi_companion.config import CompanionConfig
    from runtipi_companion.system.shell import CommandError

    runtipi = tmp_path / "runtipi"
    for app in ("broken", "healthy"):
        (runtipi / "apps" / "migrated" / app).mkdir(parents=True)
        (runtipi / "app-data" / "migrated" / app).mkdir(parents=True)
        (runtipi / "app-data" / "migrated" / app / "data.txt").write_text("hi")

    class StubCLI:
        def __init__(self, *a, **k):
            self.cli_path = "/stub"

        def is_app_running(self, app_id, store):
            return True

        def app_stop(self, ref):
            if ref.startswith("broken"):
                raise CommandError(["runtipi-cli", "app", "stop", ref], 1, "rabbitmq exploded")

        def app_start(self, ref):
            pass

    monkeypatch.setattr(runner, "RuntipiCLI", StubCLI)

    cfg = CompanionConfig()
    cfg.runtipi.path = str(runtipi)
    cfg.backup.local_path = str(tmp_path / "backups")
    cfg.backup.sleep_duration = 0

    with _pytest.raises(BackupRunError, match="1 of 2.*broken:migrated"):
        run_backup(cfg, "daily", local_only=True)

    healthy = list((tmp_path / "backups" / "migrated" / "healthy").glob("*.tar.gz"))
    assert len(healthy) == 1, "healthy app should still have been backed up"
    broken = list((tmp_path / "backups" / "migrated" / "broken").glob("*.tar.gz"))
    assert broken == [], "broken app must not produce an archive after its stop failed"
