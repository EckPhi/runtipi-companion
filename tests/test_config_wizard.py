import pytest

from runtipi_companion.config import ConfigError, load_config
from runtipi_companion.ui import config_wizard


def make_answers(**overrides):
    """A minimal, valid answers dict as gather_answers() would produce it."""
    answers = {
        "runtipi": {"path": "/opt/runtipi", "cli_path": None, "apps": []},
        "backup": {
            "work_dir": "/tmp/runtipi-companion",
            "local_path": None,
            "stop_apps": True,
            "sleep_duration": 10,
            "schedules": {"daily": {"retention": 7}},
            "remotes": [],
        },
        "security": {
            "ssh": {"disable_password_auth": True, "disable_root_login": True, "port": None},
            "ufw": {"enable": True, "allowed_tcp_ports": [22]},
            "fail2ban": {"enabled": True, "maxretry": 3, "bantime": 3600},
        },
        "tailscale": {
            "enabled": False,
            "auth_key_env": "TAILSCALE_AUTHKEY",
            "advertise_exit_node": False,
            "ssh": False,
        },
        "updates": {"auto_update_core": False, "auto_update_apps": False, "exclude_apps": []},
        "notify": {"webhook_url": None, "notify_on_success": False, "notify_on_failure": True},
    }
    answers.update(overrides)
    return answers


def test_write_config_round_trips(tmp_path):
    dest = tmp_path / "config.yaml"
    config_wizard.write_config(make_answers(), dest)
    cfg = load_config(str(dest))
    assert cfg.runtipi.path == "/opt/runtipi"
    assert cfg.backup.schedules["daily"].retention == 7
    assert cfg.backup.remotes == []


def test_write_config_with_remote(tmp_path):
    answers = make_answers()
    answers["backup"]["remotes"] = [
        {
            "name": "backblaze",
            "rclone_remote": "b2:bucket/runtipi",
            "enabled": True,
            "bandwidth_limit": "5M",
            "schedules": {"daily": {"retention": 14}, "weekly": {"retention": 8}},
        }
    ]
    dest = tmp_path / "config.yaml"
    config_wizard.write_config(answers, dest)
    cfg = load_config(str(dest))
    remote = cfg.backup.remote("backblaze")
    assert remote is not None
    assert remote.retention_for("daily") == 14
    assert remote.bandwidth_limit == "5M"


def test_write_config_rejects_invalid(tmp_path):
    answers = make_answers()
    answers["runtipi"]["path"] = "relative/path"  # validate_config requires absolute
    with pytest.raises(ConfigError):
        config_wizard.write_config(answers, tmp_path / "config.yaml")
    # Note: the invalid file is left on disk for inspection; the wizard
    # surfaces the error instead of silently succeeding.


def test_csv_list_parsing():
    assert config_wizard._csv_list("") == []
    assert config_wizard._csv_list("a, b ,c") == ["a", "b", "c"]
    assert config_wizard._or_none("  ") is None
    assert config_wizard._or_none(" x ") == "x"


class ScriptedPrompts:
    """Monkeypatch the wizard's prompt seams with a scripted answer queue."""

    def __init__(self, monkeypatch, answers):
        self.answers = list(answers)
        self.log = []
        monkeypatch.setattr(config_wizard, "_ask", self._make("ask"))
        monkeypatch.setattr(config_wizard, "_ask_bool", self._make("bool"))
        monkeypatch.setattr(config_wizard, "_ask_int", self._make("int"))

    def _make(self, kind):
        def prompt(text, default=None):
            assert self.answers, f"wizard asked more questions than scripted: {kind}: {text}"
            value = self.answers.pop(0)
            self.log.append((kind, text, value))
            return value

        return prompt


def test_full_wizard_run(tmp_path, monkeypatch):
    dest = tmp_path / "config.yaml"
    ScriptedPrompts(
        monkeypatch,
        [
            "/opt/runtipi",  # runtipi path
            "",  # cli_path (auto-detect)
            "",  # apps (all)
            "",  # local backup dir (default)
            "",  # host label (default hostname)
            "/tmp/runtipi-companion",  # work_dir
            True,  # stop apps
            True,
            7,  # keep daily locally, retention
            False,  # weekly
            False,  # monthly
            False,  # yearly
            True,  # add a remote
            "backblaze",  # remote name
            "b2:bucket/runtipi",  # rclone target
            "",  # bandwidth limit
            True,
            14,  # remote daily retention
            False,
            False,
            False,  # remote weekly/monthly/yearly
            False,  # add another remote
            True,  # recommended security defaults
            False,  # tailscale
            "",  # webhook
            str(dest),  # where to save
            True,  # confirm write
        ],
    )
    written = config_wizard.run_config_wizard()
    assert written == dest
    cfg = load_config(str(dest))
    assert cfg.backup.schedules["daily"].retention == 7
    assert cfg.backup.remote("backblaze").retention_for("daily") == 14
    assert cfg.security.ssh.disable_password_auth is True
    assert cfg.tailscale.enabled is False


def test_wizard_abort_writes_nothing(tmp_path, monkeypatch):
    dest = tmp_path / "config.yaml"
    ScriptedPrompts(
        monkeypatch,
        [
            "/opt/runtipi",
            "",
            "",  # runtipi section
            "",
            "",  # host label
            "/tmp/runtipi-companion",
            True,  # backup basics
            True,
            7,
            False,
            False,
            False,  # local schedules
            False,  # no remotes
            True,  # recommended security
            False,  # tailscale
            "",  # webhook
            str(dest),  # where to save
            False,  # decline final write
        ],
    )
    assert config_wizard.run_config_wizard() is None
    assert not dest.exists()


def test_remote_without_schedule_gets_daily_fallback(monkeypatch):
    ScriptedPrompts(
        monkeypatch,
        [
            True,  # add a remote
            "gdrive",
            "gdrive:runtipi-backups",
            "",  # bandwidth
            False,
            False,
            False,
            False,  # decline every schedule
            False,  # no more remotes
        ],
    )
    remotes = config_wizard._prompt_remotes()
    assert remotes[0]["schedules"] == {"daily": {"retention": 14}}


def test_needs_root(tmp_path, monkeypatch):
    from runtipi_companion.setup.wizard import needs_root

    # writable existing dir -> no elevation
    assert needs_root(tmp_path / "new" / "deep" / "dir") is False
    # unwritable nearest ancestor -> elevation (skip when running as root)
    import os

    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        try:
            assert needs_root(locked / "sub" / "dir") is True
        finally:
            locked.chmod(0o755)
        # as root, never
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert needs_root(locked / "sub") is False
