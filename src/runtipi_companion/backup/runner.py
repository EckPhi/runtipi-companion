from __future__ import annotations

import tarfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

from ..config import CompanionConfig
from ..system.runtipi_cli import RuntipiCLI
from .rclone import RcloneClient
from .retention import select_prunable

console = Console()


class BackupVerificationError(RuntimeError):
    pass


def verify_archive(path: Path) -> None:
    """Read every member of the archive back in full. gzip CRCs are only
    checked on read, so a truncated or bit-flipped archive fails here
    instead of at restore time. Raises BackupVerificationError.
    """
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                while extracted.read(1 << 20):
                    pass
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise BackupVerificationError(f"Archive failed verification: {path} ({e})") from e


@dataclass
class AppRef:
    store: str
    app_id: str

    @property
    def ref(self) -> str:
        return f"{self.app_id}:{self.store}"


def discover_apps(runtipi_path: str, allowlist: Optional[list] = None) -> list:
    """Walk <runtipi_path>/apps/<store>/<app-id> to find installed apps.

    Mirrors the "for appStore in apps; for app in appStore" loop from the
    original bash auto-backup script, but returns structured refs instead
    of shelling out to `ls` twice.
    """
    apps_dir = Path(runtipi_path) / "apps"
    if not apps_dir.is_dir():
        raise RuntimeError(f"Apps directory not found: {apps_dir}")
    refs = []
    for store_dir in sorted(apps_dir.iterdir()):
        if not store_dir.is_dir():
            continue
        for app_dir in sorted(store_dir.iterdir()):
            if not app_dir.is_dir():
                continue
            if allowlist and app_dir.name not in allowlist:
                continue
            refs.append(AppRef(store=store_dir.name, app_id=app_dir.name))
    return refs


def _archive_app(runtipi_path: str, store: str, app_id: str, dest_file: Path) -> None:
    """Create a tar.gz containing the app's apps/, app-data/, and
    user-config/ directories (if present), same layout as the original
    bash script (app / app-data / user-config top-level members) so
    restore can reverse it symmetrically.
    """
    app_paths = {
        Path(runtipi_path) / "apps" / store / app_id: "app",
        Path(runtipi_path) / "app-data" / store / app_id: "app-data",
        Path(runtipi_path) / "user-config" / store / app_id: "user-config",
    }
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest_file, "w:gz", dereference=True) as tar:
        for src, arcname in app_paths.items():
            if src.is_dir():
                tar.add(src, arcname=arcname)
            elif arcname == "user-config":
                pass  # user-config is optional, most apps don't have one
            else:
                console.print(f"[dim]  {arcname} directory missing for {app_id}, skipped[/dim]")


