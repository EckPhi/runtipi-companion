"""Backup package: archive creation/verification (runner), restores,
pure retention logic, and the rclone wrapper."""

from .runner import (
    AppRef,
    BackupVerificationError,
    discover_apps,
    run_backup,
    sync_to_remotes,
    verify_archive,
)

__all__ = [
    "AppRef",
    "BackupVerificationError",
    "discover_apps",
    "run_backup",
    "sync_to_remotes",
    "verify_archive",
]
