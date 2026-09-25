# Agent notes for runtipi-companion

This repository is exclusively the containerized Runtipi Companion app. Host
provisioning, shell setup, Tailscale, rclone configuration, security hardening,
diagnostics, and Runtipi update orchestration belong in
`EckPhi/mistborn-bootstrap`, not here.

## Package layout

- `backup/` — backup runner, retention, restore, rclone RC client, and per-app
  settings.
- `config/` — the app's YAML schema and loader.
- `system/` — shell execution, Docker inspection, notifications, and the
  Runtipi CLI wrapper used by backup operations.
- `container.py` — managed config rendering, scheduler, and authenticated web
  dashboard.

## Shell execution is load-bearing

All subprocesses go through `system/shell.py::run()`. Parsing callers use
`quiet=True`; interactive commands use `interactive=True`. Do not introduce
raw `subprocess` calls or bypass its error handling.

The container reaches the host through the Docker socket and mounted Runtipi
directory. Docker reads intentionally do not use sudo.

## Backup safety invariants

- A failure backing up one app must not prevent the remaining apps from being
  attempted. Collect failures, sync successful archives, then report the
  aggregate failure.
- A failed `runtipi-cli app stop` does not prove that containers remain
  running. Recheck with Docker before deciding whether it is safe to archive.
- If Companion stopped an app, its restart belongs in a `finally` block so it
  happens after every archive or verification outcome.
- Run `pre_backup_command` while the app is still running, before the stop
  decision.
- Run `post_backup_command` unconditionally in the same cleanup path, after
  restarting an app that Companion stopped.
- Run `restore_command` after the file restore and after ensuring the app is
  running. A configured hook that cannot execute must fail loudly.

## Per-app settings

Every `AppBackupConfig` override remains optional with a `None` default.
Configuration wins per field, Docker labels fill unset fields, and hard-coded
defaults apply last. A concrete falsy dataclass default breaks this merge.

Verify third-party backup examples against the product's current official
documentation before adding them.

## Configuration compatibility

The managed app config is version 4. The legacy `security`, `tailscale`, and
`updates` keys are parsed only so files created by the former standalone CLI
continue to load; the app never acts on them. Do not add host-management
behavior back to these fields.

Any app config shape change must update the schema, managed config rendering,
and tests together. A config newer than the supported version must fail rather
than be guessed at.

## Rclone paths and transport

- Strip trailing slashes from remote targets before appending paths.
- Local backups remain flat under `<local>/<store>/<app>`; only remote backups
  gain the `<host_label>` prefix.
- Remote syncs include only archives for the active schedule.
- Container operation uses the authenticated rclone Remote Control API. Do not
  require a second mount or a local rclone configuration.

## Web app invariants

- Dashboard access control belongs to Runtipi; do not add a second authentication layer.
- State-changing routes require CSRF validation.
- Only one backup may run at a time.
- Never expose a restore action casually: restore replaces live app data and
  needs a deliberate confirmation design.

## Verification

Before committing, run:

```bash
uvx ruff@0.8.4 check --fix .
uvx ruff@0.8.4 format .
uv run --isolated --frozen --extra dev pytest
docker build --build-arg APP_VERSION=0.0.0 -t runtipi-companion:test .
```

Reproduce bugs reported from a real Runtipi host with focused stubs or fixtures
instead of relying only on a visually plausible fix.
