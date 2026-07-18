import asyncio

from textual.widgets import Input

from runtipi_companion.config import load_config
from runtipi_companion.ui import validators as v
from runtipi_companion.ui.config_wizard import write_config
from runtipi_companion.ui.form_wizard import ConfigFormApp, NotifyUrlRow, RemoteForm

# ---- pure validators (the realtime checks) ----


def test_absolute_path_validator():
    assert v.absolute_path("/opt/runtipi") is None
    assert v.absolute_path("relative/path") is not None
    assert v.absolute_path("") is not None
    assert v.optional_absolute_path("") is None
    assert v.optional_absolute_path("rel") is not None


def test_int_and_port_validators():
    assert v.required_int("7") is None
    assert v.required_int("0") is not None
    assert v.required_int("x") is not None
    assert v.optional_port("") is None
    assert v.optional_port("2222") is None
    assert v.optional_port("70000") is not None
    assert v.csv_ports("22, 443") is None
    assert v.csv_ports("22, nope") is not None
    assert v.csv_ports("") is not None


def test_rclone_target_validator():
    assert v.rclone_target("b2:bucket/path") is None
    assert v.rclone_target("no-colon") is not None
    assert v.rclone_target("b2:bucket/") is not None
    assert v.remote_name("backblaze") is None
    assert v.remote_name("bad name") is not None


def test_apprise_url_validator():
    assert v.apprise_url("") is not None  # empty row -> remove it instead
    assert v.apprise_url("definitely not a url") is not None
    assert v.apprise_url("ntfy://ntfy.sh/my-topic") is None


# ---- the form itself ----


def _drive(actions, dest):
    async def main():
        app = ConfigFormApp(str(dest))
        async with app.run_test(size=(100, 40)) as pilot:
            await actions(app, pilot)
        return app.return_value

    return asyncio.run(main())


def test_form_defaults_produce_valid_config(tmp_path):
    dest = tmp_path / "config.yaml"

    async def actions(app, pilot):
        await pilot.pause()
        app.action_save()
        await pilot.pause()

    answers = _drive(actions, dest)
    assert answers is not None
    assert answers.pop("_save_path") == str(dest)
    write_config(answers, dest)
    cfg = load_config(str(dest))
    assert cfg.runtipi.path == "/opt/runtipi"
    assert cfg.backup.schedules["daily"].retention == 7
    assert cfg.security.ssh.disable_password_auth is True  # recommended preset
    assert cfg.backup.remotes == []


def test_form_blocks_save_on_invalid_field(tmp_path):
    dest = tmp_path / "config.yaml"

    async def actions(app, pilot):
        await pilot.pause()
        app.query_one("#runtipi-path", Input).value = "not-absolute"
        await pilot.pause()
        app.action_save()
        await pilot.pause()
        # still running -> save was blocked; cancel to end the test
        app.action_cancel()
        await pilot.pause()

    assert _drive(actions, dest) is None


def test_form_collects_added_remote(tmp_path):
    dest = tmp_path / "config.yaml"

    async def actions(app, pilot):
        await pilot.pause()
        remotes = app.query_one("#remotes")
        await remotes.mount(RemoteForm())
        await pilot.pause()
        form = app.query_one(RemoteForm)
        form.query_one(".r-name", Input).value = "proton"
        form.query_one(".r-target", Input).value = "proton:backups/runtipi"
        await pilot.pause()
        app.action_save()
        await pilot.pause()

    answers = _drive(actions, dest)
    assert answers is not None
    (remote,) = answers["backup"]["remotes"]
    assert remote["name"] == "proton"
    assert remote["rclone_remote"] == "proton:backups/runtipi"
    assert "daily" in remote["schedules"]


