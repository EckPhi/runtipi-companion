import asyncio

from textual.widgets import Input

from runtipi_companion.config import load_config
from runtipi_companion.ui import validators as v
from runtipi_companion.ui.config_wizard import write_config
from runtipi_companion.ui.form_wizard import ConfigFormApp, RemoteForm

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


def test_apprise_urls_validator():
    assert v.apprise_urls_csv("") is None
    assert v.apprise_urls_csv("definitely not a url") is not None
    assert v.apprise_urls_csv("ntfy://ntfy.sh/my-topic") is None


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
