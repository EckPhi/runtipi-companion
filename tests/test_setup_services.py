import pytest

from runtipi_companion.config import CompanionConfig, RemoteConfig, ScheduleConfig
from runtipi_companion.setup import services
from runtipi_companion.setup.rclone import missing_remotes


def test_unit_files_are_bundled_in_the_package():
    service = services._unit_text(services.SERVICE_UNIT)
    assert "ExecStart=/usr/local/bin/runtipi-companion backup run --type %i --apply" in service
    for schedule in services.VALID_SCHEDULES:
        timer = services._unit_text(f"runtipi-companion-backup-{schedule}.timer")
        assert "OnCalendar=" in timer
        assert f"runtipi-companion-backup@{schedule}.service" in timer
        assert "Persistent=true" in timer


def test_install_services_rejects_unknown_schedule():
    with pytest.raises(ValueError):
        services.install_services(["biweekly"], dry_run=True)


def test_install_services_dry_run_runs_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "run", lambda *a, **k: calls.append(a))
    services.install_services(["daily", "weekly"], dry_run=True)
    assert calls == []


def _cfg_with_remotes(*rclone_remotes):
    cfg = CompanionConfig()
    cfg.backup.remotes = [
        RemoteConfig(name=f"r{i}", rclone_remote=target, schedules={"daily": ScheduleConfig()})
        for i, target in enumerate(rclone_remotes)
    ]
    return cfg


def test_missing_remotes_reports_unconfigured_names():
    cfg = _cfg_with_remotes("b2:bucket/path", "gdrive:backups")
    assert missing_remotes(cfg, configured=["gdrive"]) == ["b2"]
    assert missing_remotes(cfg, configured=["gdrive", "b2"]) == []


def test_missing_remotes_ignores_disabled():
    cfg = _cfg_with_remotes("b2:bucket/path")
    cfg.backup.remotes[0].enabled = False
    assert missing_remotes(cfg, configured=[]) == []
