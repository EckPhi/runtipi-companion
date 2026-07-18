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


def test_stream_collapses_on_success(monkeypatch):
    monkeypatch.setattr(shell, "_should_stream", lambda **k: True)
    result = shell.run(["sh", "-c", "echo line1; echo line2"])
    assert result.ok
    assert "line1" in result.stdout and "line2" in result.stdout
    assert result.stderr == ""


def test_stream_failure_carries_merged_output(monkeypatch):
    monkeypatch.setattr(shell, "_should_stream", lambda **k: True)
    try:
        shell.run(["sh", "-c", "echo visible-out; echo visible-err >&2; exit 5"])
        raise AssertionError("expected CommandError")
    except shell.CommandError as e:
        assert e.returncode == 5
        assert "visible-out" in e.stderr
        assert "visible-err" in e.stderr


def test_no_stream_off_terminal():
    # pytest runs without a tty -> stream mode must be off by default
    assert shell._should_stream(quiet=False, interactive=False, input=None) is False


def test_stream_sudo_preauthenticates(monkeypatch):
    """Streamed sudo commands must refresh sudo's credential cache on the
    real terminal first -- the live tail would hide the password prompt."""
    calls = []

    def fake_subprocess_run(cmd, **kwargs):
        calls.append(cmd)

        class P:
            returncode = 0

        return P()

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append(cmd)
            import io

            self.stdout = io.StringIO("ok\n")
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(shell, "_should_stream", lambda **k: True)
    monkeypatch.setattr(shell.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    result = shell.run(["mkdir", "-p", "/opt/x"], sudo=True, quiet=False)
    assert result.ok
    assert calls[0] == ["sudo", "-v"]
    assert calls[1] == ["sudo", "mkdir", "-p", "/opt/x"]


def test_stream_sudo_auth_failure_raises(monkeypatch):
    def fake_subprocess_run(cmd, **kwargs):
        class P:
            returncode = 1

        return P()

    monkeypatch.setattr(shell, "_should_stream", lambda **k: True)
    monkeypatch.setattr(shell.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    try:
        shell.run(["mkdir", "/opt/x"], sudo=True)
        raise AssertionError("expected CommandError")
    except shell.CommandError as e:
        assert "sudo authentication failed" in e.stderr
