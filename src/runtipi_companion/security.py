from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from rich.console import Console

from .config import CompanionConfig
from .shell import confirm, run

console = Console()

SSHD_CONFIG = Path("/etc/ssh/sshd_config")


def _current_user_has_authorized_keys() -> bool:
    """Safety check before we disable SSH password auth: make sure *some*
    account we plausibly connect as already has a key, otherwise we could
    lock ourselves out of a remote box entirely.
    """
    home = Path.home()
    candidates = [home / ".ssh" / "authorized_keys", Path("/root/.ssh/authorized_keys")]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        candidates.append(Path(f"/home/{sudo_user}/.ssh/authorized_keys"))

    def _has_content(path: Path) -> bool:
        # Path.exists()/.stat() can raise (not just return False) when a
        # parent directory isn't readable by us -- e.g. this tool running
        # as a non-root user checking /root/.ssh. Treat that as "can't
        # confirm a key exists here", not a crash.
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    return any(_has_content(c) for c in candidates)


def harden_ssh(cfg: CompanionConfig, *, dry_run: bool = True, assume_yes: bool = False, force: bool = False) -> None:
    ssh_cfg = cfg.security.ssh
    console.print("[bold]SSH hardening plan[/bold]")

    if ssh_cfg.disable_password_auth and not force and not _current_user_has_authorized_keys():
        console.print(
            "[red]No authorized_keys found for root, the current user, or $SUDO_USER.[/red]\n"
            "Refusing to disable password authentication -- doing so now could lock you out "
            "of this box permanently. Set up an SSH key first "
            "(ssh-keygen + ssh-copy-id), or pass --force to override at your own risk."
        )
        return

    changes = []
    if ssh_cfg.disable_password_auth:
        changes.append(("PasswordAuthentication", "no"))
    if ssh_cfg.disable_root_login:
        changes.append(("PermitRootLogin", "no"))
    if ssh_cfg.port:
        changes.append(("Port", str(ssh_cfg.port)))

    if not changes:
        console.print("Nothing to change (all ssh hardening options disabled in config).")
        return

    for key, value in changes:
        console.print(f"  {key} {value}")

    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] -- no changes made. Re-run with --apply to write these.")
        return

    if not confirm("Apply these sshd_config changes and restart sshd?", assume_yes):
        console.print("Aborted.")
        return

    backup_path = SSHD_CONFIG.with_suffix(".bak-runtipi-companion")
    if SSHD_CONFIG.exists() and not backup_path.exists():
        shutil.copy2(SSHD_CONFIG, backup_path)
        console.print(f"Backed up sshd_config to {backup_path}")

    text = SSHD_CONFIG.read_text() if SSHD_CONFIG.exists() else ""
    for key, value in changes:
        pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s+.*$", re.MULTILINE)
        replacement = f"{key} {value}"
        if pattern.search(text):
            text = pattern.sub(replacement, text)
        else:
            text = text.rstrip("\n") + f"\n{replacement}\n"
    SSHD_CONFIG.write_text(text)

    test_result = run(["sshd", "-t"], sudo=True, check=False)
    if not test_result.ok:
        # Roll back immediately -- a broken sshd_config can mean no more SSH access.
        if backup_path.exists():
            shutil.copy2(backup_path, SSHD_CONFIG)
        console.print(
            f"[red]sshd -t reported an invalid config, rolled back sshd_config. "
            f"Nothing was restarted.[/red]\n{test_result.stderr}"
        )
        return

    run(["systemctl", "restart", "sshd"], sudo=True)
    console.print("[green]sshd hardened and restarted.[/green]")
    if ssh_cfg.port:
        console.print(
            f"[yellow]Before you disconnect, open a NEW terminal and confirm you can still connect with: "
            f"ssh -p {ssh_cfg.port} user@host[/yellow]"
        )
        console.print("Also update your firewall: runtipi-companion security harden --ufw --apply")


def harden_ufw(cfg: CompanionConfig, *, dry_run: bool = True, assume_yes: bool = False) -> None:
    ufw_cfg = cfg.security.ufw
    if not ufw_cfg.enable:
        console.print("UFW disabled in config, skipping.")
        return

    console.print("[bold]UFW firewall plan[/bold]")
    ports = sorted(set(ufw_cfg.allowed_tcp_ports))
    if cfg.security.ssh.port and cfg.security.ssh.port not in ports:
        ports.append(cfg.security.ssh.port)
    for p in ports:
        console.print(f"  allow {p}/tcp")
    console.print("  default deny incoming")

    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] -- no changes made. Re-run with --apply to write these.")
        return

    if not confirm("Apply these UFW rules? (SSH port(s) will be allowed first, so you shouldn't be locked out)", assume_yes):
        console.print("Aborted.")
        return

    run(["apt-get", "install", "-y", "ufw"], sudo=True)
    for p in ports:
        run(["ufw", "allow", f"{p}/tcp"], sudo=True)
    run(["ufw", "--force", "enable"], sudo=True)
    console.print("[green]UFW enabled.[/green] Run 'runtipi-companion security status' to review.")


def harden_fail2ban(cfg: CompanionConfig, *, dry_run: bool = True, assume_yes: bool = False) -> None:
    f2b_cfg = cfg.security.fail2ban
    if not f2b_cfg.enabled:
        console.print("fail2ban disabled in config, skipping.")
        return

    console.print("[bold]fail2ban plan[/bold]")
    console.print(f"  [sshd] maxretry={f2b_cfg.maxretry} bantime={f2b_cfg.bantime}s")

    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] -- no changes made. Re-run with --apply to write these.")
        return

    if not confirm("Install and configure fail2ban for sshd?", assume_yes):
        console.print("Aborted.")
        return

    run(["apt-get", "install", "-y", "fail2ban"], sudo=True)
    jail_local = Path("/etc/fail2ban/jail.local")
    content = (
        "[sshd]\n"
        "enabled = true\n"
        "port = ssh\n"
        "filter = sshd\n"
        "logpath = /var/log/auth.log\n"
        f"maxretry = {f2b_cfg.maxretry}\n"
        f"bantime = {f2b_cfg.bantime}\n"
    )
    jail_local.write_text(content)
    run(["systemctl", "enable", "--now", "fail2ban"], sudo=True)
    console.print("[green]fail2ban configured and running.[/green]")


def status(cfg: CompanionConfig) -> None:
    console.print("[bold]Security status[/bold]")
    console.print("\n[bold]sshd effective config (password/root login):[/bold]")
    run(["sh", "-c", "sshd -T 2>/dev/null | grep -E 'passwordauthentication|permitrootlogin|^port'"], sudo=True, check=False)
    console.print("\n[bold]UFW:[/bold]")
    run(["ufw", "status", "verbose"], sudo=True, check=False)
    console.print("\n[bold]fail2ban:[/bold]")
    run(["systemctl", "is-active", "fail2ban"], check=False)
    run(["fail2ban-client", "status", "sshd"], sudo=True, check=False)
