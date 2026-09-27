# Runtipi Companion

Runtipi Companion is a containerized Runtipi backup application. It creates
verified archives of installed app data, keeps independent local and remote
retention policies, and transfers backups through the authenticated Remote
Control API of the Rclone Mount app without a FUSE mount.

The web dashboard shows scheduler status, live per-app backup progress, a
bounded diagnostic event trail, the configured remote, the last run, and
recent local archives. It refreshes while a backup is active and can start
daily, weekly, monthly, or yearly backups on demand. Only one backup runs at a
time.

Installed apps are listed with an individual daily backup action. A separate
read-only backup explorer lists every local archive with its app, store, size,
and creation time; restore and delete actions remain intentionally unavailable.

The configuration page controls automatic schedule enablement and hour, the
rclone target and API endpoint, local and remote copies retained per schedule,
and apps excluded from all-app runs. The endpoint accepts
`http://host:port`, `https://host:port`, or a direct Unix socket such as
`unix:///run/rclone/rc.sock`. These dashboard settings persist under `/config`
and take precedence over their installation-form defaults after the first save.

Dashboard access control is provided by Runtipi. Companion does not maintain a
second set of web credentials.

## Installation

Install the app from the
[Mistborn app store](https://github.com/EckPhi/mistborn-store). Install and
configure its Rclone Mount app first, then provide the rclone API credentials,
endpoint, and target during Companion installation. The store defaults to the
authenticated host socket at `/run/rclone/rc.sock`; no extra nginx proxy is
needed for Companion.

The container mounts the Runtipi installation and Docker socket so it can
consistently stop, archive, verify, and restart applications. Docker socket
access is effectively root access to the host.

## Backup behavior

- Daily, weekly, monthly, and yearly calendar schedules
- Per-schedule local and remote retention
- Archive verification before upload
- Uploaded-size verification before remote pruning
- Per-app keep-running, exclusion, and pre/post-backup command overrides
- Failure isolation so one broken app does not cancel every other backup
- Guaranteed restart and post-backup cleanup paths
- Authenticated rclone API uploads, listings, pruning, and downloads

Restore logic remains part of the application backend. A restore web flow is
intentionally not exposed yet because restoring replaces live application data.

## Host provisioning and management

Host setup, Docker and Runtipi installation, Zsh, Tailscale, rclone setup,
SSH/UFW/fail2ban hardening, diagnostics, and Runtipi update commands live in
[Mistborn Bootstrap](https://github.com/EckPhi/mistborn-bootstrap).

```bash
curl -fsSL https://raw.githubusercontent.com/EckPhi/mistborn-bootstrap/main/dist/server.sh | sudo bash
```

## Development

```bash
uv sync --extra dev
uv run pytest
uvx ruff@0.8.4 check .
uvx ruff@0.8.4 format --check .
```

The release workflow publishes the multi-architecture container image to
`ghcr.io/eckphi/runtipi-companion`.
