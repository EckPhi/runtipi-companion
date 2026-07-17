# Ansible installer

A thin playbook that installs `runtipi-companion` on one or more Debian/Ubuntu
boxes and (optionally) runs its setup wizard, security hardening, tailscale
install, and systemd backup timers.

**It contains no logic of its own.** Every step that changes system state
shells out to `runtipi-companion` with `--yes --apply`, so the CLI's safety
checks (sshd_config validation, the authorized_keys lockout guard) still
apply, and there is exactly one implementation of everything. If you only
manage a single box, you don't need this at all — just `pipx install
runtipi-companion` and follow the top-level README.

## Usage

```bash
cp inventory.example.yml inventory.yml   # edit hosts
ansible-playbook -i inventory.yml install.yml
```

By default this only installs the package, generates an example config at
`/etc/runtipi-companion/config.yaml` (if none exists), runs the setup wizard,
and installs the systemd backup timers. Hardening and tailscale are opt-in:

```bash
ansible-playbook -i inventory.yml install.yml \
  -e companion_config_src=./my-box-config.yaml \
  -e companion_harden=true \
  -e companion_tailscale_install=true
```

## Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `companion_config_src` | `""` | Local config file to upload to `/etc/runtipi-companion/config.yaml`. Empty: generate an example config on the target instead (first run only). |
| `companion_upgrade` | `false` | Run `pipx upgrade runtipi-companion`. |
| `companion_run_setup` | `true` | Run `setup wizard --yes --apply` (clone runtipi if missing, prepare, start, create backup dirs). |
| `companion_harden` | `false` | Run `security harden` with `companion_harden_flags`. |
| `companion_harden_flags` | `--ssh --ufw --fail2ban` | Which hardening steps to apply. |
| `companion_tailscale_install` | `false` | Run `tailscale install --yes --apply`. |
| `companion_tailscale_security` | `false` | **Danger, read below.** Run `security harden --tailscale-security`. |
| `companion_install_timers` | `true` | Run `setup services` to install + enable the bundled systemd backup timers. |
| `companion_timers` | daily, weekly, monthly | Which timers to enable. |

## Tailscale lockdown ordering (read before enabling)

`companion_tailscale_security=true` restricts UFW to the `tailscale0`
interface and switches SSH to tailscale ssh. **If ansible is connected via
the public IP, this cuts off the connection it is running over.**

Safe order:

1. Run once with `companion_tailscale_install=true` (lockdown still off),
   then `tailscale up` on the box and note its tailscale IP.
2. Point `ansible_host` at the tailscale IP.
3. Re-run with `companion_tailscale_security=true`.

## Notes

- rclone remotes cannot be set up unattended (`rclone config` is
  interactive and needs credentials). Configure them once per box, or
  distribute a pre-built `~/.config/rclone/rclone.conf` yourself.
- SSH hardening refuses to disable password auth when it finds no
  `authorized_keys` (so you can't lock yourself out). Since ansible
  usually connects with a key, this check passes; the playbook does not
  expose `--force` on purpose.
- The playbook installs with `PIPX_BIN_DIR=/usr/local/bin` so the binary
  lands where the systemd units and cron examples expect it.
