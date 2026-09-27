from pathlib import Path

import pytest

from runtipi_companion.backup.rclone import RcloneAPIError, RcloneRCClient, client_for_remote, probe_remote_upload
from runtipi_companion.config import RemoteConfig


def test_factory_selects_rc_client(monkeypatch):
    monkeypatch.setenv("RCLONE_SECRET", "secret")
    remote = RemoteConfig(
        name="cloud",
        rclone_remote="encrypted:backups",
        api_url="http://rclone:5533",
        api_username="companion",
        api_password_env="RCLONE_SECRET",
    )
    assert isinstance(client_for_remote(remote), RcloneRCClient)


def test_rc_client_requires_password_env(monkeypatch):
    monkeypatch.delenv("MISSING_RCLONE_SECRET", raising=False)
    client = RcloneRCClient("http://rclone:5533", "companion", "MISSING_RCLONE_SECRET")
    with pytest.raises(RcloneAPIError, match="MISSING_RCLONE_SECRET"):
        client.list_files("encrypted:backups")


def test_rc_list_files_uses_remote_and_path(monkeypatch):
    monkeypatch.setenv("RCLONE_SECRET", "secret")
    client = RcloneRCClient("http://rclone:5533", "companion", "RCLONE_SECRET")
    calls = []

    def request(endpoint, payload):
        calls.append((endpoint, payload))
        return {"list": [{"Path": "store/app/a.tar.gz", "IsDir": False}]}

    monkeypatch.setattr(client, "_request", request)
    assert client.list_files("encrypted:backups/host") == ["store/app/a.tar.gz"]
    assert calls == [
        (
            "operations/list",
            {"fs": "encrypted:", "remote": "backups/host", "opt": {"recurse": True, "filesOnly": True}},
        )
    ]


def test_rc_sync_filters_schedule_and_preserves_relative_path(tmp_path, monkeypatch):
    monkeypatch.setenv("RCLONE_SECRET", "secret")
    daily = tmp_path / "store" / "app" / "app-daily-2026-09-22.tar.gz"
    weekly = tmp_path / "store" / "app" / "app-weekly-2026-09-22.tar.gz"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(b"daily")
    weekly.write_bytes(b"weekly")
    client = RcloneRCClient("http://rclone:5533", "companion", "RCLONE_SECRET")
    uploaded = []
    monkeypatch.setattr(client, "_upload_file", lambda source, target: uploaded.append((Path(source), target)))

    client.sync_dir(tmp_path, "encrypted:backups/host", include="*-daily-*.tar.gz")

    assert uploaded == [(daily, "encrypted:backups/host/store/app/app-daily-2026-09-22.tar.gz")]


def test_remote_upload_uses_configured_target_and_cleans_up(monkeypatch):
    remote = RemoteConfig(name="cloud", rclone_remote="encrypted:backups")
    calls = []

    class StubClient:
        def sync_dir(self, source, target):
            files = list(Path(source).iterdir())
            assert len(files) == 1
            assert files[0].read_text() == "runtipi-companion rclone upload test\n"
            calls.append(("upload", target, files[0].name))

        def delete_file(self, target):
            calls.append(("delete", target))

    monkeypatch.setattr("runtipi_companion.backup.rclone.client_for_remote", lambda config: StubClient())

    target = probe_remote_upload(remote)

    assert target.startswith("encrypted:backups/.runtipi-companion-test-")
    assert calls == [("upload", "encrypted:backups", target.rsplit("/", 1)[1]), ("delete", target)]


def test_remote_upload_attempts_cleanup_after_upload_failure(monkeypatch):
    remote = RemoteConfig(name="cloud", rclone_remote="encrypted:backups")
    deleted = []

    class StubClient:
        def sync_dir(self, source, target):
            raise RcloneAPIError("broken pipe")

        def delete_file(self, target):
            deleted.append(target)

    monkeypatch.setattr("runtipi_companion.backup.rclone.client_for_remote", lambda config: StubClient())

    with pytest.raises(RcloneAPIError, match="broken pipe"):
        probe_remote_upload(remote)
    assert deleted[0].startswith("encrypted:backups/.runtipi-companion-test-")
