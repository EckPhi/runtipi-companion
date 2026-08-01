"""Per-app backup settings: merges docker labels on the app's container
(base layer, useful for zero-config discovery across many apps) with the
config file's backup.app_settings.<app_id> section (override layer, wins
per-field when both are set) into one resolved, typed settings object.

Use case this exists for: a database app (e.g. QuestDB) that supports a
zero-downtime backup via its own snapshot mechanism -- keep_running=true
plus a pre_backup_command that triggers the snapshot (run via `docker exec`
into the app's own container) instead of stopping it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console

from ..config import CompanionConfig
from ..config.schema import AppBackupConfig
from ..system.docker import container_id, exec_in_container, read_labels
from ..system.shell import CommandError

console = Console()

# Docker label namespace read for per-app backup settings, e.g.
#   runtipi-companion.backup.keep-running: "true"
#   runtipi-companion.backup.exclude: "\\.log$,/tmp/"
#   runtipi-companion.backup.pre-command: "curl -G ... query=CHECKPOINT+CREATE"
#   runtipi-companion.backup.post-command: "curl -G ... query=CHECKPOINT+RELEASE"
#   runtipi-companion.backup.restore-command: "..."
LABEL_PREFIX = "runtipi-companion.backup."


@dataclass
class ResolvedAppSettings:
    keep_running: bool = False
    exclude_patterns: list = field(default_factory=list)
    pre_backup_command: Optional[str] = None
    post_backup_command: Optional[str] = None
    restore_command: Optional[str] = None


def _bool_label(value: Optional[str]) -> Optional[bool]:
    return None if value is None else value.strip().lower() in ("true", "1", "yes")


def labels_to_overrides(labels: dict) -> AppBackupConfig:
    """Parse the runtipi-companion.backup.* docker labels on a container
    into the same override shape as the config file section, so both can
    be merged the same way."""
    exclude = labels.get(f"{LABEL_PREFIX}exclude")
    return AppBackupConfig(
        keep_running=_bool_label(labels.get(f"{LABEL_PREFIX}keep-running")),
        exclude_patterns=[p.strip() for p in exclude.split(",") if p.strip()] if exclude else None,
        pre_backup_command=labels.get(f"{LABEL_PREFIX}pre-command"),
        post_backup_command=labels.get(f"{LABEL_PREFIX}post-command"),
        restore_command=labels.get(f"{LABEL_PREFIX}restore-command"),
    )


def merge_overrides(base: AppBackupConfig, override: AppBackupConfig) -> AppBackupConfig:
    """Field-by-field merge: `override`'s value wins wherever it isn't
    None, otherwise `base`'s value is kept."""
    return AppBackupConfig(
        keep_running=override.keep_running if override.keep_running is not None else base.keep_running,
        exclude_patterns=override.exclude_patterns if override.exclude_patterns is not None else base.exclude_patterns,
        pre_backup_command=(
            override.pre_backup_command if override.pre_backup_command is not None else base.pre_backup_command
        ),
        post_backup_command=(
            override.post_backup_command if override.post_backup_command is not None else base.post_backup_command
        ),
        restore_command=override.restore_command if override.restore_command is not None else base.restore_command,
    )


def resolve_app_settings(cfg: CompanionConfig, app_id: str, store: str) -> ResolvedAppSettings:
    """Docker labels (base layer) merged with config.yaml's
    backup.app_settings.<app_id> (override layer, wins per-field) into
    concrete settings. A missing container or unreadable labels are
    silently treated as "no label overrides" -- this must never fail a
    backup/restore over a docker inspect hiccup.
    """
    label_overrides = labels_to_overrides(read_labels(container_id(app_id, store)))
    config_overrides = cfg.backup.app_settings.get(app_id, AppBackupConfig())
    merged = merge_overrides(label_overrides, config_overrides)
    return ResolvedAppSettings(
        keep_running=merged.keep_running if merged.keep_running is not None else False,
        exclude_patterns=merged.exclude_patterns if merged.exclude_patterns is not None else [],
        pre_backup_command=merged.pre_backup_command,
        post_backup_command=merged.post_backup_command,
        restore_command=merged.restore_command,
    )


def run_app_command(
    app_id: str, store: str, kind: str, command: str, *, container_running: bool, dry_run: bool
) -> None:
    """Execute a configured pre_backup_command/post_backup_command/
    restore_command inside the app's container via `docker exec`. Raises
    CommandError (isolated as a per-app failure by the caller, same as any
    other command failure) if the container isn't running -- there's
    nothing to exec into -- or can't be resolved at all, rather than
    silently skipping a command the user explicitly configured.
    """
    label = f"{app_id}:{store}"
    if not container_running:
        raise CommandError(
            ["docker", "exec"],
            1,
            f"{label}: {kind} is configured but the app isn't running -- nothing to exec into.",
        )
    cid = container_id(app_id, store)
    if not cid:
        raise CommandError(["docker", "exec"], 1, f"{label}: could not resolve a container to run {kind} in.")
    console.print(f"Running {kind} in {label}: {command}")
    exec_in_container(cid, command, dry_run=dry_run)
