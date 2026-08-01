import io
import tarfile

import pytest

from runtipi_companion.backup import restore as restore_mod
from runtipi_companion.config import CompanionConfig
from runtipi_companion.system.shell import CommandError


def _seed_runtipi(tmp_path, apps):
    runtipi = tmp_path / "runtipi"
    for store, app_id in apps:
        (runtipi / "apps" / store / app_id).mkdir(parents=True)
        (runtipi / "app-data" / store / app_id).mkdir(parents=True)
    return runtipi


def _seed_archive(tmp_path, store, app_id, filename, content=b"hi"):
    backups = tmp_path / "backups" / store / app_id
    backups.mkdir(parents=True, exist_ok=True)
    archive = backups / filename
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="app-data/payload.txt")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return archive


class StubCLI:
    def __init__(self, *a, **k):
        self.cli_path = "/stub"
        self.stop_calls = []
        self.start_calls = []
        self.running = {}
        self.stop_error_apps = set()

    def is_app_running(self, app_id, store):
        return self.running.get((store, app_id), True)

    def app_stop(self, ref):
        self.stop_calls.append(ref)
        app_id, store = ref.split(":")
        if ref in self.stop_error_apps:
            raise CommandError(["runtipi-cli", "app", "stop", ref], 1, "rabbitmq exploded")
        self.running[(store, app_id)] = False

    def app_start(self, ref):
        self.start_calls.append(ref)
        app_id, store = ref.split(":")
        self.running[(store, app_id)] = True


def _cfg(tmp_path):
    cfg = CompanionConfig()
    cfg.runtipi.path = str(tmp_path / "runtipi")
    cfg.backup.local_path = str(tmp_path / "backups")
    cfg.backup.sleep_duration = 0
    return cfg


def test_restore_backup_returns_false_when_user_declines(tmp_path, monkeypatch):
    _seed_runtipi(tmp_path, [("migrated", "gitea")])
    _seed_archive(tmp_path, "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz")
    stub = StubCLI()
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(restore_mod, "confirm", lambda *a, **k: False)

    result = restore_mod.restore_backup(_cfg(tmp_path), "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz")
    assert result is False
    assert stub.stop_calls == []


def test_restore_backup_restarts_when_stop_command_lies(tmp_path, monkeypatch):
    """Same false-failure case as backup/runner.py: a noisy app_stop exit
    code must not prevent the restore or skip the restart. is_app_running
    says True before the stop attempt (so we do try to stop it) and False
    on every check after -- mirroring dockerd's real state despite
    runtipi-cli's misleading non-zero exit."""
    _seed_runtipi(tmp_path, [("migrated", "gitea")])
    _seed_archive(tmp_path, "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz")
    stub = StubCLI()

    def fake_stop(ref):
        stub.stop_calls.append(ref)
        raise CommandError(["runtipi-cli", "app", "stop", ref], 1, "rabbitmq exploded")

    stub.app_stop = fake_stop
    stub.is_app_running = lambda app_id, store: len(stub.stop_calls) == 0
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)

    result = restore_mod.restore_backup(
        _cfg(tmp_path), "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz", assume_yes=True
    )
    assert result is True
    assert stub.start_calls == ["gitea:migrated"], "must restart despite the noisy stop error"


def test_restore_backup_raises_when_stop_genuinely_fails(tmp_path, monkeypatch):
    _seed_runtipi(tmp_path, [("migrated", "gitea")])
    _seed_archive(tmp_path, "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz")
    stub = StubCLI()

    def fake_stop(ref):
        stub.stop_calls.append(ref)
        raise CommandError(["runtipi-cli", "app", "stop", ref], 1, "genuinely stuck")

    stub.app_stop = fake_stop
    stub.running[("migrated", "gitea")] = True  # still running after the failed stop
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)

    with pytest.raises(CommandError):
        restore_mod.restore_backup(
            _cfg(tmp_path), "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz", assume_yes=True
        )

    assert stub.start_calls == [], "nothing to restart -- the app was never actually stopped"


def test_restore_apps_batch_restores_each_from_its_latest(tmp_path, monkeypatch):
    _seed_runtipi(tmp_path, [("migrated", "gitea"), ("migrated", "grafana")])
    _seed_archive(tmp_path, "migrated", "gitea", "gitea-daily-2026-07-30.tar.gz", content=b"old")
    _seed_archive(tmp_path, "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz", content=b"new")
    _seed_archive(tmp_path, "migrated", "grafana", "grafana-weekly-2026-07-28.tar.gz")

    stub = StubCLI()
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)

    cfg = _cfg(tmp_path)
    result = restore_mod.restore_apps(cfg, ["gitea", "grafana"], assume_yes=True)

    assert result["restored"] == ["gitea", "grafana"]
    assert result["skipped"] == []
    assert result["failed"] == []
    # picked the NEWEST gitea archive (2026-08-01, content "new"), not the older one
    restored_payload = tmp_path / "runtipi" / "app-data" / "migrated" / "gitea" / "payload.txt"
    assert restored_payload.read_bytes() == b"new"
    assert stub.start_calls == ["gitea:migrated", "grafana:migrated"]


def test_restore_apps_raises_for_unknown_app_id(tmp_path):
    _seed_runtipi(tmp_path, [("migrated", "gitea")])
    _seed_archive(tmp_path, "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz")

    with pytest.raises(ValueError, match="nonexistent-app"):
        restore_mod.restore_apps(_cfg(tmp_path), ["gitea", "nonexistent-app"], assume_yes=True)


def test_restore_apps_isolates_one_apps_failure(tmp_path, monkeypatch):
    """One app's genuine stop failure must not cancel the rest of the
    batch; it's reported as failed while the others still restore."""
    _seed_runtipi(tmp_path, [("migrated", "broken"), ("migrated", "healthy")])
    _seed_archive(tmp_path, "migrated", "broken", "broken-daily-2026-08-01.tar.gz")
    _seed_archive(tmp_path, "migrated", "healthy", "healthy-daily-2026-08-01.tar.gz")

    stub = StubCLI()

    def fake_stop(ref):
        stub.stop_calls.append(ref)
        if ref.startswith("broken"):
            raise CommandError(["runtipi-cli", "app", "stop", ref], 1, "stuck")
        app_id, store = ref.split(":")
        stub.running[(store, app_id)] = False

    stub.app_stop = fake_stop
    stub.running[("migrated", "broken")] = True  # stays running -- genuine failure
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)

    with pytest.raises(restore_mod.RestoreRunError, match="1 of 2.*broken"):
        restore_mod.restore_apps(_cfg(tmp_path), ["broken", "healthy"], assume_yes=True)

    assert stub.start_calls == ["healthy:migrated"]


def test_restore_apps_skip_is_not_a_failure(tmp_path, monkeypatch):
    _seed_runtipi(tmp_path, [("migrated", "gitea")])
    _seed_archive(tmp_path, "migrated", "gitea", "gitea-daily-2026-08-01.tar.gz")
    stub = StubCLI()
    monkeypatch.setattr(restore_mod, "RuntipiCLI", lambda *a, **k: stub)
    monkeypatch.setattr(restore_mod, "confirm", lambda *a, **k: False)

    result = restore_mod.restore_apps(_cfg(tmp_path), ["gitea"], assume_yes=False)
    assert result == {"restored": [], "skipped": ["gitea"], "failed": []}
