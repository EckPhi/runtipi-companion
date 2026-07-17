"""Version-based config migrations.

Each step transforms the raw YAML dict from version N to N+1. Steps must be
pure dict-to-dict (no I/O) so they compose and unit-test trivially;
migrate_file() handles reading, backing up, writing, and validating.
"""

from __future__ import annotations

import difflib
from copy import deepcopy
from pathlib import Path

import yaml
from rich.console import Console

from .schema import CONFIG_VERSION, ConfigError

console = Console()


def _migrate_1_to_2(raw: dict) -> dict:
    """v2 introduced apprise notifications (notify.urls), pre-update
    snapshots (updates.backup_before), and the per-remote host subfolder
    (backup.host_label). All additive -- make them explicit so the file
    documents its own knobs."""
    notify = raw.get("notify") or {}
    raw["notify"] = notify
    notify.setdefault("urls", [])

    updates = raw.get("updates") or {}
    raw["updates"] = updates
    updates.setdefault("backup_before", True)

    backup = raw.get("backup") or {}
    raw["backup"] = backup
    backup.setdefault("host_label", None)
    return raw


# version N -> the step that produces N+1
MIGRATIONS = {
    1: _migrate_1_to_2,
}


def migrate(raw: dict) -> tuple:
    """Apply every migration step from the dict's version up to
    CONFIG_VERSION. Returns (migrated_dict, [step descriptions])."""
    raw = deepcopy(raw)
    version = raw.get("version", 1)
    if version > CONFIG_VERSION:
        raise ConfigError(
            f"Config is version {version}, but this runtipi-companion only knows version {CONFIG_VERSION}. "
            f"Upgrade runtipi-companion ('self-update') instead of migrating the config down."
        )
    applied = []
    while version < CONFIG_VERSION:
        raw = MIGRATIONS[version](raw)
        applied.append(f"v{version} -> v{version + 1}")
        version += 1
    raw["version"] = CONFIG_VERSION
    return raw, applied


def migrate_file(path: Path, *, dry_run: bool = True) -> bool:
    """Migrate a config file in place. Dry-run prints the steps and a diff;
    apply backs the original up next to it first. Hand-written comments are
    lost on rewrite (yaml round-trip), same trade-off as 'backup remotes'.
    Returns True if the file is (now) at the current version."""
    original_text = path.read_text()
    raw = yaml.safe_load(original_text) or {}
    old_version = raw.get("version", 1)
    if old_version == CONFIG_VERSION:
        console.print(f"[green]{path} is already at config version {CONFIG_VERSION}.[/green]")
        return True

    migrated, applied = migrate(raw)
    header = f"# runtipi-companion configuration (migrated to version {CONFIG_VERSION})\n\n"
    new_text = header + yaml.safe_dump(migrated, sort_keys=False, default_flow_style=False)

    console.print(f"Migration steps: {', '.join(applied)}")
    diff = difflib.unified_diff(
        original_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"{path} (v{old_version})",
        tofile=f"{path} (v{CONFIG_VERSION})",
    )
    console.print("".join(diff), soft_wrap=True, highlight=False)

    if dry_run:
        console.print("[yellow]DRY-RUN[/yellow] -- nothing written. Re-run with --apply to migrate.")
        return False

    backup_path = path.with_name(path.name + f".bak-v{old_version}")
    backup_path.write_text(original_text)
    path.write_text(new_text)
    # Never leave a config behind that the CLI can't read.
    from .loader import load_config

    try:
        load_config(str(path))
    except ConfigError as e:
        path.write_text(original_text)
        console.print(f"[red]Migrated config failed validation, original restored: {e}[/red]")
        return False
    console.print(f"[green]Migrated {path} to version {CONFIG_VERSION}.[/green] Original saved as {backup_path}.")
    return True
