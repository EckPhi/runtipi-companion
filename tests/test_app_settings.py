from runtipi_companion.backup import app_settings
from runtipi_companion.config import CompanionConfig
from runtipi_companion.config.schema import AppBackupConfig


def test_labels_to_overrides_parses_known_keys():
    labels = {
        "runtipi-companion.backup.keep-running": "true",
        "runtipi-companion.backup.exclude": " \\.log$ , /tmp/ ",
        "runtipi-companion.backup.pre-command": "snapshot prepare",
        "runtipi-companion.backup.post-command": "snapshot release",
        "runtipi-companion.backup.restore-command": "snapshot complete",
        "some.other.label": "ignored",
    }
    overrides = app_settings.labels_to_overrides(labels)
    assert overrides.keep_running is True
    assert overrides.exclude_patterns == ["\\.log$", "/tmp/"]
    assert overrides.pre_backup_command == "snapshot prepare"
    assert overrides.post_backup_command == "snapshot release"
    assert overrides.restore_command == "snapshot complete"


def test_labels_to_overrides_empty_when_no_labels():
    overrides = app_settings.labels_to_overrides({})
    assert overrides == AppBackupConfig()


def test_labels_to_overrides_keep_running_false():
    overrides = app_settings.labels_to_overrides({"runtipi-companion.backup.keep-running": "false"})
    assert overrides.keep_running is False


def test_merge_overrides_config_wins_per_field():
    base = AppBackupConfig(
        keep_running=True,
        exclude_patterns=["from-label"],
        pre_backup_command="label-cmd",
        post_backup_command="label-post",
    )
    override = AppBackupConfig(keep_running=False, restore_command="config-restore")
    merged = app_settings.merge_overrides(base, override)
    assert merged.keep_running is False  # override wins (explicitly set)
    assert merged.exclude_patterns == ["from-label"]  # override didn't set it -> base kept
    assert merged.pre_backup_command == "label-cmd"  # override didn't set it -> base kept
    assert merged.post_backup_command == "label-post"  # override didn't set it -> base kept
    assert merged.restore_command == "config-restore"  # only override set it


def test_resolve_app_settings_defaults_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(app_settings, "container_id", lambda app_id, store: None)
    monkeypatch.setattr(app_settings, "read_labels", lambda cid: {})
    cfg = CompanionConfig()
    resolved = app_settings.resolve_app_settings(cfg, "jellyfin", "migrated")
    assert resolved.keep_running is False
    assert resolved.exclude_patterns == []
    assert resolved.pre_backup_command is None
    assert resolved.restore_command is None


def test_resolve_app_settings_uses_labels_when_no_config_override(monkeypatch):
    monkeypatch.setattr(app_settings, "container_id", lambda app_id, store: "cid123")
    monkeypatch.setattr(
        app_settings,
        "read_labels",
        lambda cid: {"runtipi-companion.backup.keep-running": "true"} if cid == "cid123" else {},
    )
    cfg = CompanionConfig()
    resolved = app_settings.resolve_app_settings(cfg, "questdb", "migrated")
    assert resolved.keep_running is True


def test_resolve_app_settings_config_file_overrides_label(monkeypatch):
    monkeypatch.setattr(app_settings, "container_id", lambda app_id, store: "cid123")
    monkeypatch.setattr(app_settings, "read_labels", lambda cid: {"runtipi-companion.backup.keep-running": "true"})
    cfg = CompanionConfig()
    cfg.backup.app_settings["questdb"] = AppBackupConfig(keep_running=False)
    resolved = app_settings.resolve_app_settings(cfg, "questdb", "migrated")
    assert resolved.keep_running is False


def test_resolve_app_settings_full_questdb_use_case(monkeypatch):
    """Real QuestDB OSS checkpoint procedure (verified against
    https://questdb.com/docs/operations/backup/): CHECKPOINT CREATE before
    the file copy, CHECKPOINT RELEASE unconditionally after -- their docs
    explicitly require RELEASE to run "regardless of whether the copy
    operation succeeded or failed", which is exactly what
    post_backup_command guarantees. No restore_command needed: restoring
    is just putting the backed-up db/snapshot directories back.
    """
    monkeypatch.setattr(app_settings, "container_id", lambda app_id, store: None)
    monkeypatch.setattr(app_settings, "read_labels", lambda cid: {})
    cfg = CompanionConfig()
    cfg.backup.app_settings["questdb"] = AppBackupConfig(
        keep_running=True,
        pre_backup_command="curl -sf -G --data-urlencode 'query=CHECKPOINT CREATE' http://localhost:9000/exec",
        post_backup_command="curl -sf -G --data-urlencode 'query=CHECKPOINT RELEASE' http://localhost:9000/exec",
    )
    resolved = app_settings.resolve_app_settings(cfg, "questdb", "migrated")
    assert resolved.keep_running is True
    assert "CHECKPOINT CREATE" in resolved.pre_backup_command
    assert "CHECKPOINT RELEASE" in resolved.post_backup_command
    assert resolved.restore_command is None
    assert resolved.exclude_patterns == []


def test_run_app_command_raises_when_not_running():
    from runtipi_companion.system.shell import CommandError

    try:
        app_settings.run_app_command(
            "questdb", "migrated", "pre_backup_command", "echo hi", container_running=False, dry_run=False
        )
        raise AssertionError("expected CommandError")
    except CommandError as e:
        assert "isn't running" in str(e)


def test_run_app_command_raises_when_container_unresolvable(monkeypatch):
    from runtipi_companion.system.shell import CommandError

    monkeypatch.setattr(app_settings, "container_id", lambda app_id, store: None)
    try:
        app_settings.run_app_command(
            "questdb", "migrated", "pre_backup_command", "echo hi", container_running=True, dry_run=False
        )
        raise AssertionError("expected CommandError")
    except CommandError as e:
        assert "could not resolve" in str(e)


def test_run_app_command_execs_when_container_found(monkeypatch):
    calls = []
    monkeypatch.setattr(app_settings, "container_id", lambda app_id, store: "cid123")
    monkeypatch.setattr(app_settings, "exec_in_container", lambda cid, cmd, **k: calls.append((cid, cmd, k)))
    app_settings.run_app_command(
        "questdb", "migrated", "pre_backup_command", "echo hi", container_running=True, dry_run=False
    )
    assert calls == [("cid123", "echo hi", {"dry_run": False})]
