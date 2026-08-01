"""Integration of per-app backup settings (keep_running, pre_backup_command,
exclude_patterns, restore_command) into the backup runner and restore flow.
The settings-resolution logic itself (labels + config merge) is covered in
test_app_settings.py; these tests exercise how runner.py/restore.py USE the
resolved settings.
"""

import io
import tarfile

import pytest

from runtipi_companion.backup import app_settings as app_settings_mod
from runtipi_companion.backup import restore as restore_mod
from runtipi_companion.backup import runner
from runtipi_companion.config import CompanionConfig
from runtipi_companion.system.shell import CommandError


class StubCLI:
    def __init__(self, *a, **k):
        self.cli_path = "/stub"
        self.stop_calls = []
        self.start_calls = []
        self.running = {}

    def is_app_running(self, app_id, store):
        return self.running.get((store, app_id), True)

    def app_stop(self, ref):
        self.stop_calls.append(ref)
        app_id, store = ref.split(":")
        self.running[(store, app_id)] = False

    def app_start(self, ref):
        self.start_calls.append(ref)
        app_id, store = ref.split(":")
        self.running[(store, app_id)] = True


def _seed(tmp_path, app_id="questdb", store="migrated"):
    runtipi = tmp_path / "runtipi"
    (runtipi / "apps" / store / app_id).mkdir(parents=True)
    (runtipi / "app-data" / store / app_id).mkdir(parents=True)
    (runtipi / "app-data" / store / app_id / "data.db").write_text("data")
    (runtipi / "app-data" / store / app_id / "cache.log").write_text("noisy")
    return runtipi


def _cfg(tmp_path, runtipi_path):
    cfg = CompanionConfig()
    cfg.runtipi.path = str(runtipi_path)
    cfg.backup.local_path = str(tmp_path / "backups")
    cfg.backup.sleep_duration = 0
    return cfg


# ---- backup runner: keep_running / pre_backup_command ----


def test_keep_running_skips_stop_and_start(tmp_path, monkeypatch):
    runtipi = _seed(tmp_path)
    cfg = _cfg(tmp_path, runtipi)
    stub = StubCLI()
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(
        runner,
        "resolve_app_settings",
        lambda cfg, app_id, store: app_settings_mod.ResolvedAppSettings(keep_running=True),
    )

    created = runner.run_backup(cfg, "daily", local_only=True)

    assert len(created) == 1
    assert stub.stop_calls == [], "keep_running must skip the stop entirely"
    assert stub.start_calls == [], "nothing was stopped, so nothing to restart"


def test_pre_backup_command_runs_before_stop(tmp_path, monkeypatch):
    runtipi = _seed(tmp_path)
    cfg = _cfg(tmp_path, runtipi)
    stub = StubCLI()
    calls = []
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(
        runner,
        "resolve_app_settings",
        lambda cfg, app_id, store: app_settings_mod.ResolvedAppSettings(pre_backup_command="snapshot prepare"),
    )

    def fake_run_app_command(app_id, store, kind, command, *, container_running, dry_run):
        calls.append((kind, command, container_running))
        assert stub.stop_calls == [], "pre_backup_command must run BEFORE the app is stopped"

    monkeypatch.setattr(runner, "run_app_command", fake_run_app_command)

    created = runner.run_backup(cfg, "daily", local_only=True)

    assert len(created) == 1
    assert calls == [("pre_backup_command", "snapshot prepare", True)]
    assert stub.stop_calls == ["questdb:migrated"], "default keep_running=False still stops the app"
    assert stub.start_calls == ["questdb:migrated"]


def test_pre_backup_command_failure_isolates_the_app(tmp_path, monkeypatch):
    """A failing pre_backup_command must fail only that app's backup (per-app
    isolation, same as every other command failure) -- and since it runs
    before the stop, the app is never touched."""
    runtipi = _seed(tmp_path)
    (runtipi / "apps" / "migrated" / "healthy").mkdir(parents=True)
    (runtipi / "app-data" / "migrated" / "healthy").mkdir(parents=True)
    cfg = _cfg(tmp_path, runtipi)
    stub = StubCLI()
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)

    def fake_resolve(cfg, app_id, store):
        if app_id == "questdb":
            return app_settings_mod.ResolvedAppSettings(pre_backup_command="snapshot prepare")
        return app_settings_mod.ResolvedAppSettings()

    def fake_run_app_command(app_id, store, kind, command, *, container_running, dry_run):
        raise CommandError(["docker", "exec"], 1, "snapshot endpoint unreachable")

    monkeypatch.setattr(runner, "resolve_app_settings", fake_resolve)
    monkeypatch.setattr(runner, "run_app_command", fake_run_app_command)

    with pytest.raises(runner.BackupRunError, match="1 of 2.*questdb"):
        runner.run_backup(cfg, "daily", local_only=True)

    assert "questdb:migrated" not in stub.stop_calls, "questdb was never stopped -- the failing hook ran first"
    healthy_archives = list((tmp_path / "backups" / "migrated" / "healthy").glob("*.tar.gz"))
    assert len(healthy_archives) == 1, "the other app's backup must still succeed"


def test_exclude_patterns_drop_matching_members(tmp_path):
    runtipi = _seed(tmp_path)
    dest = tmp_path / "out.tar.gz"
    runner._archive_app(str(runtipi), "migrated", "questdb", dest, exclude_patterns=[r"\.log$"])

    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("data.db") for n in names)
    assert not any(n.endswith("cache.log") for n in names)


def test_no_exclude_patterns_keeps_everything(tmp_path):
    runtipi = _seed(tmp_path)
    dest = tmp_path / "out.tar.gz"
    runner._archive_app(str(runtipi), "migrated", "questdb", dest)

    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("cache.log") for n in names)


