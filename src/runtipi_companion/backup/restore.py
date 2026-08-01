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
from ..system.shell import CommandError, confirm, run
from .rclone import RcloneClient
from .retention import select_latest

console = Console()


class RestoreRunError(RuntimeError):
    """One or more apps failed while restoring a batch. Restoring continues
    past a single app's failure so a comma-separated batch is best-effort,
    not all-or-nothing; raised at the end with a summary."""


def list_local_backups(cfg: CompanionConfig, app_id: str, store: Optional[str] = None) -> list:
    root = Path(cfg.backup_local_path)
    if store:
        return sorted((root / store / app_id).glob(f"{app_id}-*.tar.gz"))
    return sorted(root.glob(f"*/{app_id}/{app_id}-*.tar.gz"))


def _remote_files(cfg: CompanionConfig, remote_name: str, host: str) -> list:
    """Remote-relative paths (including the host prefix) under one host's
    subtree of the given remote."""
    remote = cfg.backup.remote(remote_name)
    if not remote:
        raise ValueError(f"Unknown remote '{remote_name}'. Configured remotes: {[r.name for r in cfg.backup.remotes]}")
    rclone = RcloneClient()
    return [f"{host}/{f}" for f in rclone.list_files(f"{remote.rclone_remote}/{host}")]


def list_remote_backups(cfg: CompanionConfig, remote_name: str, app_id: str, host: Optional[str] = None) -> list:
    files = _remote_files(cfg, remote_name, host or cfg.host_label)
    return [f for f in files if Path(f).name.startswith(f"{app_id}-")]


def list_remote_hosts(cfg: CompanionConfig, remote_name: str) -> list:
    """Host subfolders present on a remote -- other machines backing up to
    the same bucket show up here, so their backups can be restored too."""
    remote = cfg.backup.remote(remote_name)
    if not remote:
        raise ValueError(f"Unknown remote '{remote_name}'")
    return RcloneClient().list_dirs(remote.rclone_remote)


def latest_per_app(files: list) -> list:
    """Group <...>/<store>/<app>/<file> paths and reduce each app to its
    newest archive (by the date in the filename). Returns
    [(store, app_id, filename), ...] sorted by app.
    """
    grouped = {}
    for f in files:
        parts = Path(f).parts
        if len(parts) < 3:
            continue
        grouped.setdefault((parts[-3], parts[-2]), []).append(Path(f).name)
    out = []
    for (store, app_id), names in sorted(grouped.items()):
        newest = select_latest(names)
        if newest:
            out.append((store, app_id, newest))
    return out


def _latest_by_app_id(cfg: CompanionConfig, *, from_remote: Optional[str] = None, host: Optional[str] = None) -> dict:
    """{app_id: (store, filename)} for every app's newest archive, local or
    from one host's subtree of a remote. If an app id exists under more
    than one store, the last one wins -- restore a specific store's copy
    with a single-app `restore run --store` instead."""
    if from_remote:
        files = _remote_files(cfg, from_remote, host or cfg.host_label)
    else:
        root = Path(cfg.backup_local_path)
        files = [str(p.relative_to(root)) for p in root.glob("*/*/*.tar.gz")]
    return {app_id: (store, filename) for store, app_id, filename in latest_per_app(files)}


