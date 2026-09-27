import http.client
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

import pytest
import yaml

from runtipi_companion import container


def test_managed_container_config_uses_rc_api(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setenv("RCLONE_REMOTE", "encrypted:server-backups")
    monkeypatch.setenv("RCLONE_API_URL", "http://host.example:5572")
    monkeypatch.setenv("RCLONE_API_USERNAME", "backup-agent")

    container.write_managed_config()

    raw = yaml.safe_load(config_path.read_text())
    remote = raw["backup"]["remotes"][0]
    assert remote["rclone_remote"] == "encrypted:server-backups"
    assert remote["api_url"] == "http://host.example:5572"
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


def test_dashboard_settings_render_managed_config_and_schedule(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setattr(container, "SETTINGS_PATH", tmp_path / "settings.json")
    settings = container._default_settings()
    settings.update(
        backup_hour=5,
        enabled_schedules=["daily"],
        rclone_remote="encrypted:custom-target",
        rclone_api_url="unix:///run/rclone/rc.sock",
    )
    settings["local_retention"]["daily"] = 11
    settings["remote_retention"]["daily"] = 22
    container._save_settings(settings)

    container.write_managed_config()

    raw = yaml.safe_load(config_path.read_text())
    assert raw["backup"]["remotes"][0]["rclone_remote"] == "encrypted:custom-target"
    assert raw["backup"]["remotes"][0]["api_url"] == "unix:///run/rclone/rc.sock"
    assert raw["backup"]["schedules"]["daily"]["retention"] == 11
    assert raw["backup"]["remotes"][0]["schedules"]["daily"]["retention"] == 22
    assert container._due_schedules(datetime(2026, 9, 22, 5, 0), {}) == ["daily"]
    assert container._due_schedules(datetime(2026, 9, 27, 5, 0), {}) == ["daily"]


def test_coordinator_rejects_invalid_or_concurrent_runs(tmp_path):
    coordinator = container.BackupCoordinator(tmp_path / "config.yaml")

    with pytest.raises(ValueError, match="Unknown schedule"):
        coordinator.start("hourly")

    coordinator.lock.acquire()
    try:
        assert coordinator.start("daily") is False
    finally:
        coordinator.lock.release()


def test_individual_backup_uses_exact_store_reference(tmp_path):
    runtipi_path = tmp_path / "runtipi"
    for store in ("custom", "migrated"):
        (runtipi_path / "apps" / store / "gitea").mkdir(parents=True)

    refs = container.discover_apps(str(runtipi_path), ["gitea:migrated"])

    assert [ref.ref for ref in refs] == ["gitea:migrated"]
    assert container.discover_apps(str(runtipi_path), []) == []


def test_all_app_run_omits_dashboard_exclusions(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    runtipi_path = tmp_path / "runtipi"
    for app in ("gitea", "paperless"):
        (runtipi_path / "apps" / "migrated" / app).mkdir(parents=True)
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setattr(container, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(container, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("RUNTIPI_PATH", str(runtipi_path))
    container.write_managed_config()
    settings = container._default_settings()
    settings["excluded_apps"] = ["gitea:migrated"]
    container._save_settings(settings)
    calls = []
    monkeypatch.setattr(container, "run_backup", lambda cfg, schedule, **kwargs: calls.append(kwargs) or [])
    monkeypatch.setattr(container, "notify", lambda *args, **kwargs: None)
    coordinator = container.BackupCoordinator(config_path)
    coordinator.lock.acquire()

    coordinator._run("daily")

    assert calls[0]["apps"] == ["paperless:migrated"]


def test_coordinator_persists_bounded_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(container, "STATE_PATH", tmp_path / "state.json")
    coordinator = container.BackupCoordinator(tmp_path / "config.yaml")
    outcome = {"status": "running"}

    for index in range(55):
        coordinator._update_progress(
            outcome,
            {"stage": "archiving", "app": f"app-{index}", "completed_apps": index, "total_apps": 55},
        )

    stored = container._load_state()["last_run"]
    assert stored["app"] == "app-54"
    assert stored["completed_apps"] == 54
    assert len(stored["events"]) == 50


def test_web_ui_relies_on_runtipi_access_control(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    runtipi_path = tmp_path / "runtipi"
    (runtipi_path / "apps" / "migrated" / "gitea").mkdir(parents=True)
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setattr(container, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setenv("RUNTIPI_PATH", str(runtipi_path))
    monkeypatch.setenv("BACKUP_LOCAL_PATH", str(tmp_path / "backups"))
    container.write_managed_config()
    archive = tmp_path / "backups" / "migrated" / "gitea" / "gitea-daily-2026-09-27.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"archive")

    coordinator = container.BackupCoordinator(config_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), container.build_handler(coordinator, "csrf-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base_url}/healthz") as response:
            assert response.read() == b"ok\n"

        with urllib.request.urlopen(f"{base_url}/") as response:
            page = response.read().decode()
        assert "Runtipi Companion" in page
        assert "Run daily" in page
        assert "encrypted:runtipi-backups" in page
        assert "Backup progress" in page
        assert "Diagnostics" in page
        assert "Installed apps" in page
        assert "gitea" in page
        assert 'value="gitea:migrated"' in page
        assert 'href="/backups"' in page
        assert 'href="/config"' in page

        with urllib.request.urlopen(f"{base_url}/backups") as response:
            explorer = response.read().decode()
        assert "Backup Explorer" in explorer
        assert "read-only" in explorer
        assert "gitea-daily-2026-09-27.tar.gz" in explorer
        assert "1 local archives" in explorer
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_dashboard_refreshes_and_escapes_live_progress(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setattr(container, "STATE_PATH", tmp_path / "state.json")
    container.write_managed_config()
    container._set_state_value(
        "last_run",
        {
            "status": "running",
            "completed_apps": 1,
            "total_apps": 4,
            "app": "unsafe<script>",
            "message": "Archiving unsafe<script>",
            "events": [{"at": "now", "message": "Started <script>"}],
        },
    )
    coordinator = container.BackupCoordinator(config_path)
    coordinator.active_schedule = "daily"

    page = container._dashboard(coordinator, "csrf-token").decode()

    assert '<meta http-equiv="refresh" content="3">' in page
    assert 'aria-valuenow="25"' in page
    assert "unsafe&lt;script&gt;" in page
    assert "Started &lt;script&gt;" in page
    assert "unsafe<script>" not in page


def test_backup_post_redirects_to_dashboard(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setattr(container, "STATE_PATH", tmp_path / "state.json")
    container.write_managed_config()
    coordinator = container.BackupCoordinator(config_path)
    starts = []
    monkeypatch.setattr(coordinator, "start", lambda schedule, app_ref=None: starts.append((schedule, app_ref)) or True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), container.build_handler(coordinator, "csrf-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request(
            "POST",
            "/backup",
            "csrf=csrf-token&schedule=daily&app=gitea%3Amigrated",
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/"
        assert starts == [("daily", "gitea:migrated")]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_config_page_saves_validated_settings(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    runtipi_path = tmp_path / "runtipi"
    (runtipi_path / "apps" / "migrated" / "gitea").mkdir(parents=True)
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setattr(container, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(container, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("RUNTIPI_PATH", str(runtipi_path))
    container.write_managed_config()
    coordinator = container.BackupCoordinator(config_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), container.build_handler(coordinator, "csrf-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base_url}/config") as response:
            page = response.read().decode()
        assert "Configuration" in page
        assert "Excluded apps" in page
        assert "Test rclone upload" in page
        assert "gitea:migrated" in page

        fields = {
            "csrf": "csrf-token",
            "backup_hour": "6",
            "rclone_remote": "encrypted:new-target/",
            "rclone_api_url": "http://192.0.2.10:5572/",
            "enabled_schedule": ["daily", "weekly"],
            "excluded_app": "gitea:migrated",
        }
        for schedule, local, remote in (
            ("daily", 5, 10),
            ("weekly", 4, 8),
            ("monthly", 3, 6),
            ("yearly", 2, 4),
        ):
            fields[f"local_{schedule}"] = str(local)
            fields[f"remote_{schedule}"] = str(remote)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/config",
            urllib.parse.urlencode(fields, doseq=True),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/config"
        connection.close()

        settings = container._load_settings()
        assert settings["backup_hour"] == 6
        assert settings["enabled_schedules"] == ["daily", "weekly"]
        assert settings["rclone_remote"] == "encrypted:new-target"
        assert settings["rclone_api_url"] == "http://192.0.2.10:5572"
        assert settings["excluded_apps"] == ["gitea:migrated"]
        assert settings["local_retention"]["monthly"] == 3
        assert settings["remote_retention"]["yearly"] == 4
        managed = yaml.safe_load(config_path.read_text())
        assert managed["backup"]["remotes"][0]["rclone_remote"] == "encrypted:new-target"
        assert managed["backup"]["remotes"][0]["api_url"] == "http://192.0.2.10:5572"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_rclone_upload_test_route_reports_success(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(container, "CONFIG_PATH", config_path)
    monkeypatch.setattr(container, "STATE_PATH", tmp_path / "state.json")
    container.write_managed_config()
    tested = []
    monkeypatch.setattr(
        container, "probe_remote_upload", lambda remote: tested.append(remote) or "encrypted:test/probe"
    )
    coordinator = container.BackupCoordinator(config_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), container.build_handler(coordinator, "csrf-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request(
            "POST",
            "/rclone-test",
            "csrf=csrf-token",
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/config"
        assert len(tested) == 1
        result = container._load_state()["rclone_test"]
        assert result["status"] == "success"
        assert "test file removed" in result["message"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()
