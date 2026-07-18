from __future__ import annotations

import os

from rich.console import Console

from ..config import CompanionConfig
from ..system.shell import confirm, run

console = Console()

INSTALL_URL = "https://tailscale.com/install.sh"


def install_tailscale(cfg: CompanionConfig, *, dry_run: bool = True, assume_yes: bool = False) -> None:
    ts_cfg = cfg.tailscale
    console.print("[bold]Tailscale install plan[/bold]")
    console.print(f"  curl -fsSL {INSTALL_URL} | sh")

    auth_key = os.environ.get(ts_cfg.auth_key_env)
    up_cmd = ["tailscale", "up"]
    if auth_key:
        up_cmd += [f"--authkey={auth_key}"]
        console.print(f"  tailscale up --authkey=<from ${ts_cfg.auth_key_env}>")
    else:
        console.print(
            f"  tailscale up   (${ts_cfg.auth_key_env} not set; you'll need to open the "
            f"printed login URL interactively)"
        )
    if ts_cfg.advertise_exit_node:
        up_cmd += ["--advertise-exit-node"]
    if ts_cfg.ssh:
        up_cmd += ["--ssh"]

    console.print(
        "\n[yellow]Reminder:[/yellow] once Tailscale is up, keep ports 80/443 closed on your public "
        "firewall and access Runtipi via its 100.x.x.x Tailscale IP instead -- see "
        "runtipi's VPS security guide (Option A: VPN access only). "
        "Pair this with 'runtipi-companion security harden --ufw'.\n"
    )

    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] -- nothing installed. Re-run with --apply to execute.")
        return

    if not confirm("Install and bring up Tailscale?", assume_yes):
        console.print("Aborted.")
        return

    # Both need the live terminal: the installer prints progress, and
    # without an authkey `tailscale up` prints a login URL and blocks until
    # the user visits it -- captured output would look like a freeze.
    run(["sh", "-c", f"curl -fsSL {INSTALL_URL} | sh"], sudo=True, interactive=True)
    run(up_cmd, sudo=True, interactive=True)
    console.print(
        "[green]Tailscale installed.[/green] Run 'runtipi-companion tailscale status' to see your device's IP."
    )


def status() -> None:
    run(["tailscale", "status"], check=False)
