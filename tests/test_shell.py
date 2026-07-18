import subprocess

from runtipi_companion.system import shell


def test_run_captures_by_default():
    result = shell.run(["echo", "hi"], quiet=True)
    assert result.ok
    assert result.stdout.strip() == "hi"


def test_run_interactive_inherits_stdio(monkeypatch):
    """Interactive mode must not capture -- `tailscale up` prints its login
    URL and blocks; captured output looks like a freeze."""
    seen = {}

    def fake_subprocess_run(cmd, **kwargs):
        seen["kwargs"] = kwargs

        class P:
            returncode = 0
            stdout = None
            stderr = None

        return P()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    result = shell.run(["tailscale", "up"], interactive=True, quiet=True)
    assert "capture_output" not in seen["kwargs"]
    assert "input" not in seen["kwargs"]
    assert result.ok
    assert result.stdout == ""


def test_run_interactive_failure_raises_clean_commanderror(monkeypatch):
    def fake_subprocess_run(cmd, **kwargs):
        class P:
            returncode = 3
            stdout = None
            stderr = None

        return P()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    try:
        shell.run(["x"], interactive=True, quiet=True)
        raise AssertionError("expected CommandError")
    except shell.CommandError as e:
        assert e.returncode == 3
        assert e.stderr == ""
