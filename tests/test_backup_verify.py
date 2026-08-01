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


def test_run_backup_restarts_app_when_stop_command_lies(tmp_path, monkeypatch):
    """Real-world case: runtipi-cli's 'app stop' can exit non-zero for a
    reason unrelated to the actual docker stop (seen live: a RabbitMQ
    event-publish failure on an otherwise-healthy stop). If the containers
    are actually down afterward, we must still archive AND restart the app
    -- not leave it stopped because we trusted a misleading exit code.
    """

    from runtipi_companion.backup import runner
    from runtipi_companion.backup.runner import run_backup
    from runtipi_companion.config import CompanionConfig
    from runtipi_companion.system.shell import CommandError

    runtipi = tmp_path / "runtipi"
    app_dir = runtipi / "apps" / "migrated" / "gitea"
    app_dir.mkdir(parents=True)
    (runtipi / "app-data" / "migrated" / "gitea").mkdir(parents=True)
    (runtipi / "app-data" / "migrated" / "gitea" / "data.txt").write_text("hi")

    class StubCLI:
        def __init__(self, *a, **k):
            self.cli_path = "/stub"
            self.stop_calls = 0
            self.start_calls = 0

        def is_app_running(self, app_id, store):
            # First check (before stop): running. Every check after the
            # failed stop call: actually stopped, mirroring dockerd's real
            # state despite runtipi-cli's misleading non-zero exit.
            return self.stop_calls == 0

        def app_stop(self, ref):
            self.stop_calls += 1
            raise CommandError(["runtipi-cli", "app", "stop", ref], 1, "rabbitmq exploded")

        def app_start(self, ref):
            self.start_calls += 1

    stub = StubCLI()
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)

    cfg = CompanionConfig()
    cfg.runtipi.path = str(runtipi)
    cfg.backup.local_path = str(tmp_path / "backups")
    cfg.backup.sleep_duration = 0

    created = run_backup(cfg, "daily", local_only=True)  # must NOT raise

    assert len(created) == 1, "archive should still be created despite the noisy exit code"
    assert stub.start_calls == 1, "the app must be restarted after a false-failure stop"


def test_run_backup_skips_archive_when_stop_genuinely_fails(tmp_path, monkeypatch):
    """Counterpart to the false-failure case: if the app is confirmed still
    running after 'app stop' errors, that's a real failure -- no archive,
    and nothing to restart (it was never actually stopped)."""
    import pytest as _pytest

    from runtipi_companion.backup import runner
    from runtipi_companion.backup.runner import BackupRunError, run_backup
    from runtipi_companion.config import CompanionConfig
    from runtipi_companion.system.shell import CommandError

    runtipi = tmp_path / "runtipi"
    (runtipi / "apps" / "migrated" / "gitea").mkdir(parents=True)
    (runtipi / "app-data" / "migrated" / "gitea").mkdir(parents=True)

    class StubCLI:
        def __init__(self, *a, **k):
            self.cli_path = "/stub"
            self.start_calls = 0

        def is_app_running(self, app_id, store):
            return True  # still running no matter what we try

        def app_stop(self, ref):
            raise CommandError(["runtipi-cli", "app", "stop", ref], 1, "genuinely stuck")

        def app_start(self, ref):
            self.start_calls += 1

    stub = StubCLI()
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)

    cfg = CompanionConfig()
    cfg.runtipi.path = str(runtipi)
    cfg.backup.local_path = str(tmp_path / "backups")
    cfg.backup.sleep_duration = 0

    with _pytest.raises(BackupRunError):
        run_backup(cfg, "daily", local_only=True)

    assert stub.start_calls == 0, "nothing to restart -- the app was never actually stopped"
    archives = list((tmp_path / "backups" / "migrated" / "gitea").glob("*.tar.gz"))
    assert archives == []


def test_run_backup_restarts_app_even_if_archiving_crashes(tmp_path, monkeypatch):
    """Any failure between a successful stop and the end of the app's
    backup (not just a stop-command failure) must still restart the app --
    previously an archiving error left the app stopped indefinitely too."""
    import pytest as _pytest

    from runtipi_companion.backup import runner
    from runtipi_companion.backup.runner import run_backup
    from runtipi_companion.config import CompanionConfig

    runtipi = tmp_path / "runtipi"
    (runtipi / "apps" / "migrated" / "gitea").mkdir(parents=True)
    (runtipi / "app-data" / "migrated" / "gitea").mkdir(parents=True)

    class StubCLI:
        def __init__(self, *a, **k):
            self.cli_path = "/stub"
            self.start_calls = 0

        def is_app_running(self, app_id, store):
            return True

        def app_stop(self, ref):
            pass  # succeeds cleanly

        def app_start(self, ref):
            self.start_calls += 1

    stub = StubCLI()
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(runner, "_archive_app", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    cfg = CompanionConfig()
    cfg.runtipi.path = str(runtipi)
    cfg.backup.local_path = str(tmp_path / "backups")
    cfg.backup.sleep_duration = 0

    with _pytest.raises(OSError):
        run_backup(cfg, "daily", local_only=True)

    assert stub.start_calls == 1, "app must be restarted even when archiving itself crashes"


def test_sync_only_copies_current_schedule(monkeypatch):
    """The remote sync must be filtered to the schedule being synced --
    other schedules' archives and local-only pre-update snapshots must not
    ride along (they'd never be pruned on that remote)."""
    from runtipi_companion.backup import rclone as rclone_mod

    calls = []
    monkeypatch.setattr(rclone_mod, "run", lambda cmd, **k: calls.append(cmd))
    rclone_mod.RcloneClient().sync_dir("/backups", "proton:bucket/host", include="*-daily-*.tar.gz")
    (cmd,) = calls
    i = cmd.index("--include")
    assert cmd[i + 1] == "*-daily-*.tar.gz"
