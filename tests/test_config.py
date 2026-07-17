import textwrap
from pathlib import Path

import pytest

from runtipi_companion.config import ConfigError, load_config


def write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def test_load_minimal_config(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: /opt/runtipi
    """)
    cfg = load_config(str(p))
    assert cfg.runtipi.path == "/opt/runtipi"
    assert cfg.backup_local_path == "/opt/runtipi/backups"
    assert cfg.backup.schedules["daily"].retention == 3
    assert cfg.security.tailscale_only.enabled is False
    assert cfg.security.tailscale_only.tailscale_ssh is True
    assert cfg.security.tailscale_only.tailscale_port_udp == 41641


def test_tailscale_only_config(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: /opt/runtipi
        security:
          tailscale_only:
            enabled: true
            tailscale_ssh: false
            tailscale_port_udp: 12345
    """)
    cfg = load_config(str(p))
    assert cfg.security.tailscale_only.enabled is True
    assert cfg.security.tailscale_only.tailscale_ssh is False
    assert cfg.security.tailscale_only.tailscale_port_udp == 12345


def test_remote_requires_schedule(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: b2
              rclone_remote: "b2:bucket"
    """)
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_remote_with_retention(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: /opt/runtipi
        backup:
          remotes:
            - name: b2
              rclone_remote: "b2:bucket"
              schedules:
                daily:
                  retention: 14
    """)
    cfg = load_config(str(p))
    remote = cfg.backup.remote("b2")
    assert remote.retention_for("daily") == 14
    assert remote.retention_for("weekly") is None


def test_duplicate_remote_names_rejected(tmp_path):
    p = write_config(tmp_path, """
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
    """)
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_relative_runtipi_path_rejected(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: relative/path
    """)
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_unknown_schedule_name_rejected(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: /opt/runtipi
        backup:
          schedules:
            biweekly:
              retention: 2
    """)
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_backup_before_defaults_true(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: /opt/runtipi
    """)
    assert load_config(str(p)).updates.backup_before is True


def test_backup_before_can_be_disabled(tmp_path):
    p = write_config(tmp_path, """
        runtipi:
          path: /opt/runtipi
        updates:
          backup_before: false
    """)
    assert load_config(str(p)).updates.backup_before is False
