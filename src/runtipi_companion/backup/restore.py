from __future__ import annotations

import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional

from rich.console import Console

from ..config import CompanionConfig
from ..system.runtipi_cli import RuntipiCLI
from ..system.shell import confirm, run
from .rclone import RcloneClient

console = Console()


def list_local_backups(cfg: CompanionConfig, app_id: str, store: Optional[str] = None) -> list:
    root = Path(cfg.backup_local_path)
    if store:
        return sorted((root / store / app_id).glob(f"{app_id}-*.tar.gz"))
    return sorted(root.glob(f"*/{app_id}/{app_id}-*.tar.gz"))


def list_remote_backups(cfg: CompanionConfig, remote_name: str, app_id: str) -> list:
    remote = cfg.backup.remote(remote_name)
    if not remote:
        raise ValueError(
            f"Unknown remote '{remote_name}'. Configured remotes: " f"{[r.name for r in cfg.backup.remotes]}"
        )
    rclone = RcloneClient()
    all_files = rclone.list_files(remote.rclone_remote)
    return [f for f in all_files if Path(f).name.startswith(f"{app_id}-")]


def restore_backup(
    cfg: CompanionConfig,
    store: str,
    app_id: str,
    backup_file: str,
    *,
    from_remote: Optional[str] = None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> None:
    """Restore a single app from a runtipi-companion backup archive.

    This reverses `_archive_app` in backup.py: extracts the app/app-data/
    user-config members from the tar.gz and drops them back into their
    real locations under the runtipi install, replacing whatever is there.
    """
    cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=dry_run)

    if from_remote:
        remote = cfg.backup.remote(from_remote)
        if not remote:
            raise ValueError(f"Unknown remote '{from_remote}'")
        local_target = Path(cfg.backup.work_dir) / "restore" / backup_file
        local_target.parent.mkdir(parents=True, exist_ok=True)
        console.print(f"Downloading {backup_file} from remote '{from_remote}'")
        run(
            ["rclone", "copyto", f"{remote.rclone_remote}/{backup_file}", str(local_target)],
            dry_run=dry_run,
        )
        archive_path = local_target
    else:
        archive_path = Path(cfg.backup_local_path) / store / app_id / backup_file
        if not archive_path.exists() and not dry_run:
            raise FileNotFoundError(
                f"Backup not found: {archive_path}\n"
                f"Run 'runtipi-companion backup list {app_id}' to see what's available."
            )

    console.print(
        f"[bold red]This will overwrite the current app, app-data, and user-config " f"for {app_id}:{store}.[/bold red]"
    )
    if not confirm(f"Restore {app_id}:{store} from {Path(backup_file).name}?", assume_yes=dry_run or assume_yes):
        console.print("Aborted.")
        return

    was_running = cli.is_app_running(app_id, store) if not dry_run else True
    if was_running:
        console.print(f"Stopping {app_id}:{store}")
        cli.app_stop(f"{app_id}:{store}")
        if not dry_run:
            time.sleep(cfg.backup.sleep_duration)

    dest_map = {
        "app": Path(cfg.runtipi.path) / "apps" / store / app_id,
        "app-data": Path(cfg.runtipi.path) / "app-data" / store / app_id,
        "user-config": Path(cfg.runtipi.path) / "user-config" / store / app_id,
    }

    if dry_run:
        console.print(f"[yellow]DRY-RUN[/yellow] would extract {archive_path} and replace:")
        for dest in dest_map.values():
            console.print(f"  {dest}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=tmp)
            for arcname, dest in dest_map.items():
                src = Path(tmp) / arcname
                if not src.exists():
                    continue
                if dest.exists():
                    shutil.rmtree(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
        console.print(f"[green]Restored {app_id}:{store} from {Path(backup_file).name}[/green]")

    if was_running:
        console.print(f"Starting {app_id}:{store}")
        cli.app_start(f"{app_id}:{store}")
        if not dry_run:
            time.sleep(cfg.backup.sleep_duration)