def run_backup(
    cfg: CompanionConfig,
    schedule: str,
    *,
    apps: Optional[list] = None,
    stop_apps: Optional[bool] = None,
    remotes: Optional[list] = None,
    local_only: bool = False,
    dry_run: bool = False,
    retention_override: Optional[int] = None,
) -> list:
    """Back up every matched app for `schedule`, prune local retention, then
    sync + prune each enabled remote that has a retention configured for
    this schedule. Returns the list of archive paths created.

    `retention_override` also allows schedules outside the configured ones
    (used for the ad-hoc "pre-update" snapshots).
    """
    if retention_override is not None:
        retention = retention_override
    elif schedule in cfg.backup.schedules:
        retention = cfg.backup.schedules[schedule].retention
    else:
        raise ValueError(
            f"No retention configured for schedule '{schedule}'. " f"Configured schedules: {list(cfg.backup.schedules)}"
        )
    stop = cfg.backup.stop_apps if stop_apps is None else stop_apps

    cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=dry_run)
    allowlist = apps if apps else cfg.runtipi.apps
    app_refs = discover_apps(cfg.runtipi.path, allowlist)
    if not app_refs:
        console.print("[yellow]No apps matched, nothing to back up.[/yellow]")
        return []

    # Per-host subfolder so several machines can share one backup location
    # (local NAS mount or remote bucket) without mixing archives.
    host_backup_root = Path(cfg.backup_local_path) / cfg.host_label
    created_files = []
    date_str = time.strftime("%Y-%m-%d")

    for ref in app_refs:
        app_backup_dir = host_backup_root / ref.store / ref.app_id
        app_backup_dir.mkdir(parents=True, exist_ok=True)
        dest_file = app_backup_dir / f"{ref.app_id}-{schedule}-{date_str}.tar.gz"

        was_running = cli.is_app_running(ref.app_id, ref.store) if not dry_run else True
        if stop:
            if was_running:
                console.print(f"Stopping {ref.ref}")
                cli.app_stop(ref.ref)
                if not dry_run:
                    time.sleep(cfg.backup.sleep_duration)
            else:
                console.print(f"{ref.ref} already stopped")

        console.print(f"Archiving {ref.ref} -> {dest_file}")
        verify_error = None
        if not dry_run:
            _archive_app(cfg.runtipi.path, ref.store, ref.app_id, dest_file)
            try:
                verify_archive(dest_file)
                created_files.append(dest_file)
            except BackupVerificationError as e:
                # A corrupt archive must not survive (a later prune could
                # delete an older good backup in its favor) and must not
                # fail silently -- but restart the app first.
                dest_file.unlink(missing_ok=True)
                verify_error = e
        else:
            console.print(f"[yellow]DRY-RUN[/yellow] would create and verify {dest_file}")

        if verify_error is None:
            # Local retention: keep the `retention` most recent archives for
            # this app+schedule, delete the rest.
            existing = [p.name for p in app_backup_dir.glob(f"{ref.app_id}-{schedule}-*.tar.gz")]
            prunable = select_prunable(existing, ref.app_id, schedule, retention)
            for name in prunable:
                target = app_backup_dir / name
                console.print(f"Pruning old local backup {target}")
                if not dry_run:
                    target.unlink(missing_ok=True)

        if stop and was_running:
            console.print(f"Starting {ref.ref}")
            cli.app_start(ref.ref)
            if not dry_run:
                time.sleep(cfg.backup.sleep_duration)

        if verify_error is not None:
            console.print(f"[red]{verify_error}[/red] Deleted the corrupt archive.")
            raise verify_error

    if not local_only:
        sync_to_remotes(cfg, schedule, remotes=remotes, dry_run=dry_run)

    return created_files


def sync_to_remotes(
    cfg: CompanionConfig,
    schedule: str,
    *,
    remotes: Optional[list] = None,
    dry_run: bool = False,
) -> None:
    rclone = RcloneClient(dry_run=dry_run)
    # Sync and prune only this host's subtree: other hosts backing up to the
    # same remote must never be touched by this machine's retention policy.
    host_backup_root = Path(cfg.backup_local_path) / cfg.host_label

    for remote in cfg.backup.remotes:
        if not remote.enabled:
            continue
        if remotes and remote.name not in remotes:
            continue
        remote_retention = remote.retention_for(schedule)
        if remote_retention is None:
            continue  # this remote isn't configured to keep this schedule

        remote_host_root = f"{remote.rclone_remote}/{cfg.host_label}"
        console.print(f"[bold]Syncing schedule '{schedule}' to remote '{remote.name}'[/bold]")
        rclone.sync_dir(
            host_backup_root,
            remote_host_root,
            bandwidth_limit=remote.bandwidth_limit,
            extra_flags=remote.extra_rclone_flags,
        )
        if not dry_run:
            prune_remote(rclone, remote_host_root, schedule, remote_retention)
        else:
            console.print(
                f"[yellow]DRY-RUN[/yellow] would prune remote '{remote.name}' to {remote_retention} {schedule} backups per app"
            )


def prune_remote(rclone: RcloneClient, remote_root: str, schedule: str, retention: int) -> None:
    """Prune per app+schedule under `remote_root` (an rclone path already
    scoped to one host's subtree)."""
    files_by_dir = {}
    for path in rclone.list_files(remote_root):
        directory = str(Path(path).parent)
        files_by_dir.setdefault(directory, []).append(Path(path).name)

    for directory, names in files_by_dir.items():
        apps_in_dir = {n.split(f"-{schedule}-")[0] for n in names if f"-{schedule}-" in n}
        for app in apps_in_dir:
            prunable = select_prunable(names, app, schedule, retention)
            for name in prunable:
                remote_path = f"{remote_root}/{name}" if directory in (".", "") else f"{remote_root}/{directory}/{name}"
                console.print(f"Pruning old remote backup {remote_path}")
                rclone.delete_file(remote_path)
