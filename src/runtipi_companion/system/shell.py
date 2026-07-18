from __future__ import annotations

import shlex
import subprocess
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.text import Text

console = Console()

# How many trailing output lines the live tail shows while a command runs.
STREAM_TAIL_LINES = 8


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


def _should_stream(*, quiet: bool, interactive: bool, input: Optional[str]) -> bool:
    """Stream-and-collapse only makes sense on a real terminal, for display
    commands (quiet callers parse stdout instead) that need no stdin."""
    return not quiet and not interactive and input is None and console.is_terminal


def _stream(full_cmd: list, cwd: Optional[str]) -> tuple:
    """Run showing a live tail of output that collapses when the command
    exits. stderr is merged into stdout so the tail (and any failure
    report) shows everything in order. Returns (returncode, merged_output).
    """
    proc = subprocess.Popen(
        full_cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines = []
    tail = deque(maxlen=STREAM_TAIL_LINES)
    # transient=True erases the tail once the command is done -- success
    # leaves only the dim "$ cmd" line behind; failures re-surface the full
    # output through CommandError.
    with Live(console=console, transient=True, refresh_per_second=8) as live:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            tail.append(line.rstrip())
            live.update(Text("\n".join(f"  {t}" for t in tail), style="dim"))
        proc.wait()
    return proc.returncode, "".join(lines)


def run(
    cmd: Sequence[str],
    *,
    dry_run: bool = False,
    check: bool = True,
    sudo: bool = False,
    cwd: Optional[str] = None,
    input: Optional[str] = None,
    quiet: bool = False,
    interactive: bool = False,
) -> RunResult:
    """Run a shell command, honoring dry-run mode.

    In dry-run mode the command is printed and never executed, and a
    successful no-op RunResult is returned so callers can chain logic
    without special-casing dry-run everywhere.

    Output handling:
    - default on a terminal: live tail of the child's output while it runs,
      collapsed once it exits -- success leaves just the "$ cmd" line,
      failure carries the full merged output in CommandError/RunResult.
    - `quiet` captures silently (for callers that parse stdout).
    - `interactive` hands the terminal to the child (stdin/stdout/stderr
      inherited, nothing captured) -- for commands that prompt or must be
      seen live even after success, e.g. `tailscale up` printing its login
      URL, installer scripts, or status commands. RunResult then has empty
      stdout/stderr.
    - off-terminal (cron, systemd, pipes) falls back to plain capture so
      logs stay clean.
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
        if interactive:
            proc = subprocess.run(full_cmd, cwd=cwd)
            returncode, stdout, stderr = proc.returncode, "", ""
        elif _should_stream(quiet=quiet, interactive=interactive, input=input):
            returncode, merged = _stream(full_cmd, cwd)
            # stderr was merged into the stream; expose the merged text on
            # both fields so failure paths that print .stderr still work.
            stdout, stderr = merged, merged if returncode != 0 else ""
        else:
            proc = subprocess.run(
                full_cmd,
                cwd=cwd,
                input=input,
                capture_output=True,
                text=True,
            )
            returncode, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError as e:
        # Missing binary (docker, rclone, tailscale, ...) shouldn't produce a
        # raw Python traceback -- surface it the same way a failed command
        # would, so callers only need to handle one error type.
        raise CommandError(full_cmd, 127, f"{full_cmd[0]}: command not found ({e})") from e

    if check and returncode != 0:
        # Interactive children already wrote their errors to the terminal.
        raise CommandError(full_cmd, returncode, stderr)

    return RunResult(
        cmd=full_cmd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
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
