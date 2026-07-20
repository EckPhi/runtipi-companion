# Agent notes for runtipi-companion

Gotchas discovered from real-box testing (a live Runtipi VPS, not just
unit tests / e2e). Read this before touching `system/shell.py`,
`security/hardening.py`, or `setup/wizard.py` — that's where all of this
bit us. Each item is a rule + the incident that taught it, so you can judge
edge cases instead of cargo-culting the rule.

## Package layout

Subpackages by concern, not by "everything flat":

- `backup/` — runner, retention, restore, rclone client
- `config/` — schema (dataclasses), loader (YAML→dataclass), migrations,
  templates (bundled example config)
- `security/` — hardening (SSH/UFW/fail2ban/lockdown), tailscale
- `setup/` — first-run wizard, services (systemd), rclone setup
- `system/` — shell (the `run()` wrapper everything goes through), notify,
  runtipi_cli wrapper, version_check
- `ui/` — TUI bits: classic prompt wizard, form wizard (textual),
  validators, restore picker

`cli.py`, `doctor.py`, `update.py` stay top-level (thin, cross-cutting).

## `system/shell.py` is load-bearing — read it before adding any subprocess call

Every command in this codebase should go through `shell.run()`, not raw
`subprocess`. It has three output modes and getting the wrong one causes
silent hangs that look like bugs elsewhere:

1. **Default (stream-and-collapse)**: on a real terminal, shows a live
   8-line tail of output while the command runs, erases it on exit 0,
   keeps the full output for the error report on failure. This is what
   most non-quiet commands should use — no special flag needed, it's the
   default when `quiet=False, interactive=False, input=None` and
   `console.is_terminal`.
2. **`quiet=True`**: silent capture, for callers that parse `result.stdout`
   (rclone listings, `sshd -T`, `is_app_running`). Never stream these —
   streaming is presentational and would still work, but there's no reason
   to paint a tail nobody reads.
