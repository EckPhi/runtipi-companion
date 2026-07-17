from __future__ import annotations

from typing import Optional

from rich.console import Console

from .backup import discover_apps, run_backup
from .config import CompanionConfig
from .runtipi_cli import RuntipiCLI

console = Console()

# Snapshots taken right before an update: keep the current one plus the
# previous, so a botched update followed by a second (also botched) attempt
# still leaves a restorable state.
PRE_UPDATE_RETENTION = 2


def _pre_update_snapshot(cfg: CompanionConfig, apps: Optional[list], dry_run: bool) -> None:
    """Local-only backup of the apps about to be updated, so every update is
    trivially reversible via `restore run`. Never syncs to remotes (remotes
    only carry schedules they explicitly list, and speed matters here).
    """
    console.print("[bold]Pre-update snapshot[/bold]")
    run_backup(
        cfg,
        "pre-update",
        apps=apps,
        local_only=True,
        retention_override=PRE_UPDATE_RETENTION,
        dry_run=dry_run,
    )


def _should_backup(cfg: CompanionConfig, backup_first: Optional[bool]) -> bool:
    return cfg.updates.backup_before if backup_first is None else backup_first


def update_apps(
    cfg: CompanionConfig,
    *,
    apps: Optional[list] = None,
    dry_run: bool = False,
    backup_first: Optional[bool] = None,
) -> list:
    """Update installed apps via `runtipi-cli app update`, honoring the
    configured (or CLI-provided) allowlist and updates.exclude_apps.
    Returns the list of app refs that were updated.
    """
    if _should_backup(cfg, backup_first):
        _pre_update_snapshot(cfg, apps, dry_run)

    cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=dry_run)
    allowlist = apps if apps else cfg.runtipi.apps
    refs = discover_apps(cfg.runtipi.path, allowlist)
    excluded = set(cfg.updates.exclude_apps)

    updated = []
    for ref in refs:
        if ref.app_id in excluded:
            console.print(f"Skipping excluded app {ref.ref}")
            continue
        console.print(f"Updating {ref.ref}")
        cli.app_update(ref.ref)
        updated.append(ref.ref)
    return updated


def update_core(
    cfg: CompanionConfig,
    version: str = "latest",
    *,
    dry_run: bool = False,
    backup_first: Optional[bool] = None,
) -> None:
    if _should_backup(cfg, backup_first):
        _pre_update_snapshot(cfg, None, dry_run)

    cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=dry_run)
    console.print(f"Updating runtipi core to {version}")
    if version != "latest":
        console.print(
            "[yellow]Downgrading or pinning versions can break your installation -- "
            "make sure you have a recent backup first.[/yellow]"
        )
    cli.update_core(version)


def update_appstores(cfg: CompanionConfig, *, dry_run: bool = False) -> None:
    cli = RuntipiCLI(cfg.runtipi.path, cfg.runtipi.cli_path, dry_run=dry_run)
    console.print("Updating app stores")
    cli.appstore_update()