def test_form_collects_notify_url_rows(tmp_path):
    dest = tmp_path / "config.yaml"

    async def actions(app, pilot):
        await pilot.pause()
        container = app.query_one("#notify-urls")
        await container.mount(NotifyUrlRow())
        await container.mount(NotifyUrlRow())
        await pilot.pause()
        rows = list(app.query(NotifyUrlRow))
        rows[0].query_one(".notify-url", Input).value = "ntfy://ntfy.sh/my-topic"
        # second row left empty -> dropped on collect (but would block save
        # via its validator; clear it by removing the row)
        await rows[1].remove()
        await pilot.pause()
        app.action_save()
        await pilot.pause()

    answers = _drive(actions, dest)
    assert answers is not None
    assert answers["notify"]["urls"] == ["ntfy://ntfy.sh/my-topic"]


def test_form_prefills_and_preserves_unexposed_fields(tmp_path):
    """Reopening the wizard on an existing config must show its values and
    carry through fields the form doesn't expose."""

    initial = {
        "version": 2,
        "runtipi": {"path": "/srv/runtipi", "cli_path": None, "apps": ["jellyfin"]},
        "backup": {
            "work_dir": "/tmp/rc",
            "local_path": None,
            "host_label": "nas",
            "stop_apps": False,
            "sleep_duration": 42,
            "schedules": {"weekly": {"retention": 9}},
            "remotes": [
                {
                    "name": "proton",
                    "rclone_remote": "proton:backups",
                    "enabled": False,
                    "bandwidth_limit": "5M",
                    "schedules": {"daily": {"retention": 14}},
                }
            ],
        },
        "security": {
            "ssh": {"disable_password_auth": True, "disable_root_login": True, "port": None},
            "ufw": {"enable": True, "allowed_tcp_ports": [22]},
            "fail2ban": {"enabled": True, "maxretry": 3, "bantime": 3600},
            "tailscale_only": {"enabled": False, "tailscale_ssh": False, "tailscale_port_udp": 55555},
        },
        "tailscale": {"enabled": True, "auth_key_env": "MY_KEY", "advertise_exit_node": True, "ssh": True},
        "updates": {
            "auto_update_core": True,
            "auto_update_apps": False,
            "exclude_apps": ["grist"],
            "backup_before": False,
        },
        "notify": {
            "urls": ["ntfy://ntfy.sh/topic"],
            "webhook_url": "https://legacy.example/hook",
            "notify_on_success": True,
            "notify_on_failure": True,
        },
    }
    dest = tmp_path / "config.yaml"

    async def actions(app, pilot):
        await pilot.pause()
        # prefill visible
        assert app.query_one("#runtipi-path", Input).value == "/srv/runtipi"
        assert app.query_one("#host-label", Input).value == "nas"
        assert len(list(app.query(RemoteForm))) == 1
        assert len(list(app.query(NotifyUrlRow))) == 1
        app.action_save()
        await pilot.pause()

    async def main():
        app = ConfigFormApp(str(dest), initial=initial)
        async with app.run_test(size=(100, 40)) as pilot:
            await actions(app, pilot)
        return app.return_value

    import asyncio

    answers = asyncio.run(main())
    assert answers is not None
    answers.pop("_save_path")
    # visible values round-trip
    assert answers["runtipi"]["apps"] == ["jellyfin"]
    assert answers["backup"]["schedules"] == {"weekly": {"retention": 9}}
    (remote,) = answers["backup"]["remotes"]
    assert remote["enabled"] is False
    assert remote["bandwidth_limit"] == "5M"
    assert answers["notify"]["urls"] == ["ntfy://ntfy.sh/topic"]
    # unexposed values preserved
    assert answers["backup"]["sleep_duration"] == 42
    assert answers["updates"] == {
        "auto_update_core": True,
        "auto_update_apps": False,
        "exclude_apps": ["grist"],
        "backup_before": False,
    }
    assert answers["notify"]["webhook_url"] == "https://legacy.example/hook"
    assert answers["tailscale"]["auth_key_env"] == "MY_KEY"
    assert answers["security"]["tailscale_only"]["tailscale_port_udp"] == 55555
    # config still valid end to end
    import yaml as _yaml  # noqa: F401

    write_config(answers, dest)
    cfg = load_config(str(dest))
    assert cfg.backup.host_label == "nas"
