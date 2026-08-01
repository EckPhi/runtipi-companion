from __future__ import annotations

import re
import tarfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rich.console import Console

from ..config import CompanionConfig
from ..system.runtipi_cli import RuntipiCLI
from ..system.shell import CommandError
from .app_settings import resolve_app_settings, run_app_command
from .rclone import RcloneClient
from .retention import select_prunable

console = Console()


class BackupVerificationError(RuntimeError):
    pass


class BackupRunError(RuntimeError):
    """One or more apps failed during a backup run. The run continues past
    per-app failures (a broken app must not stop every other app's backup);
    this is raised at the end so the exit code and notifications still
    reflect the failure."""


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


def _exclude_filter(patterns: list):
    """tarfile.add's `filter` callback: drop members whose archived path
    matches any of `patterns` (regex, searched -- not anchored)."""
    if not patterns:
        return None
    compiled = [re.compile(p) for p in patterns]

    def _filter(tarinfo):
        return None if any(p.search(tarinfo.name) for p in compiled) else tarinfo

    return _filter


def _archive_app(
    runtipi_path: str, store: str, app_id: str, dest_file: Path, exclude_patterns: Optional[list] = None
) -> None:
    """Create a tar.gz containing the app's apps/, app-data/, and
    user-config/ directories (if present), same layout as the original
    bash script (app / app-data / user-config top-level members) so
    restore can reverse it symmetrically. `exclude_patterns` (regex,
    matched against the archived path, e.g. "app-data/.../cache/") drops
    matching files/directories from the archive -- see AppBackupConfig.
    """
    app_paths = {
        Path(runtipi_path) / "apps" / store / app_id: "app",
        Path(runtipi_path) / "app-data" / store / app_id: "app-data",
        Path(runtipi_path) / "user-config" / store / app_id: "user-config",
    }
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    tar_filter = _exclude_filter(exclude_patterns or [])
    with tarfile.open(dest_file, "w:gz", dereference=True) as tar:
        for src, arcname in app_paths.items():
            if src.is_dir():
                tar.add(src, arcname=arcname, filter=tar_filter)
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

    local_backup_root = Path(cfg.backup_local_path)
    created_files = []
    date_str = time.strftime("%Y-%m-%d")

    failures = []
    for ref in app_refs:
        try:
            _backup_one_app(
                cfg, cli, ref, schedule, retention, local_backup_root, date_str, stop, dry_run, created_files
            )
        except (CommandError, BackupVerificationError) as e:
            # One broken app (e.g. runtipi-cli failing to stop it) must not
            # cancel every other app's backup. Record, move on, fail at end.
            console.print(f"[red]Backup of {ref.ref} failed:[/red] {e}")
            failures.append((ref.ref, e))

    if not local_only:
        # Sync whatever succeeded -- a partial backup on the remote beats none.
        sync_to_remotes(cfg, schedule, remotes=remotes, dry_run=dry_run)

    if failures:
        failed_refs = ", ".join(ref for ref, _ in failures)
        raise BackupRunError(
            f"{len(failures)} of {len(app_refs)} app backup(s) failed ({failed_refs}); "
            f"{len(created_files)} archive(s) were still created and synced. See output above for details."
        )

    return created_files


