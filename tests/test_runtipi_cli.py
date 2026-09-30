from runtipi_companion.system import runtipi_cli
from runtipi_companion.system.shell import RunResult


def test_app_start_has_a_bounded_wait(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return RunResult(list(cmd), 0, "", "", False)

    monkeypatch.setattr(runtipi_cli, "run", fake_run)
    cli = runtipi_cli.RuntipiCLI("/runtipi", cli_path="/runtipi/runtipi-cli")

    cli.app_start("broken:migrated")

    assert seen["cmd"] == ["/runtipi/runtipi-cli", "app", "start", "broken:migrated"]
    assert seen["kwargs"]["timeout"] == runtipi_cli.APP_START_TIMEOUT_SECONDS
