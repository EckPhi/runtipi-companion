# runtipi-companion

A single Python CLI that extends and replaces the shell scripts scattered
across Runtipi's docs (auto-backup, VPS security, server hardening) with one
tool: `runtipi-companion`.

Built on top of, not instead of, `runtipi-cli` -- every action that
Runtipi's own CLI already does well (start/stop, app update, appstore
update) is a thin wrapper around it. What this tool adds:

- **Backups to multiple rclone remotes, each with its own retention.** The
  official [auto-backup-apps](https://runtipi.io/docs/guides/auto-backup-apps)
  script only writes to local disk with one shared retention policy. This
  keeps that local behavior but adds any number of rclone remotes (B2, S3,
  Google Drive, SFTP, ...), each syncing independently with its own
  daily/weekly/monthly/yearly retention.
- **Restore** from local disk or any configured remote, symmetric with the
  backup format. Every archive is verified (fully read back) right after
  creation, so a corrupt backup fails the backup run instead of surfacing
  at restore time.
- **Updates** for individual apps, all apps (with an exclude list), app
  stores, and Runtipi core itself. By default a local pre-update snapshot
  of the affected apps is taken first (`updates.backup_before`), so every
  update is reversible via `restore run`.
- **A config wizard** that runs on first start (or via `config wizard`):
  interviews you in the terminal and writes a validated config file.
- **A setup wizard** for a fresh box: clone/verify the Runtipi install,
  locate `runtipi-cli` (it isn't on `$PATH` by default), run
  `prepare`/`start`, create backup directories, sanity-check rclone remotes.
- **Security hardening** that applies the checklists from
  [VPS security](https://runtipi.io/docs/security/vps-security) and
  [Server hardening](https://runtipi.io/docs/security/server-hardening):
  SSH key-only + no root login, UFW, fail2ban, and an optional
  tailscale-only lockdown (see below). Pick what to apply from an
  interactive menu, or target items individually with flags.
- **Tailscale install + `up`**, matching the VPN-only access pattern the VPS
  security guide recommends (don't open 80/443 publicly). `security harden
  --tailscale-security` goes further, based on
  [this UFW+tailscale writeup](https://dev.to/binsarjr/turn-your-vps-into-an-impenetrable-fortress-how-to-make-your-public-server-private-using-tailscale-and-ufw-3841):
  it switches SSH to `tailscale ssh` and restricts UFW to allow inbound
  traffic only on the `tailscale0` interface, so the box is unreachable
  from the public internet entirely (only tailscale's own coordination
  port stays open).
- One global YAML config for all of the above.

## Safety model

Every command that changes system state (backups stop/start containers,
security hardening edits `sshd_config`/firewall/installs packages, restore
overwrites app data) **defaults to `--dry-run`**, which only prints what it
would do. Add `--apply` to actually execute. SSH hardening additionally
refuses to disable password authentication unless it finds an existing
`authorized_keys` for root/the current user/`$SUDO_USER` (override with
`--force` if you know what you're doing), and validates `sshd_config` with
`sshd -t` before restarting -- rolling back automatically if the config is
broken, so you can't lock yourself out of a remote box.

## Install

From [PyPI](https://pypi.org/project/runtipi-companion/):

```
pipx install runtipi-companion
```

(Or without pipx:)

```
python3 -m venv /opt/runtipi-companion-venv
/opt/runtipi-companion-venv/bin/pip install /path/to/runtipi-companion
sudo ln -s /opt/runtipi-companion-venv/bin/runtipi-companion /usr/local/bin/runtipi-companion
```

Or, for local development:

```
pip install -e ".[dev]"
pre-commit install      # ruff lint + format on every commit
pytest
```

The end-to-end suite (`tests/e2e/e2e.sh`) exercises real `rclone`/`tailscale`
binaries and applies changes for real, so run it in the provided Docker
image rather than on your own machine:

```
docker build -f tests/e2e/Dockerfile -t runtipi-companion-e2e .
docker run --rm runtipi-companion-e2e
```

Requires `rclone` on `$PATH` for remote backups, and `tailscale`'s installer
handles its own binary. `runtipi-cli` does not need to be on `$PATH` --
runtipi-companion looks for it at `<runtipi.path>/runtipi-cli` automatically
(set `runtipi.cli_path` in the config if yours lives elsewhere).

Managing several boxes (or provisioning unattended)? There's a thin Ansible
playbook in [`deploy/ansible/`](./deploy/ansible/) that installs the package,
uploads a config, and runs the setup/hardening steps non-interactively.

## Quickstart

On a first run (no config file anywhere), any command offers to launch the
interactive **config wizard**, which interviews you and writes a validated
config file. You can also start it explicitly:

```
runtipi-companion config wizard
```

Or go the manual route:

```
runtipi-companion config init --path ~/.config/runtipi-companion/config.yaml
$EDITOR ~/.config/runtipi-companion/config.yaml   # set runtipi.path, add rclone remotes
```

Then bootstrap the system and take a first backup:

```
runtipi-companion setup --apply                    # the setup wizard (same as 'setup wizard')
runtipi-companion setup rclone --apply             # install rclone + configure remotes
runtipi-companion setup services --apply           # systemd timers for automated backups

runtipi-companion backup run --type daily --apply
runtipi-companion backup list jellyfin

runtipi-companion security harden                  # interactive: pick what to harden
runtipi-companion security harden --all --apply    # or apply everything at once
runtipi-companion security harden --tailscale-security --apply   # VPN-only lockdown only

runtipi-companion setup tailscale --apply
```

## Configuration

See [`runtipi-companion.example.yaml`](./runtipi-companion.example.yaml) for
every field with comments. Config is searched at `/etc/runtipi-companion/config.yaml`
then `~/.config/runtipi-companion/config.yaml`, or pass `--config /path`.

The part that matters most for the "individual retention per remote" ask:

```yaml
backup:
  schedules:                      # local disk retention
    daily: { retention: 7 }
    weekly: { retention: 4 }
  remotes:
    - name: backblaze
      rclone_remote: "b2-runtipi:my-bucket/runtipi-backups"
      schedules:                  # THIS remote's own retention, independent of local
        daily: { retention: 14 }
        monthly: { retention: 12 }
    - name: gdrive
      rclone_remote: "gdrive:runtipi-backups"
      schedules:
        weekly: { retention: 4 } # only syncs weekly backups, keeps 4
```

A remote only receives backups for schedules it explicitly lists, and prunes
itself independently of local disk and every other remote.

Backups live in a per-machine subfolder, locally and on every remote:

```
<backup dir>/<host_label>/<store>/<app>/<app>-<schedule>-<date>.tar.gz
```

`backup.host_label` defaults to the machine's hostname (the config wizard
asks about it too). Several machines can share one remote bucket: each syncs
and prunes only its own subtree, and never touches another host's backups.

## Commands

```
runtipi-companion config   wizard|init|show|validate
runtipi-companion backup   run|list|remotes
runtipi-companion restore  run|list
runtipi-companion update   apps|core|appstores
runtipi-companion setup    wizard|services|rclone|fail2ban|tailscale   # bare 'setup' = wizard
runtipi-companion security harden|status
runtipi-companion tailscale status
runtipi-companion doctor        # health audit: pass/warn/fail, changes nothing
runtipi-companion self-update   # upgrade this tool from PyPI (pipx or pip)
runtipi-companion version
```

`doctor` audits the whole setup read-only -- runtipi install, docker,
backup freshness, rclone remotes, and the VPS-security checklist (sshd
effective config, UFW, fail2ban, tailscale) -- and exits non-zero if any
check fails, so you can run it from monitoring.

Run `runtipi-companion <group> <command> --help` for full options.

Three commands have interactive TUI modes:

- `backup remotes` — add/edit/remove/enable/disable rclone backup remotes in
  your config file from a menu, including per-schedule retention. Changes are
  validated before saving.
- `restore run` with no arguments — pick the source (local disk or any
  configured remote), then the app, then the archive from a list of what
  actually exists, instead of typing filenames. Defaults to a dry-run
  preview; add `--apply` to restore for real.
- `security harden` with no flags (or `--interactive`/`-i`) — checklist menu
  to pick any combination of SSH / UFW / fail2ban / tailscale-only lockdown.
  Flags (`--ssh`, `--ufw`, `--fail2ban`, `--tailscale-security`, `--all`)
  still work for scripting and skip the menu.

## Automating backups

Either cron (matches the original guide):

```
sudo crontab -e
```

```
0 2 * * 2-7 /usr/local/bin/runtipi-companion backup run --type daily --apply
0 2 * * 1   /usr/local/bin/runtipi-companion backup run --type weekly --apply
0 3 1 * *   /usr/local/bin/runtipi-companion backup run --type monthly --apply
```

Or the bundled systemd timers, which add logging via `journalctl` and
`Persistent=true` (a backup missed because the box was off runs as soon as
it's back):

```
runtipi-companion setup services --apply           # daily,weekly,monthly by default
runtipi-companion setup services --schedules daily,weekly,monthly,yearly --apply
```

## Notifications

Set `notify.urls` in the config to any number of
[apprise](https://github.com/caronc/apprise) URLs -- ntfy, Discord, Slack,
email, matrix, and ~80 other services:

```yaml
notify:
  urls:
    - ntfy://ntfy.sh/my-topic
    - discord://webhook_id/webhook_token
  notify_on_success: false   # failures always notify by default
  notify_on_failure: true
```

Backup runs notify on failure (and optionally success) with proper
success/failure message types -- ntfy users get priority/color out of the
box. The old single `notify.webhook_url` (generic JSON POST) still works
but is deprecated.

## Restore

```
runtipi-companion backup list                      # every app's latest backup (shows app ids)
runtipi-companion restore list jellyfin --remote backblaze
runtipi-companion restore run jellyfin jellyfin-daily-2026-07-01.tar.gz --from-remote backblaze --apply
```

Omit `--from-remote` to restore from the local backup directory instead.

Restoring **another machine's** backups (migration path): pass `--host` with
that machine's label, or use the interactive picker (`restore run` with no
arguments), which lists every host it finds on the chosen source. An empty
listing also prints which other host labels exist.

```
runtipi-companion backup list --remote backblaze --host old-vps
runtipi-companion restore run jellyfin jellyfin-daily-2026-07-01.tar.gz \
    --from-remote backblaze --host old-vps --apply
```

## Limitations / things to know

- The backup format is this tool's own (tar.gz of `app`/`app-data`/`user-config`,
  same layout as the original bash script), not `runtipi-cli app backup`'s
  native format -- they aren't interchangeable. `runtipi-cli app backup`
  is still exposed via the `RuntipiCLI` wrapper for scripting if you want it.
- Remote pruning parses `rclone lsf -R` output; very unusual remote path
  layouts (colons or the schedule name embedded oddly in a path segment)
  could confuse the app-name grouping. Test with `--dry-run` after your
  first real sync to a new remote before trusting the retention prune.
- Security hardening covers the concrete steps from Runtipi's own docs
  (SSH keys, UFW, fail2ban). It's not a substitute for reading those docs
  once yourself.

## Ideas for later (not built yet)

Things worth considering if this becomes your daily driver:

- **Backup encryption** (age or gpg) before upload, since remotes are
  third-party cloud storage.
- **`--json` output** on every command for monitoring integration
  (`doctor`'s exit code already works for simple alerting).
- **Fleet mode**: point one config at multiple Runtipi hosts (e.g. via SSH)
  for centralized backup/update status across boxes. Provisioning many
  boxes is already covered by the ansible playbook in
  [`deploy/ansible/`](./deploy/ansible/); this would add day-2 status.
Already graduated from this list: pre-update snapshots, backup integrity
verification, `doctor`, `self-update`, and richer notifications (apprise).

Happy to build any of these next -- say the word.
