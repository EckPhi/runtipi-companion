from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

from rich.console import Console

console = Console()


class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Command failed ({returncode}): {' '.join(cmd)}\n{stderr}")


@dataclass
class RunResult:
    cmd: list
    returncode: int
    stdout: str
    stderr: str
    dry_run: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    cmd: Sequence[str],
    *,
    dry_run: bool = False,
    check: bool = True,
    sudo: bool = False,
    cwd: Optional[str] = None,
    input: Optional[str] = None,
    quiet: bool = False,
) -> RunResult:
    """Run a shell command, honoring dry-run mode.

    In dry-run mode the command is printed and never executed, and a
    successful no-op RunResult is returned so callers can chain logic
    without special-casing dry-run everywhere.
    """
    full_cmd = list(cmd)
    if sudo and full_cmd[0] != "sudo":
        full_cmd = ["sudo"] + full_cmd

    printable = " ".join(shlex.quote(part) for part in full_cmd)

    if dry_run:
        console.print(f"[yellow]DRY-RUN[/yellow] $ {printable}")
        return RunResult(cmd=full_cmd, returncode=0, stdout="", stderr="", dry_run=True)

    if not quiet:
        console.print(f"[dim]$ {printable}[/dim]")

    try:
        proc = subprocess.run(
            full_cmd,
            cwd=cwd,
            input=input,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        # Missing binary (docker, rclone, tailscale, ...) shouldn't produce a
        # raw Python traceback -- surface it the same way a failed command
        # would, so callers only need to handle one error type.
        raise CommandError(full_cmd, 127, f"{full_cmd[0]}: command not found ({e})") from e

    if check and proc.returncode != 0:
        raise CommandError(full_cmd, proc.returncode, proc.stderr)

    return RunResult(
        cmd=full_cmd,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        dry_run=False,
    )


def confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
