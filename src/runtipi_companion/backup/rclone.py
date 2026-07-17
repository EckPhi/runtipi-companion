from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..system.shell import run


class RcloneClient:
    """Thin wrapper around the `rclone` CLI.

    We deliberately shell out to the real rclone binary rather than using a
    Python rclone library, since rclone's remotes (configured via
    `rclone config`) already handle auth/credentials for ~70 storage
    backends -- reimplementing that would be reinventing rclone badly.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def sync_dir(
        self,
        local_dir: Path,
        remote: str,
        *,
        bandwidth_limit: Optional[str] = None,
        extra_flags: Optional[list] = None,
    ):
        cmd = ["rclone", "copy", str(local_dir), remote, "--create-empty-src-dirs"]
        if bandwidth_limit:
            cmd += ["--bwlimit", bandwidth_limit]
        if extra_flags:
            cmd += list(extra_flags)
        return run(cmd, dry_run=self.dry_run)

    def list_files(self, remote: str) -> list:
        """Recursively list files under `remote`, relative paths, files only."""
        result = run(["rclone", "lsf", "-R", "--files-only", remote], dry_run=False, quiet=True, check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_dirs(self, remote: str) -> list:
        """Immediate subdirectories of `remote` (no trailing slashes)."""
        result = run(["rclone", "lsf", "--dirs-only", remote], dry_run=False, quiet=True, check=False)
        if result.returncode != 0:
            return []
        return [line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip()]

    def delete_file(self, remote_path: str):
        return run(["rclone", "deletefile", remote_path], dry_run=self.dry_run)

    def list_remotes(self) -> list:
        result = run(["rclone", "listremotes"], dry_run=False, quiet=True, check=False)
        return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]

    def is_installed(self) -> bool:
        result = run(["rclone", "version"], dry_run=False, quiet=True, check=False)
        return result.returncode == 0
