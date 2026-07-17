"""Install rclone and help configure the remotes the config file references.

`rclone config` itself stays interactive on purpose: it handles OAuth flows
and credentials for ~70 storage backends, and reimplementing or automating
that would mean handling other people's credentials ourselves.
"""

from __future__ import annotations

import subprocess
import sys

from rich.console import Console

from ..backup.rclone import RcloneClient
from ..config import CompanionConfig
from ..system.shell import confirm, run

console = Console()


def missing_remotes(cfg: CompanionConfig, configured: list) -> list:
    """rclone remote names the config references but rclone doesn't know."""
    missing = []
    for remote in cfg.backup.remotes:
        if not remote.enabled:
            continue
        name = remote.rclone_remote.split(":")[0]
        if name not in configured and name not in missing:
            missing.append(name)
    return missing


def setup_rclone(cfg: CompanionConfig, *, dry_run: bool = True, assume_yes: bool = False) -> None:
    client = RcloneClient(dry_run=dry_run)

    if client.is_installed():
        console.print("[green]rclone is installed.[/green]")
    else:
        console.print("rclone is not installed.")
        if dry_run:
            console.print("[yellow]DRY-RUN[/yellow] would run: apt-get install -y rclone")
        elif confirm("Install rclone via apt-get?", assume_yes):
            run(["apt-get", "install", "-y", "rclone"], sudo=True)
        else:
            console.print("Skipped. Install it yourself: https://rclone.org/install/")
            return

    if not cfg.backup.remotes:
        console.print("No backup remotes in your config. Add some with 'backup remotes' first, then re-run this.")
        return

    configured = [] if dry_run and not client.is_installed() else client.list_remotes()
    missing = missing_remotes(cfg, configured)
    if not missing:
        console.print("[green]Every rclone remote referenced by your config is configured.[/green]")
        return

    console.print(f"[yellow]rclone remotes still unconfigured: {', '.join(missing)}[/yellow]")
    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] would offer to launch 'rclone config' interactively.")
        return
    if sys.stdin.isatty() and confirm("Launch 'rclone config' now to set them up?", assume_yes=False):
        # Hand the terminal over completely: rclone config is a full
        # interactive TUI (menus, OAuth links), so no output capturing.
        subprocess.call(["rclone", "config"])
    else:
        console.print("Run 'rclone config' when ready, then verify with 'runtipi-companion doctor'.")