3. **`interactive=True`**: full terminal handoff (stdin/stdout/stderr
   inherited, nothing captured). **Required**, not optional, for:
   - anything that prompts (`tailscale up` printing a login URL, `rclone
     config`'s OAuth flow)
   - anything whose success output the user must see (`tailscale status`,
     `security status` — these commands' output *is* the result; if you
     run them captured, the command succeeds and the user sees nothing)
   - long installers where progress reassures the user it isn't frozen
     (the official runtipi installer, `apt-get install` isn't as
     important since it's quick, but the runtipi installer takes minutes)

**The sudo trap**: the very first `sudo` command in a session prompts for
a password on the tty. In both stream mode (tail repaints over the prompt)
and quiet/captured mode (prompt is captured and never shown), this makes
sudo *silently wait forever* — looks exactly like a hang. Fixed by
`_ensure_sudo_credentials()` in shell.py: before running any `sudo`-prefixed
command in a non-interactive mode, it runs `sudo -v` with full terminal
inheritance first (skipped if already root, skipped off-terminal). If you
add a new sudo call path that bypasses `shell.run()`, you will reintroduce
this bug.

**Never do Python file I/O on paths under `/etc` or other root-owned
locations.** `Path.write_text()` / `shutil.copy2()` run as the invoking
user (a normal user with pipx-installed CLI, not root), and die with
`PermissionError` — while the *shell commands* elsewhere in the same
function were correctly sudo'd, giving a confusing "some of this function
needed root and some didn't" bug. This bit `security/hardening.py` twice
in one session (sshd_config backup/write, then fail2ban's jail.local) — it
is an easy thing to miss because it works fine on a machine where you're
testing as root. Rule: any read/write of a root-owned file goes through
`_sudo_read` / `_sudo_write` / `_sudo_copy` helpers (`cat`/`tee`/`cp -a`
under sudo), never direct `Path` methods. Audit any new file-touching code
in `security/` or `setup/` for this before merging.

## `main()`'s clean-error list needs to stay in sync

`cli.py:main()` wraps `app()` and turns a specific set of exceptions into
a one-line red message + exit 1, instead of typer's full traceback wall
(`pretty_exceptions_enable=False` is set so *un*caught exceptions still
traceback plainly — that's intentional, it means "this is a bug, not an
expected failure mode"). The list is currently: `CommandError`,
`BackupRunError`, `BackupVerificationError`, `FileNotFoundError`,
`PermissionError`. When you introduce a new exception type for an
*expected* operational failure (not a bug), add it here, or a real user
will see a Python traceback for something that should have been a clean
error message. This happened live: `PermissionError` wasn't on the list
yet when the `/etc` file-I/O bug above got hit for real.

## Backup runs must survive one app's failure

`backup/runner.py::run_backup` iterates apps and must not let one app's
`runtipi-cli app stop` failure cancel every other app's backup. This isn't
hypothetical — it happened for real: RabbitMQ's `transient_nonexcl_queues`
deprecation (RabbitMQ 4.3+) broke `runtipi-cli app stop` for *every* app on
a real box, and the first implementation aborted the whole run on app #1.
Now each app is wrapped in `_backup_one_app` + try/except, failures
collected, remaining apps still processed, whatever succeeded still syncs
to remotes, and `BackupRunError` is raised at the end with a summary — so
cron/systemd exit codes and failure notifications still fire, but a
transient failure on one app (or one upstream dependency) doesn't blank
out backups for everything else. If you touch this loop, keep that
per-app isolation.

## `tailscale up` vs `tailscale set`

`tailscale up --ssh` **refuses to run** if the daemon already has other
non-default settings (e.g. `--advertise-exit-node` from an earlier `up`)
and you don't repeat every one of them on the command line — it errors
out rather than silently dropping settings. Hit this for real in
`security harden --tailscale-security`. Fix: use `tailscale set --ssh=true`
to flip one setting in place instead of `tailscale up --ssh`. If you ever
need to bring tailscale up fresh (not just toggle an existing setting),
`up` is still correct — this only applies to *changing a setting on an
already-up daemon*.

## Bootstrapping a fresh Runtipi install: use the official installer, not git clone

`setup/wizard.py` used to `git clone` the runtipi repo when
`runtipi.path` didn't exist. **This can never work** — the git repo does
not contain the `runtipi-cli` binary; it's only produced by the official
installer (`curl -L https://setup.runtipi.io | bash`), which also starts
the stack. Fixed to shell out to that installer instead (via
`interactive=True`, sudo'd via `needs_root()` when `/opt` isn't
user-writable). Also: the installer intentionally does NOT run in
dry-run mode (unlike the old harmless `mkdir`-only clone) — it downloads
and starts real services, too much side effect for a preview.

`needs_root(path)` (in wizard.py) checks the nearest *existing* ancestor
of a target path for write access — use this pattern anywhere you're
about to create a directory/file that might live under `/opt` or `/etc`
on some installs and under `$HOME` on others; don't assume either.

## Config schema versioning

`config/schema.py::CONFIG_VERSION` + `config/migrations.py`. Any change to
the config shape (new field, renamed field, changed default) needs:
1. Bump `CONFIG_VERSION`.
2. Add a `_migrate_N_to_N+1` pure dict→dict function to `MIGRATIONS` in
   migrations.py.
3. Update `config/templates.py` (the bundled example) AND
   `runtipi-companion.example.yaml` at repo root — they must stay in sync
   (a test enforces this: they're compared byte-for-byte).

The loader hard-rejects a config with a version number *higher* than
`CONFIG_VERSION` — better than silently misreading a future schema.
Migrations are additive/preserving by design: old values are kept, only
new defaults are filled in.

## Form wizard (`ui/form_wizard.py`) pre-fill contract

If the form doesn't expose a config field as a widget, it MUST be
round-tripped from the existing file in `_collect()`, not defaulted. The
wizard first shipped without this — reopening it on an existing config
would silently reset `sleep_duration`, the `updates` section, legacy
`webhook_url`, `auth_key_env`, and the tailscale coordination port back to
hardcoded defaults. Every `_get(...)` call in `compose()` needs a matching
carry-through in `_collect()`. When you add a new config field: either
expose it as a widget (with a validator) or explicitly carry it through
via `self._get(...)` — don't let it silently fall back to a hardcoded
default when editing an existing config.

Apprise URLs are a **list of dynamic rows**, not a CSV field — CSV was
briefly tried and is wrong on the merits (apprise URLs can legally contain
commas in query params / recipient lists), not just a UX preference.

## rclone remote paths

- `rclone_remote` values get `.rstrip("/")` applied on load (loader) and in
  the wizards — a trailing slash produces double-slash paths once
  `<host_label>/<store>/<app>/...` gets appended. Any new code path that
  accepts a raw rclone target string from the user needs the same
  stripping.
- Backups sync to `<remote>/<host_label>/...` — the host subfolder is
  **remote-only**; local disk stays flat (`<local_path>/<store>/<app>/`).
  Don't reintroduce a local host subfolder — it was tried and reverted
  because local disk is inherently single-machine, no isolation needed.
- Remote sync passes `--include '*-<schedule>-*.tar.gz'` — without this,
  every sync re-scans/re-considers the *entire* local backup tree
  including other schedules and pre-update snapshots that specific remote
  never lists (and therefore never prunes there), which is both wrong per
  the "a remote only gets what it lists" contract and needlessly slow.

## Testing notes

- `uvx ruff@0.8.4 check --fix .` then `uvx ruff@0.8.4 format .` before every
  commit — pre-commit hook runs this too, but running it manually first
  avoids amend-churn.
- e2e suite (`tests/e2e/e2e.sh`) actually runs against real `rclone` with a
  local-backend remote; run it locally with:
  `PATH="$(pwd)/.venv/bin:$PATH" bash tests/e2e/e2e.sh` (needs `uv sync
  --extra dev` first). It catches real path/flag mistakes that mocked unit
  tests don't (e.g. the `soft_wrap`/pipefail issue with `backup list`
  output).
- Textual form wizard tests use `App.run_test()` (headless pilot) — see
  `tests/test_form_wizard.py` for the pattern (`_drive()` helper).
- When a fix is prompted by a real terminal transcript pasted by the user,
  reproduce it with a monkeypatched `shell.run`/`subprocess.run` stub
  rather than trusting "it looks right" — several of the bugs above
  (sudo hang, `/etc` PermissionError, tailscale set vs up) only showed up
  on the actual box, never in the mocked test suite, until tests were
  added that specifically simulated the real failure mode.

## Known non-issues (don't "fix" these)

- RabbitMQ `transient_nonexcl_queues` deprecation breaking
  `runtipi-cli app stop` is an **upstream runtipi/RabbitMQ** problem, not
  ours. Our fix was making the backup loop resilient to it (see above),
  not working around RabbitMQ itself.
- `tailscale install` (the old top-level command) was intentionally
  removed in favor of `setup tailscale` — this was a deliberate breaking
  change (`f19a343`), not an oversight if you see old docs/scripts
  referencing it.
