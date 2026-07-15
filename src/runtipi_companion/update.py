from __future__ import annotations

from typing import Optional

from rich.console import Console

from .backup import discover_apps
from .config import CompanionConfig
from .runtipi_cli import RuntipiCLI

console = Console()


def update_apps(cfg: CompanionConfig, *, apps: Optional[list] = None, dry_run: bool = False) -> list:
    """Update installed apps via `runtipi-cli app update`, honoring the
    configured (or CLI-provided) allowlist and updates.exclude_apps.
    Returns the list of app refs that were updated.
    """
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


def update_core(cfg: CompanionConfig, version: str = "latest", *, dry_run: bool = False) -> None:
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
