import textwrap
from pathlib import Path

from runtipi_companion import config_wizard, tui
from runtipi_companion.config import load_config
from runtipi_companion.rclone import RcloneClient

from .test_config_wizard import ScriptedPrompts


def write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return p


BASE_CONFIG = """
    runtipi:
      path: /opt/runtipi
    backup:
      remotes:
        - name: backblaze
          rclone_remote: "b2:bucket/runtipi"
          schedules:
            daily:
              retention: 14
"""


# ---- manage_remotes ----


def test_manage_remotes_requires_config(tmp_path):
    assert config_wizard.manage_remotes(str(tmp_path / "missing.yaml")) is False


def test_manage_remotes_add_and_save(tmp_path, monkeypatch):
    p = write_config(tmp_path, "runtipi:\n  path: /opt/runtipi\n")
    ScriptedPrompts(
        monkeypatch,
        [
            "a",  # add
            "gdrive",  # name
            "gdrive:runtipi-backups",  # target
            "",  # bandwidth
            True, 14,  # daily retention
            False, False, False,  # other schedules
            "s",  # save & exit
        ],
    )
    assert config_wizard.manage_remotes(str(p)) is True
    cfg = load_config(str(p))
    remote = cfg.backup.remote("gdrive")
    assert remote is not None
    assert remote.retention_for("daily") == 14
    # untouched sections survive the rewrite
    assert cfg.runtipi.path == "/opt/runtipi"


def test_manage_remotes_edit_keeps_other_fields(tmp_path, monkeypatch):
    p = write_config(tmp_path, BASE_CONFIG)
    ScriptedPrompts(
        monkeypatch,
        [
            "e",  # edit (single remote auto-picked)
            "backblaze",  # keep name
            "b2:new-bucket/runtipi",  # new target
            "",  # bandwidth
            True, 30,  # daily retention bumped
            False, False, False,
            "s",
        ],
    )
    assert config_wizard.manage_remotes(str(p)) is True
    cfg = load_config(str(p))
    remote = cfg.backup.remote("backblaze")
    assert remote.rclone_remote == "b2:new-bucket/runtipi"
    assert remote.retention_for("daily") == 30


def test_manage_remotes_toggle_and_remove(tmp_path, monkeypatch):
    p = write_config(tmp_path, BASE_CONFIG)
    ScriptedPrompts(monkeypatch, ["t", "s"])
    assert config_wizard.manage_remotes(str(p)) is True
    assert load_config(str(p)).backup.remote("backblaze").enabled is False

    ScriptedPrompts(monkeypatch, ["r", True, "s"])
    assert config_wizard.manage_remotes(str(p)) is True
    assert load_config(str(p)).backup.remotes == []


def test_manage_remotes_quit_discards(tmp_path, monkeypatch):
    p = write_config(tmp_path, BASE_CONFIG)
    original = p.read_text()
    ScriptedPrompts(monkeypatch, ["t", "q", True])  # toggle, quit, confirm discard
    assert config_wizard.manage_remotes(str(p)) is True
    assert p.read_text() == original
    assert load_config(str(p)).backup.remote("backblaze").enabled is True


# ---- interactive restore ----


def make_local_backups(tmp_path: Path) -> Path:
    root = tmp_path / "backups"
    for store, app, name in [
        ("migrated", "hello", "hello-daily-2026-07-01.tar.gz"),
        ("migrated", "hello", "hello-daily-2026-07-02.tar.gz"),
        ("migrated", "world", "world-weekly-2026-07-01.tar.gz"),
    ]:
        d = root / store / app
        d.mkdir(parents=True, exist_ok=True)
        (d / name).touch()
    return root


def load_cfg_with_backups(tmp_path: Path, root: Path, remotes: str = ""):
    p = write_config(
        tmp_path,
        f"""
        runtipi:
          path: /opt/runtipi
        backup:
          local_path: {root}
          {remotes}
        """,
    )
    return load_config(str(p))


def test_interactive_restore_local(tmp_path, monkeypatch):
    root = make_local_backups(tmp_path)
    cfg = load_cfg_with_backups(tmp_path, root)
    # no remotes -> source auto-picked; two apps -> pick #1 (hello:migrated);
    # two files newest-first -> pick #2 (the older one)
    ScriptedPrompts(monkeypatch, [1, 2])
    sel = tui.interactive_restore(cfg)
    assert sel.store == "migrated"
    assert sel.app_id == "hello"
    assert sel.backup_file == "hello-daily-2026-07-01.tar.gz"
    assert sel.from_remote is None


def test_interactive_restore_local_empty(tmp_path):
    cfg = load_cfg_with_backups(tmp_path, tmp_path / "empty")
    assert tui.interactive_restore(cfg) is None


def test_interactive_restore_from_remote(tmp_path, monkeypatch):
    root = make_local_backups(tmp_path)
    cfg = load_cfg_with_backups(
        tmp_path,
        root,
        remotes="""remotes:
            - name: backblaze
              rclone_remote: "b2:bucket/runtipi"
              schedules:
                daily:
                  retention: 14
          """,
    )
    monkeypatch.setattr(
        RcloneClient,
        "list_files",
        lambda self, remote: [
            "migrated/hello/hello-daily-2026-06-30.tar.gz",
            "stray-file.txt",
        ],
    )
    # pick source #2 (remote), app + file auto-picked (single options)
    ScriptedPrompts(monkeypatch, [2])
    sel = tui.interactive_restore(cfg)
    assert sel.from_remote == "backblaze"
    assert sel.app_id == "hello"
    assert sel.backup_file == "migrated/hello/hello-daily-2026-06-30.tar.gz"