def restore_apps(
    cfg: CompanionConfig,
    app_ids: list,
    *,
    from_remote: Optional[str] = None,
    host: Optional[str] = None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> dict:
    """Restore several apps at once, each from its newest backup. Mirrors
    run_backup's per-app isolation: one app's failure doesn't cancel the
    rest of the batch, and RestoreRunError is raised at the end with a
    summary so the exit code reflects any failure.

    Returns {"restored": [...], "skipped": [...], "failed": [...]} (ids).
    A "skip" is the user declining that app's overwrite confirmation --
    not a failure.
    """
    latest = _latest_by_app_id(cfg, from_remote=from_remote, host=host)
    missing = [a for a in app_ids if a not in latest]
    if missing:
        raise ValueError(
            f"No backup found for: {', '.join(missing)}. "
            f"Run 'runtipi-companion backup list{f' --remote {from_remote}' if from_remote else ''}' "
            f"to see what's available."
        )

    restored, skipped, failed = [], [], []
    for app_id in app_ids:
        store, filename = latest[app_id]
        try:
            if restore_backup(
                cfg, store, app_id, filename, from_remote=from_remote, host=host, assume_yes=assume_yes, dry_run=dry_run
            ):
                restored.append(app_id)
            else:
                skipped.append(app_id)
        except Exception as e:
            console.print(f"[red]Restore of {app_id}:{store} failed:[/red] {e}")
            failed.append(app_id)

    console.print(
        f"\n[bold]Restore summary:[/bold] {len(restored)} restored, {len(skipped)} skipped, {len(failed)} failed"
    )
    if failed:
        raise RestoreRunError(f"{len(failed)} of {len(app_ids)} restore(s) failed ({', '.join(failed)}).")
    return {"restored": restored, "skipped": skipped, "failed": failed}


def restore_backup(
    cfg: CompanionConfig,
    store: str,
    app_id: str,
    backup_file: str,
    *,
    from_remote: Optional[str] = None,
    host: Optional[str] = None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> bool:
    """Restore a single app from a runtipi-companion backup archive.

    This reverses `_archive_app` in runner.py: extracts the app/app-data/
    user-config members from the tar.gz and drops them back into their
    real locations under the runtipi install, replacing whatever is there.

    Host labels only exist on remotes (local disk is inherently one
    machine's): for remote restores, `host` selects which machine's subtree
    to download from (default: this machine's own label). Restoring another
    box's remote backups onto this one is the supported migration path. A
    host-prefixed remote-relative path in `backup_file` wins over `host`.

    Returns True if the app was restored (or would be, in dry-run), False
    if the user declined the confirmation.
    """
    cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=dry_run)

    if from_remote:
        remote = cfg.backup.remote(from_remote)
        if not remote:
            raise ValueError(f"Unknown remote '{from_remote}'")
        # Bare filenames get the full <host>/<store>/<app>/ prefix added;
        # paths (as printed by 'backup list --remote') are used verbatim.
        remote_rel = backup_file
        if "/" not in backup_file:
            remote_rel = f"{host or cfg.host_label}/{store}/{app_id}/{backup_file}"
        local_target = Path(cfg.backup.work_dir) / "restore" / Path(remote_rel).name
        local_target.parent.mkdir(parents=True, exist_ok=True)
        console.print(f"Downloading {remote_rel} from remote '{from_remote}'")
        run(
            ["rclone", "copyto", f"{remote.rclone_remote}/{remote_rel}", str(local_target)],
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
        return False

    was_running = cli.is_app_running(app_id, store) if not dry_run else True
    stopped_by_us = False
    if was_running:
        console.print(f"Stopping {app_id}:{store}")
        try:
            cli.app_stop(f"{app_id}:{store}")
        except CommandError as e:
            # runtipi-cli can exit non-zero for reasons unrelated to whether
            # the containers actually stopped (e.g. a RabbitMQ event-publish
            # failure on an otherwise-healthy stop -- see backup/runner.py's
            # identical handling). Check real container state before
            # trusting the exit code, or a cosmetic error here means we
            # never reach app_start below and leave the app down.
            if dry_run or cli.is_app_running(app_id, store):
                raise
            console.print(
                f"[yellow]runtipi-cli reported an error stopping {app_id}:{store}, but the containers "
                f"are actually stopped -- continuing (will still restart it).[/yellow]\n{e}"
            )
        stopped_by_us = True
        if not dry_run:
            time.sleep(cfg.backup.sleep_duration)

    try:
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
    finally:
        # Always attempt to restart an app we actually stopped, no matter
        # what happened in between -- an app left down after a restore is
        # worse than a failed restore.
        if stopped_by_us:
            console.print(f"Starting {app_id}:{store}")
            cli.app_start(f"{app_id}:{store}")
            if not dry_run:
                time.sleep(cfg.backup.sleep_duration)

    return True