def test_post_backup_command_runs_after_successful_archive(tmp_path, monkeypatch):
    runtipi = _seed(tmp_path)
    cfg = _cfg(tmp_path, runtipi)
    stub = StubCLI()
    calls = []
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(
        runner,
        "resolve_app_settings",
        lambda cfg, app_id, store: app_settings_mod.ResolvedAppSettings(
            keep_running=True, post_backup_command="CHECKPOINT RELEASE"
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_app_command",
        lambda app_id, store, kind, command, *, container_running, dry_run: calls.append(
            (kind, command, container_running)
        ),
    )

    created = runner.run_backup(cfg, "daily", local_only=True)

    assert len(created) == 1
    assert calls == [("post_backup_command", "CHECKPOINT RELEASE", True)]


def test_post_backup_command_runs_even_when_verify_fails(tmp_path, monkeypatch):
    """QuestDB's docs require CHECKPOINT RELEASE regardless of whether the
    backup copy succeeded or failed -- post_backup_command must fire even
    when the archive step itself blows up."""
    runtipi = _seed(tmp_path)
    cfg = _cfg(tmp_path, runtipi)
    stub = StubCLI()
    calls = []
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(
        runner,
        "resolve_app_settings",
        lambda cfg, app_id, store: app_settings_mod.ResolvedAppSettings(
            keep_running=True, post_backup_command="CHECKPOINT RELEASE"
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_app_command",
        lambda app_id, store, kind, command, *, container_running, dry_run: calls.append(kind),
    )
    monkeypatch.setattr(
        runner, "verify_archive", lambda path: (_ for _ in ()).throw(runner.BackupVerificationError("corrupt"))
    )

    with pytest.raises(runner.BackupRunError):
        runner.run_backup(cfg, "daily", local_only=True)

    assert calls == ["post_backup_command"], "must still run despite the verify failure"


def test_post_backup_command_after_restart_when_app_was_stopped(tmp_path, monkeypatch):
    """With keep_running=False (default), the app gets stopped for the
    archive -- post_backup_command must run only after it's back up."""
    runtipi = _seed(tmp_path)
    cfg = _cfg(tmp_path, runtipi)
    stub = StubCLI()
    calls = []
    monkeypatch.setattr(runner, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(
        runner,
        "resolve_app_settings",
        lambda cfg, app_id, store: app_settings_mod.ResolvedAppSettings(post_backup_command="cleanup"),
    )

    def fake_run_app_command(app_id, store, kind, command, *, container_running, dry_run):
        calls.append((kind, container_running))
        assert stub.start_calls == ["questdb:migrated"], "restart must happen before post_backup_command"

    monkeypatch.setattr(runner, "run_app_command", fake_run_app_command)

    runner.run_backup(cfg, "daily", local_only=True)

    assert calls == [("post_backup_command", True)]


# ---- restore: restore_command ----


def _seed_archive(tmp_path, store, app_id, filename):
    backups = tmp_path / "backups" / store / app_id
    backups.mkdir(parents=True, exist_ok=True)
    archive = backups / filename
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="app-data/data.db")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    return archive


def test_restore_command_runs_after_restart(tmp_path, monkeypatch):
    runtipi = tmp_path / "runtipi"
    (runtipi / "apps" / "migrated" / "questdb").mkdir(parents=True)
    (runtipi / "app-data" / "migrated" / "questdb").mkdir(parents=True)
    _seed_archive(tmp_path, "migrated", "questdb", "questdb-daily-2026-08-02.tar.gz")

    stub = StubCLI()
    calls = []
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(
        restore_mod,
        "resolve_app_settings",
        lambda cfg, app_id, store: app_settings_mod.ResolvedAppSettings(restore_command="snapshot complete"),
    )
    monkeypatch.setattr(
        restore_mod,
        "run_app_command",
        lambda app_id, store, kind, command, *, container_running, dry_run: calls.append(
            (kind, command, container_running)
        ),
    )

    cfg = _cfg(tmp_path, runtipi)
    result = restore_mod.restore_backup(cfg, "migrated", "questdb", "questdb-daily-2026-08-02.tar.gz", assume_yes=True)

    assert result is True
    assert calls == [("restore_command", "snapshot complete", True)]
    assert stub.start_calls == ["questdb:migrated"], "restart from the normal restore flow happened first"


def test_restore_command_starts_app_if_it_was_never_running(tmp_path, monkeypatch):
    """If the app wasn't running before the restore at all, the normal
    restart-if-we-stopped-it path never fires -- restore_command must still
    get a running container to exec into."""
    runtipi = tmp_path / "runtipi"
    (runtipi / "apps" / "migrated" / "questdb").mkdir(parents=True)
    (runtipi / "app-data" / "migrated" / "questdb").mkdir(parents=True)
    _seed_archive(tmp_path, "migrated", "questdb", "questdb-daily-2026-08-02.tar.gz")

    stub = StubCLI()
    stub.running[("migrated", "questdb")] = False
    calls = []
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(
        restore_mod,
        "resolve_app_settings",
        lambda cfg, app_id, store: app_settings_mod.ResolvedAppSettings(restore_command="snapshot complete"),
    )
    monkeypatch.setattr(
        restore_mod,
        "run_app_command",
        lambda app_id, store, kind, command, *, container_running, dry_run: calls.append(container_running),
    )

    cfg = _cfg(tmp_path, runtipi)
    restore_mod.restore_backup(cfg, "migrated", "questdb", "questdb-daily-2026-08-02.tar.gz", assume_yes=True)

    assert stub.start_calls == ["questdb:migrated"], "must start the app before running restore_command"
    assert calls == [True]
