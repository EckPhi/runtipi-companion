"""Bundled config template used by `runtipi-companion config init`.

Kept byte-for-byte in sync with the top-level runtipi-companion.example.yaml
(tests/test_config.py enforces this).
"""

EXAMPLE_CONFIG = """\
# runtipi-companion configuration
# See README.md for full documentation of every field.

runtipi:
  path: /opt/runtipi
  # runtipi-cli is NOT on $PATH by default. We auto-detect <path>/runtipi-cli;
  # set this explicitly if yours lives somewhere else.
  cli_path: null
  # Leave empty to back up / update every installed app. Otherwise list app
  # ids explicitly (the part before the ":store" in "app-id:store").
  apps: []

backup:
  work_dir: /tmp/runtipi-companion
  # Defaults to <runtipi.path>/backups if unset.
  local_path: null
  # On every remote, this machine's backups sync into their own subfolder:
  # <remote>/<host_label>/<store>/<app>/. Several machines can share one
  # bucket and never prune each other. Local disk stays flat. Defaults to
  # this machine's hostname.
  host_label: null
  stop_apps: true
  sleep_duration: 10

  # Local retention per schedule (number of archives to keep on disk).
  schedules:
    daily:
      retention: 7
    weekly:
      retention: 4
    monthly:
      retention: 6
    yearly:
      retention: 2

  # Each remote is an rclone remote (configure it first with `rclone config`).
  # Every remote gets its OWN retention per schedule -- e.g. keep more daily
  # backups in cheap cold storage than you keep on the local disk.
  remotes:
    - name: backblaze
      rclone_remote: "b2-runtipi:my-bucket/runtipi-backups"
      enabled: true
      bandwidth_limit: null   # e.g. "5M" to cap upload speed
      schedules:
        daily:
          retention: 14
        weekly:
          retention: 8
        monthly:
          retention: 12

    - name: gdrive
      rclone_remote: "gdrive:runtipi-backups"
      enabled: false
      schedules:
        weekly:
          retention: 4

security:
  ssh:
    disable_password_auth: true
    disable_root_login: true
    port: null   # e.g. 2847 to change the SSH port; leave null to keep 22
  ufw:
    enable: true
    allowed_tcp_ports: [22]   # 80/443 intentionally NOT opened by default --
                              # pair with tailscale for remote dashboard access
  fail2ban:
    enabled: true
    maxretry: 3
    bantime: 3600
  # VPN-only lockdown: allow inbound traffic only on the tailscale0 interface
  # (plus tailscale's own coordination port) and switch SSH to `tailscale ssh`.
  # Requires tailscale to already be installed and up -- see the `tailscale`
  # section below. Apply with `security harden --tailscale-security --apply`.
  tailscale_only:
    enabled: false
    tailscale_ssh: true
    tailscale_port_udp: 41641

tailscale:
  enabled: false
  auth_key_env: TAILSCALE_AUTHKEY   # export this env var; never put keys in yaml
  advertise_exit_node: false
  ssh: false

updates:
  auto_update_core: false
  auto_update_apps: false
  exclude_apps: []
  # Take a local-only snapshot of the affected apps right before
  # `update apps` / `update core`, so every update is reversible via
  # `restore run`. Keeps the 2 most recent snapshots per app.
  backup_before: true

notify:
  # Apprise URLs (https://github.com/caronc/apprise) -- any number, each one
  # receives every notification. Verify with: runtipi-companion notify test
  # Examples:
  #   - ntfy://ntfy.sh/my-topic
  #   - discord://webhook_id/webhook_token
  #   - mailto://user:app-password@gmail.com
  urls: []
  # Deprecated: single generic JSON-POST webhook, kept for backward
  # compatibility. Prefer urls above.
  webhook_url: null
  notify_on_success: false
  notify_on_failure: true
"""
