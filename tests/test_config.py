import textwrap
from pathlib import Path

import pytest

from runtipi_companion.config import ConfigError, load_config


def write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def test_load_minimal_config(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
    """,
    )
    cfg = load_config(str(p))
    assert cfg.runtipi.path == "/opt/runtipi"
    assert cfg.backup_local_path == "/opt/runtipi/backups"
    assert cfg.backup.schedules["daily"].retention == 3
    assert cfg.security.tailscale_only.enabled is False
    assert cfg.security.tailscale_only.tailscale_ssh is True
    assert cfg.security.tailscale_only.tailscale_port_udp == 41641


def test_tailscale_only_config(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        security:
          tailscale_only:
            enabled: true
            tailscale_ssh: false
            tailscale_port_udp: 12345
    """,
    )
    cfg = load_config(str(p))
    assert cfg.security.tailscale_only.enabled is True
    assert cfg.security.tailscale_only.tailscale_ssh is False
    assert cfg.security.tailscale_only.tailscale_port_udp == 12345


def test_remote_requires_schedule(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: b2
              rclone_remote: "b2:bucket"
    """,
    )
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_remote_with_retention(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: b2
              rclone_remote: "b2:bucket"
              schedules:
                daily:
                  retention: 14
    """,
    )
    cfg = load_config(str(p))
    remote = cfg.backup.remote("b2")
    assert remote.retention_for("daily") == 14
    assert remote.retention_for("weekly") is None


def test_remote_control_connection_parsed(tmp_path):
    p = write_config(
        tmp_path,
        """
        version: 4
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: cloud
              rclone_remote: "encrypted:backups"
              api_url: "http://rclone:5533"
              api_username: companion
              api_password_env: RCLONE_API_PASSWORD
              schedules:
                daily: {retention: 14}
    """,
    )
    remote = load_config(str(p)).backup.remote("cloud")
    assert remote.api_url == "http://rclone:5533"
    assert remote.api_username == "companion"
    assert remote.api_password_env == "RCLONE_API_PASSWORD"


def test_remote_control_connection_requires_all_fields(tmp_path):
    p = write_config(
        tmp_path,
        """
        version: 4
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: cloud
              rclone_remote: "encrypted:backups"
              api_url: "http://rclone:5533"
              schedules:
                daily: {retention: 14}
    """,
    )
    with pytest.raises(ConfigError, match="must set api_url"):
        load_config(str(p))


def test_duplicate_remote_names_rejected(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: b2
              rclone_remote: "b2:bucket"
              schedules:
                daily: {retention: 1}
            - name: b2
              rclone_remote: "b2:other"
              schedules:
                daily: {retention: 1}
    """,
    )
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_relative_runtipi_path_rejected(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: relative/path
    """,
    )
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_unknown_schedule_name_rejected(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          schedules:
            biweekly:
              retention: 2
    """,
    )
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_backup_before_defaults_true(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
    """,
    )
    assert load_config(str(p)).updates.backup_before is True


def test_backup_before_can_be_disabled(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        updates:
          backup_before: false
    """,
    )
    assert load_config(str(p)).updates.backup_before is False


def test_host_label_defaults_to_hostname(tmp_path):
    import socket

    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
    """,
    )
    assert load_config(str(p)).host_label == socket.gethostname()


def test_host_label_override(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          host_label: nas-primary
    """,
    )
    assert load_config(str(p)).host_label == "nas-primary"


def test_rclone_remote_trailing_slash_stripped(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: proton
              rclone_remote: "proton:backups/runtipi-companion/"
              schedules:
                daily:
                  retention: 7
    """,
    )
    assert load_config(str(p)).backup.remote("proton").rclone_remote == "proton:backups/runtipi-companion"


def test_app_settings_parsed(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          app_settings:
            questdb:
              keep_running: true
              exclude_patterns:
                - "\\\\.log$"
              pre_backup_command: "snapshot prepare"
              post_backup_command: "snapshot release"
              restore_command: "snapshot complete"
    """,
    )
    settings = load_config(str(p)).backup.app_settings["questdb"]
    assert settings.keep_running is True
    assert settings.exclude_patterns == ["\\.log$"]
    assert settings.pre_backup_command == "snapshot prepare"
    assert settings.post_backup_command == "snapshot release"
    assert settings.restore_command == "snapshot complete"


def test_app_settings_default_empty(tmp_path):
    p = write_config(tmp_path, "runtipi:\n  path: /opt/runtipi\n")
    assert load_config(str(p)).backup.app_settings == {}


def test_app_settings_partial_override_leaves_rest_unset(tmp_path):
    p = write_config(
        tmp_path,
        """
        runtipi:
          path: /opt/runtipi
        backup:
          app_settings:
            questdb:
              keep_running: true
    """,
    )
    settings = load_config(str(p)).backup.app_settings["questdb"]
    assert settings.keep_running is True
    assert settings.exclude_patterns is None
    assert settings.pre_backup_command is None
    assert settings.post_backup_command is None
