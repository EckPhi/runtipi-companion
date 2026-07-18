from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from rich.console import Console

from ..config import CompanionConfig
from ..system.shell import confirm, run

console = Console()

SSHD_CONFIG = Path("/etc/ssh/sshd_config")


# Root-owned files under /etc must be touched through sudo'd commands, not
# Python file I/O -- the CLI usually runs as a normal user.


def _sudo_read(path: Path) -> str:
    return run(["cat", str(path)], sudo=True, quiet=True).stdout


def _sudo_write(path: Path, content: str) -> None:
    run(["tee", str(path)], sudo=True, quiet=True, input=content)


def _sudo_copy(src: Path, dst: Path) -> None:
    run(["cp", "-a", str(src), str(dst)], sudo=True, quiet=True)


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
        _sudo_copy(SSHD_CONFIG, backup_path)
        console.print(f"Backed up sshd_config to {backup_path}")

    text = _sudo_read(SSHD_CONFIG) if SSHD_CONFIG.exists() else ""
    for key, value in changes:
        pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s+.*$", re.MULTILINE)
        replacement = f"{key} {value}"
        if pattern.search(text):
            text = pattern.sub(replacement, text)
        else:
            text = text.rstrip("\n") + f"\n{replacement}\n"
    _sudo_write(SSHD_CONFIG, text)

    test_result = run(["sshd", "-t"], sudo=True, check=False)
    if not test_result.ok:
        # Roll back immediately -- a broken sshd_config can mean no more SSH access.
        if backup_path.exists():
            _sudo_copy(backup_path, SSHD_CONFIG)
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

    if not confirm(
        "Apply these UFW rules? (SSH port(s) will be allowed first, so you shouldn't be locked out)", assume_yes
    ):
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
    _sudo_write(jail_local, content)
    run(["systemctl", "enable", "--now", "fail2ban"], sudo=True)
    console.print("[green]fail2ban configured and running.[/green]")


def harden_tailscale_security(cfg: CompanionConfig, *, dry_run: bool = True, assume_yes: bool = False) -> None:
    """VPN-only lockdown: allow traffic only on the tailscale0 interface (plus
    tailscale's own coordination port), and switch SSH access over to
    `tailscale ssh`. Mirrors
    https://dev.to/binsarjr/turn-your-vps-into-an-impenetrable-fortress-how-to-make-your-public-server-private-using-tailscale-and-ufw-3841
    """
    ts_cfg = cfg.security.tailscale_only
    if not ts_cfg.enabled:
        console.print("Tailscale-only lockdown disabled in config, skipping.")
        return

    console.print("[bold]Tailscale-only access plan[/bold]")
    if shutil.which("tailscale") is None:
        console.print("[red]tailscale binary not found.[/red] Run 'runtipi-companion setup tailscale --apply' first.")
        return

    if ts_cfg.tailscale_ssh:
        console.print("  tailscale up --ssh")
    console.print("  ufw allow in on tailscale0")
    console.print(f"  ufw allow {ts_cfg.tailscale_port_udp}/udp   (tailscale's own coordination port, stays public)")
    public_ports = sorted(set(cfg.security.ufw.allowed_tcp_ports))
    if cfg.security.ssh.port and cfg.security.ssh.port not in public_ports:
        public_ports.append(cfg.security.ssh.port)
    for p in public_ports:
        console.print(f"  ufw delete allow {p}/tcp   (was publicly allowed, now tailscale0-only)")
    console.print("  ufw default deny incoming")

    console.print(
        "\n[yellow]After this, the box is reachable only via its 100.x.x.x tailscale IP "
        "(or through 'tailscale ssh').[/yellow] Make sure tailscale is already up and you have "
        "a tested connection to this machine over the tailnet before applying, or you may lock "
        "yourself out.\n"
    )

    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] -- no changes made. Re-run with --apply to write these.")
        return

    if not confirm(
        "Apply tailscale-only lockdown? (public TCP access to this box will be cut, keep a tailnet "
        "connection open in another terminal)",
        assume_yes,
    ):
        console.print("Aborted.")
        return

    run(["apt-get", "install", "-y", "ufw"], sudo=True)

    if ts_cfg.tailscale_ssh:
        run(["tailscale", "up", "--ssh"], sudo=True)

    run(["ufw", "allow", "in", "on", "tailscale0"], sudo=True)
    run(["ufw", "allow", f"{ts_cfg.tailscale_port_udp}/udp"], sudo=True)
    for p in public_ports:
        # Rules may not exist yet on a fresh box -- absence isn't an error.
        run(["ufw", "delete", "allow", f"{p}/tcp"], sudo=True, check=False)
    run(["ufw", "default", "deny", "incoming"], sudo=True)
    run(["ufw", "--force", "enable"], sudo=True)

    console.print("[green]Tailscale-only lockdown applied.[/green]")
    console.print(
        "[yellow]Before you disconnect, open a NEW terminal and confirm you can still connect "
        "over the tailnet (tailscale ssh user@host, or ssh over the 100.x.x.x address).[/yellow]"
    )


def status(cfg: CompanionConfig) -> None:
    # interactive: these commands ARE the output -- captured mode would
    # swallow everything and print nothing.
    console.print("[bold]Security status[/bold]")
    console.print("\n[bold]sshd effective config (password/root login):[/bold]")
    run(
        ["sh", "-c", "sshd -T 2>/dev/null | grep -E 'passwordauthentication|permitrootlogin|^port'"],
        sudo=True,
        check=False,
        interactive=True,
    )
    console.print("\n[bold]UFW:[/bold]")
    run(["ufw", "status", "verbose"], sudo=True, check=False, interactive=True)
    console.print("\n[bold]fail2ban:[/bold]")
    run(["systemctl", "is-active", "fail2ban"], check=False, interactive=True)
    run(["fail2ban-client", "status", "sshd"], sudo=True, check=False, interactive=True)
    if cfg.security.tailscale_only.enabled:
        console.print("\n[bold]Tailscale:[/bold]")
        run(["tailscale", "status"], check=False, interactive=True)
