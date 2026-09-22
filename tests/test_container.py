from datetime import datetime

import yaml

from runtipi_companion import container


def test_managed_container_config_uses_rc_api(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setenv("RCLONE_REMOTE", "encrypted:server-backups")
    monkeypatch.setenv("RCLONE_API_USERNAME", "backup-agent")

    container.write_managed_config()

    raw = yaml.safe_load(config_path.read_text())
    remote = raw["backup"]["remotes"][0]
    assert remote["rclone_remote"] == "encrypted:server-backups"
    assert remote["api_url"] == "http://rclone:5533"
    assert remote["api_username"] == "backup-agent"
    assert remote["api_password_env"] == "RCLONE_API_PASSWORD"
    assert raw["backup"]["app_settings"]["runtipi-companion"]["keep_running"] is True
    assert raw["backup"]["app_settings"]["rclone"]["keep_running"] is True


def test_due_schedules_are_calendar_based(monkeypatch):
    monkeypatch.setenv("BACKUP_HOUR", "3")
    new_year_sunday = datetime(2023, 1, 1, 3, 0)
    assert container._due_schedules(new_year_sunday, {}) == ["daily", "weekly", "monthly", "yearly"]


def test_due_schedule_runs_once_per_day(monkeypatch):
    monkeypatch.setenv("BACKUP_HOUR", "3")
    now = datetime(2026, 9, 22, 3, 30)
    assert container._due_schedules(now, {"daily": "2026-09-22"}) == []
