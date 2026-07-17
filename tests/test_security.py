import textwrap
from pathlib import Path

from runtipi_companion.config import load_config
from runtipi_companion.security import hardening as security


def write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return p


TS_ONLY_CONFIG = """
    runtipi:
      path: /opt/runtipi
    security:
      ufw:
        allowed_tcp_ports: [22]
      tailscale_only:
        enabled: true
        tailscale_ssh: true
        tailscale_port_udp: 41641
"""


def test_harden_tailscale_security_skips_when_disabled(tmp_path, capsys):
    cfg = load_config(str(write_config(tmp_path, "runtipi:\n  path: /opt/runtipi\n")))
    security.harden_tailscale_security(cfg, dry_run=True)
    assert "disabled in config, skipping" in capsys.readouterr().out


def test_harden_tailscale_security_requires_tailscale_binary(tmp_path, monkeypatch, capsys):
    cfg = load_config(str(write_config(tmp_path, TS_ONLY_CONFIG)))
    monkeypatch.setattr(security.shutil, "which", lambda name: None)
    security.harden_tailscale_security(cfg, dry_run=True)
    assert "tailscale binary not found" in capsys.readouterr().out


def test_harden_tailscale_security_dry_run_makes_no_changes(tmp_path, monkeypatch, capsys):
    cfg = load_config(str(write_config(tmp_path, TS_ONLY_CONFIG)))
    monkeypatch.setattr(security.shutil, "which", lambda name: "/usr/bin/tailscale")
    calls = []
    monkeypatch.setattr(security, "run", lambda *a, **k: calls.append((a, k)))
    security.harden_tailscale_security(cfg, dry_run=True)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert calls == []


def test_harden_tailscale_security_apply_runs_expected_commands(tmp_path, monkeypatch):
    cfg = load_config(str(write_config(tmp_path, TS_ONLY_CONFIG)))
    monkeypatch.setattr(security.shutil, "which", lambda name: "/usr/bin/tailscale")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class R:
            ok = True
            stderr = ""

        return R()

    monkeypatch.setattr(security, "run", fake_run)
    security.harden_tailscale_security(cfg, dry_run=False, assume_yes=True)

    assert ["tailscale", "up", "--ssh"] in calls
    assert ["ufw", "allow", "in", "on", "tailscale0"] in calls
    assert ["ufw", "allow", "41641/udp"] in calls
    assert ["ufw", "delete", "allow", "22/tcp"] in calls
    assert ["ufw", "default", "deny", "incoming"] in calls
    assert ["ufw", "--force", "enable"] in calls


def test_harden_tailscale_security_declined_confirm_makes_no_changes(tmp_path, monkeypatch):
    cfg = load_config(str(write_config(tmp_path, TS_ONLY_CONFIG)))
    monkeypatch.setattr(security.shutil, "which", lambda name: "/usr/bin/tailscale")
    calls = []
    monkeypatch.setattr(security, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(security, "confirm", lambda *a, **k: False)
    security.harden_tailscale_security(cfg, dry_run=False, assume_yes=False)
    assert calls == []
