"""Direct docker helpers for per-app backup settings: resolving a container
(running or stopped), reading its labels, and running a command inside it.

Deliberately independent of RuntipiCLI -- these read/exec docker directly
(no sudo, matching RuntipiCLI.is_app_running's existing docker ps call) so a
runtipi-cli hiccup never blocks label discovery.
"""

from __future__ import annotations

import json
from typing import Optional

from .shell import CommandError, RunResult, run


def container_id(app_id: str, store: str) -> Optional[str]:
    """Docker container ID for an app, running or stopped -- labels and
    exec both need this regardless of current state. None if docker is
    unreachable or the container doesn't exist (yet)."""
    try:
        result = run(
            ["docker", "ps", "-a", "-f", f"name=^/{app_id}_{store}$", "-q"],
            dry_run=False,
            quiet=True,
            check=False,
        )
    except CommandError:
        return None
    ids = result.stdout.strip().splitlines()
    return ids[0] if ids else None


def read_labels(cid: Optional[str]) -> dict:
    """Docker labels on a container, or {} if it can't be inspected (no
    container, docker unreachable, no labels set). Read-only -- safe to
    call even in dry-run or before deciding whether to stop the app."""
    if not cid:
        return {}
    try:
        result = run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", cid],
            dry_run=False,
            quiet=True,
            check=False,
        )
    except CommandError:
        return {}
    if not result.ok or not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout.strip()) or {}
    except ValueError:
        return {}


def exec_in_container(cid: str, command: str, *, dry_run: bool = False) -> RunResult:
    """Run a shell command inside a container via `docker exec`."""
    return run(["docker", "exec", cid, "sh", "-c", command], dry_run=dry_run)
