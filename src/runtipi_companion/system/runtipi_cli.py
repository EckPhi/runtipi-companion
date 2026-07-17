from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from .shell import CommandError, RunResult, console, run


class RuntipiCLIError(RuntimeError):
    pass


class RuntipiCLI:
    """Thin wrapper around Runtipi's ``runtipi-cli``.

    Note that ``runtipi-cli`` is NOT added to $PATH by a default install, so
    we resolve its location once at construction time: an explicit
    ``cli_path`` from config wins, otherwise we look in the usual spots
    under the runtipi install directory, and finally fall back to $PATH.

    We shell out to the real CLI for every action instead of reimplementing
    it, so we automatically stay in sync with whatever Runtipi itself
    supports (start/stop/update/app management/appstore management).
    """

    def __init__(self, runtipi_path: str, cli_path: Optional[str] = None, dry_run: bool = False):
        self.runtipi_path = runtipi_path
        self.dry_run = dry_run
        self.cli_path = cli_path or self._resolve_cli_path()

    def _resolve_cli_path(self) -> str:
        candidates = [
            Path(self.runtipi_path) / "runtipi-cli",
            Path(self.runtipi_path) / "scripts" / "runtipi-cli",
        ]
        for c in candidates:
            if c.exists() and os.access(c, os.X_OK):
                return str(c)
        found = shutil.which("runtipi-cli")
        if found:
            return found
        raise RuntipiCLIError(
            "Could not find runtipi-cli. It is not added to $PATH by default. "
            f"Looked in {', '.join(str(c) for c in candidates)} and $PATH. "
            "Set `runtipi.cli_path` in your config to its absolute path."
        )

    def _run(self, args: list, **kwargs) -> RunResult:
        return run([self.cli_path, *args], sudo=True, dry_run=self.dry_run, **kwargs)

    # --- core ---

    def start(self) -> RunResult:
        return self._run(["start"])

    def stop(self) -> RunResult:
        return self._run(["stop"])

    def restart(self) -> RunResult:
        return self._run(["restart"])

    def prepare(self) -> RunResult:
        return self._run(["prepare"])

    def update_core(self, version: str = "latest") -> RunResult:
        return self._run(["update", version])

    def version(self) -> str:
        result = run([self.cli_path, "version"], dry_run=False, quiet=True, check=False)
        return result.stdout.strip()

    def installed(self) -> list:
        result = run([self.cli_path, "installed"], dry_run=False, quiet=True, check=False)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def debug(self) -> str:
        result = run([self.cli_path, "debug"], dry_run=False, quiet=True, check=False)
        return result.stdout

    # --- app management ---

    def app_start(self, app_ref: str) -> RunResult:
        return self._run(["app", "start", app_ref])

    def app_stop(self, app_ref: str) -> RunResult:
        return self._run(["app", "stop", app_ref])

    def app_start_all(self) -> RunResult:
        return self._run(["app", "start-all"])

    def app_update(self, app_ref: str) -> RunResult:
        return self._run(["app", "update", app_ref])

    def app_uninstall(self, app_ref: str) -> RunResult:
        return self._run(["app", "uninstall", app_ref])

    # Native single-shot backup/restore built into runtipi-cli itself (v4+).
    # runtipi-companion's own backup/restore commands are richer (multi
    # schedule, retention, rclone remotes) and don't depend on these, but
    # they're exposed here for completeness / scripting convenience.
    def app_backup(self, app_ref: str) -> RunResult:
        return self._run(["app", "backup", app_ref])

    def app_restore(self, app_ref: str, backup_filename: str) -> RunResult:
        return self._run(["app", "restore", app_ref, backup_filename])

    def app_list_backups(self, app_ref: str) -> list:
        result = run(
            [self.cli_path, "app", "list-backups", app_ref], sudo=True, dry_run=False, quiet=True, check=False
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def app_delete_backup(self, app_ref: str, backup_filename: str) -> RunResult:
        return self._run(["app", "delete-backup", app_ref, backup_filename])

    # --- appstore management ---

    def appstore_list(self) -> str:
        result = run([self.cli_path, "appstore", "list"], dry_run=False, quiet=True, check=False)
        return result.stdout

    def appstore_update(self) -> RunResult:
        return self._run(["appstore", "update"])

    # --- helpers ---

    def is_app_running(self, app_id: str, store: str) -> bool:
        try:
            result = run(
                ["docker", "ps", "-f", f"name=^/{app_id}_{store}", "-q"],
                dry_run=False,
                quiet=True,
                check=False,
            )
        except CommandError:
            # docker missing/unreachable -- don't crash the whole backup run
            # over a status check; assume stopped so we don't try to
            # start/stop something we can't see.
            console.print(
                f"[yellow]Could not query docker for {app_id}:{store} status, assuming stopped.[/yellow]"
            )
            return False
        return bool(result.stdout.strip())
