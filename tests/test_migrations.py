import textwrap

import pytest

from runtipi_companion.config import CONFIG_VERSION, ConfigError, load_config
from runtipi_companion.config.migrations import migrate, migrate_file


def test_migrate_v1_to_current():
    raw = {"runtipi": {"path": "/opt/runtipi"}}
    migrated, applied = migrate(raw)
    assert migrated["version"] == CONFIG_VERSION
    assert applied == ["v1 -> v2"]
    assert migrated["notify"]["urls"] == []
    assert migrated["updates"]["backup_before"] is True
    assert migrated["backup"]["host_label"] is None
    # input untouched (deepcopy)
    assert "version" not in raw


def test_migrate_preserves_existing_values():
    raw = {
        "version": 1,
        "notify": {"urls": ["ntfy://x/y"], "webhook_url": "https://h"},
        "updates": {"backup_before": False},
        "backup": {"host_label": "nas"},
    }
    migrated, _ = migrate(raw)
    assert migrated["notify"]["urls"] == ["ntfy://x/y"]
    assert migrated["notify"]["webhook_url"] == "https://h"
    assert migrated["updates"]["backup_before"] is False
    assert migrated["backup"]["host_label"] == "nas"


def test_migrate_current_version_is_noop():
    migrated, applied = migrate({"version": CONFIG_VERSION, "runtipi": {"path": "/opt/runtipi"}})
    assert applied == []
    assert migrated["version"] == CONFIG_VERSION


def test_migrate_rejects_future_version():
    with pytest.raises(ConfigError):
        migrate({"version": CONFIG_VERSION + 1})


def test_loader_rejects_future_version(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(f"version: {CONFIG_VERSION + 1}\nruntipi:\n  path: /opt/runtipi\n")
    with pytest.raises(ConfigError):
        load_config(str(p))


def test_loader_defaults_missing_version_to_1(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("runtipi:\n  path: /opt/runtipi\n")
    assert load_config(str(p)).version == 1


def test_migrate_file_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "config.yaml"
    original = "runtipi:\n  path: /opt/runtipi\n"
    p.write_text(original)
    assert migrate_file(p, dry_run=True) is False
    assert p.read_text() == original
    assert list(tmp_path.glob("*.bak-*")) == []


def test_migrate_file_apply_backs_up_and_validates(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        textwrap.dedent("""
        runtipi:
          path: /opt/runtipi
        backup:
          host_label: nas
        """)
    )
    assert migrate_file(p, dry_run=False) is True
    cfg = load_config(str(p))
    assert cfg.version == CONFIG_VERSION
    assert cfg.backup.host_label == "nas"
    backup = tmp_path / "config.yaml.bak-v1"
    assert backup.exists()
    assert "version" not in backup.read_text()


def test_migrate_file_already_current(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(f"version: {CONFIG_VERSION}\nruntipi:\n  path: /opt/runtipi\n")
    assert migrate_file(p, dry_run=True) is True
