"""Pure, testable retention logic shared by local-disk and rclone-remote pruning.

Kept dependency-free (no filesystem or subprocess calls) so it's trivial to
unit test in isolation from real backups/remotes.
"""

from __future__ import annotations

import re
from typing import Optional

BACKUP_NAME_RE = re.compile(
    r"^(?P<app>.+)-(?P<schedule>daily|weekly|monthly|yearly|pre-update)-"
    r"(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<seq>\d+))?\.tar\.gz$"
)


def parse_backup_filename(filename: str) -> Optional[dict]:
    """Parse a backup filename of the form ``<app>-<schedule>-<date>.tar.gz``.

    Returns None if the filename doesn't match the expected pattern.
    """
    m = BACKUP_NAME_RE.match(filename)
    if not m:
        return None
    return m.groupdict()


def select_prunable(filenames: list, app: str, schedule: str, keep: int) -> list:
    """Given filenames in a directory (or remote listing), return the ones
    that should be deleted for a given app/schedule combo, keeping the
    ``keep`` most recent (sorted by the date embedded in the filename, not
    filesystem mtime, so behavior is deterministic and remote-listing safe).
    """
    matches = []
    for name in filenames:
        parsed = parse_backup_filename(name)
        if parsed and parsed["app"] == app and parsed["schedule"] == schedule:
            matches.append((parsed["date"], parsed.get("seq") or "", name))
    matches.sort(reverse=True)  # newest date first
    if keep < 0:
        keep = 0
    prunable = [name for _, _, name in matches[keep:]]
    return prunable