def _backup_one_app(
    cfg: CompanionConfig,
    cli: RuntipiCLI,
    ref: AppRef,
    schedule: str,
    retention: int,
    local_backup_root: Path,
    date_str: str,
    stop: bool,
    dry_run: bool,
    created_files: list,
) -> None:
    app_backup_dir = local_backup_root / ref.store / ref.app_id
    app_backup_dir.mkdir(parents=True, exist_ok=True)
    dest_file = app_backup_dir / f"{ref.app_id}-{schedule}-{date_str}.tar.gz"

    # Per-app override (docker labels + config.yaml's backup.app_settings)
    # resolved up front: it decides whether we stop this app at all, and
    # the pre-backup hook needs to run before that decision takes effect.
    settings = resolve_app_settings(cfg, ref.app_id, ref.store)
    effective_stop = stop and not settings.keep_running

    was_running = cli.is_app_running(ref.app_id, ref.store) if not dry_run else True

    if settings.pre_backup_command:
        run_app_command(
            ref.app_id,
            ref.store,
            "pre_backup_command",
            settings.pre_backup_command,
            container_running=was_running,
            dry_run=dry_run,
        )

    stopped_by_us = False
    stop_error = None

    if effective_stop and was_running:
        console.print(f"Stopping {ref.ref}")
        try:
            cli.app_stop(ref.ref)
        except CommandError as e:
            # runtipi-cli can exit non-zero for reasons unrelated to whether
            # the containers actually stopped (seen in the wild: a RabbitMQ
            # event-publish failure on a healthy stop). Check real container
            # state via docker directly instead of trusting the exit code --
            # otherwise a cosmetic CLI error leaves the app stopped forever
            # (we'd bail out here and never reach app_start below).
            if dry_run or cli.is_app_running(ref.app_id, ref.store):
                stop_error = e
            else:
                console.print(
                    f"[yellow]runtipi-cli reported an error stopping {ref.ref}, but the containers "
                    f"are actually stopped -- continuing (will still restart it).[/yellow]\n{e}"
                )
        if stop_error is None:
            stopped_by_us = True
            if not dry_run:
                time.sleep(cfg.backup.sleep_duration)
    elif effective_stop:
        console.print(f"{ref.ref} already stopped")
    elif settings.keep_running and stop:
        console.print(f"Keeping {ref.ref} running during backup (per-app override)")

    try:
        if stop_error is not None:
            raise stop_error

        console.print(f"Archiving {ref.ref} -> {dest_file}")
        verify_error = None
        if not dry_run:
            _archive_app(cfg.runtipi.path, ref.store, ref.app_id, dest_file, settings.exclude_patterns)
            try:
                verify_archive(dest_file)
                created_files.append(dest_file)
            except BackupVerificationError as e:
                # A corrupt archive must not survive (a later prune could
                # delete an older good backup in its favor) and must not
                # fail silently -- but restart the app first (the outer
                # finally below handles that).
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
        else:
            console.print("Deleted the corrupt archive.")
            raise verify_error
    finally:
        # Always attempt to restart an app we actually stopped, no matter
        # what happened in between (archive error, verify failure, ...) --
        # an app left down after a backup run is worse than a failed backup.
        # The restart itself must not be able to skip post_backup_command
        # below (that hook has its own unconditional guarantee to keep, see
        # the comment on it) -- so a restart failure is caught, printed, and
        # re-raised only AFTER post_backup_command has had its chance to run.
        restart_error = None
        if stopped_by_us:
            console.print(f"Starting {ref.ref}")
            try:
                cli.app_start(ref.ref)
                if not dry_run:
                    time.sleep(cfg.backup.sleep_duration)
            except CommandError as e:
                restart_error = e
                console.print(f"[red]Failed to restart {ref.ref}: {e}[/red]")

        if settings.post_backup_command:
            # Always runs, regardless of archive/verify/restart success or
            # failure -- some databases require an unconditional cleanup
            # step here (e.g. QuestDB's CHECKPOINT RELEASE after CHECKPOINT
            # CREATE, which its own docs say must run "regardless of
            # whether the copy operation succeeded or failed"). Re-checked
            # against real container state, not the restart's outcome above.
            container_running = cli.is_app_running(ref.app_id, ref.store) if not dry_run else True
            run_app_command(
                ref.app_id,
                ref.store,
                "post_backup_command",
                settings.post_backup_command,
                container_running=container_running,
                dry_run=dry_run,
            )

        if restart_error is not None:
            raise restart_error


def sync_to_remotes(
    cfg: CompanionConfig,
    schedule: str,
    *,
    remotes: Optional[list] = None,
    dry_run: bool = False,
) -> None:
    rclone = RcloneClient(dry_run=dry_run)
    # Remotes may be shared between machines, so each host syncs its local
    # backups into its own <remote>/<host_label>/ subtree and prunes only
    # there -- other hosts' backups are never touched by this machine's
    # retention policy. Local disk needs no such subfolder.
    local_backup_root = Path(cfg.backup_local_path)

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
            local_backup_root,
            remote_host_root,
            # Only this schedule's archives: a remote must receive exactly the
            # schedules it lists, and local-only pre-update snapshots (or other
            # schedules this remote never prunes) must not leak onto it.
            include=f"*-{schedule}-*.tar.gz",
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
