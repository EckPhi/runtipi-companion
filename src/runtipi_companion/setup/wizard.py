from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

from ..backup.rclone import RcloneClient
from ..config import CompanionConfig
from ..system.runtipi_cli import RuntipiCLI, RuntipiCLIError
from ..system.shell import confirm, run

console = Console()


def run_wizard(cfg: CompanionConfig, *, dry_run: bool = True, assume_yes: bool = False) -> None:
    """Guided first-run: get a fresh box to a working, backed-up Runtipi
    install. Doesn't do anything destructive on its own -- each step is a
    separate confirm (or dry-run preview).
    """
    console.print("[bold]Runtipi Companion setup wizard[/bold]")

    # The uniform dry-run default surprises people mid-wizard ("I answered
    # yes to the clone, why did nothing happen?"). Say so upfront, and on an
    # interactive terminal offer to flip to apply mode right here -- the
    # wizard confirms every step individually anyway.
    if dry_run:
        console.print("[yellow]Preview mode: commands are printed, nothing is changed (--dry-run default).[/yellow]")
        if not assume_yes and sys.stdin.isatty() and confirm("Apply changes for real this run?", False):
            dry_run = False

    runtipi_path = Path(cfg.runtipi.path)
    if not runtipi_path.exists():
        console.print(f"Runtipi path {runtipi_path} does not exist yet.")
        if confirm(f"Clone runtipi into {runtipi_path}?", assume_yes):
            # The clone runs for real even in dry-run mode: it only creates a
            # new directory (nothing to preview, nothing overwritten), and
            # every later step depends on the files actually existing.
            run(["git", "clone", "https://github.com/runtipi/runtipi.git", str(runtipi_path)], dry_run=False)
        else:
            console.print("Skipping clone -- make sure runtipi.path in your config points at an existing install.")
            return
    else:
        console.print(f"Found existing runtipi install at {runtipi_path}")

    try:
        cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=dry_run)
        console.print(f"Found runtipi-cli at {cli.cli_path}")
    except RuntipiCLIError as e:
        console.print(f"[red]{e}[/red]")
        console.print(
            "If you just cloned runtipi, runtipi-cli is built as part of first boot. "
            "Set runtipi.cli_path once you know where it landed and re-run this wizard."
        )
        return

    if confirm("Run 'runtipi-cli prepare' now? (checks permissions, generates config)", assume_yes):
        cli.prepare()

    if confirm("Start Runtipi now ('runtipi-cli start')?", assume_yes):
        cli.start()

    if not dry_run:
        Path(cfg.backup_local_path).mkdir(parents=True, exist_ok=True)
        Path(cfg.backup.work_dir).mkdir(parents=True, exist_ok=True)
    console.print(f"Backup directories ready: {cfg.backup_local_path}, {cfg.backup.work_dir}")

    if cfg.backup.remotes:
        rclone = RcloneClient(dry_run=dry_run)
        if not rclone.is_installed():
            console.print(
                "[yellow]rclone is not installed but remotes are configured. "
                "Run 'runtipi-companion setup rclone' to install and configure "
                "it before your first backup.[/yellow]"
            )
        else:
            configured = set(rclone.list_remotes())
            for remote in cfg.backup.remotes:
                remote_name = remote.rclone_remote.split(":")[0]
                if remote_name not in configured:
                    console.print(
                        f"[yellow]rclone remote '{remote_name}' (used by backup remote "
                        f"'{remote.name}') isn't configured yet. Run 'rclone config' to add it.[/yellow]"
                    )

    console.print("\n[green]Base setup complete.[/green] Suggested next steps:\n")
    console.print("  runtipi-companion setup rclone               (install/configure backup remotes)")
    console.print("  runtipi-companion security harden --all      (defaults to a dry-run preview)")
    console.print("  runtipi-companion setup tailscale")
    console.print("  runtipi-companion setup services --apply     (systemd timers for automated backups)")
    console.print("  runtipi-companion backup run --type daily --apply\n")
