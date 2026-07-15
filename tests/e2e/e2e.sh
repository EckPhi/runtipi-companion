#!/usr/bin/env bash
# End-to-end exercise of runtipi-companion against real tools:
#   - real rclone with a local-backend remote (same code path as B2/S3/...)
#   - a real runtipi install if $RUNTIPI_DIR points at one (CI installs it);
#     falls back to seeding the directory layout + a stub runtipi-cli, since
#     apps can only be installed through the runtipi web UI and the backup/
#     restore flow never needs the CLI when apps aren't running.
#   - real tailscale if installed (status + dry-run install plan)
#
# Run in Docker (recommended -- don't run this against your real host):
#   docker build -f tests/e2e/Dockerfile -t runtipi-companion-e2e .
#   docker run --rm runtipi-companion-e2e
#
# Run directly (needs rclone + tailscale + runtipi-companion on PATH):
#   bash tests/e2e/e2e.sh
set -euo pipefail

WORK="${E2E_WORK_DIR:-$(mktemp -d)}"
RUNTIPI_DIR="${RUNTIPI_DIR:-$WORK/runtipi}"
REMOTE_DIR="$WORK/remote-storage"
BACKUP_DIR="$WORK/backups"
CFG="$WORK/config.yaml"
RC=(runtipi-companion)

say()  { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
fail() { echo "E2E FAILURE: $*" >&2; exit 1; }

command -v rclone >/dev/null || fail "rclone not installed"
command -v runtipi-companion >/dev/null || fail "runtipi-companion not on PATH"

say "Seed a test app into the runtipi layout at $RUNTIPI_DIR"
mkdir -p "$RUNTIPI_DIR/apps/migrated/e2etest" \
         "$RUNTIPI_DIR/app-data/migrated/e2etest" \
         "$RUNTIPI_DIR/user-config/migrated/e2etest"
echo "services: {}" > "$RUNTIPI_DIR/apps/migrated/e2etest/docker-compose.yml"
echo "precious-data-v1" > "$RUNTIPI_DIR/app-data/migrated/e2etest/data.txt"
echo "USER_SETTING=1" > "$RUNTIPI_DIR/user-config/migrated/e2etest/app.env"

if [ ! -x "$RUNTIPI_DIR/runtipi-cli" ]; then
  say "No real runtipi-cli at $RUNTIPI_DIR -- installing a stub"
  printf '#!/usr/bin/env bash\necho "stub runtipi-cli: $*"\nexit 0\n' > "$RUNTIPI_DIR/runtipi-cli"
  chmod +x "$RUNTIPI_DIR/runtipi-cli"
else
  say "Using real runtipi-cli: $("$RUNTIPI_DIR/runtipi-cli" version || true)"
fi

say "Configure an rclone local-backend remote"
# Hermetic rclone config -- don't touch the user's real remotes. Exported so
# every rclone invocation runtipi-companion spawns picks it up too.
export RCLONE_CONFIG="$WORK/rclone.conf"
rclone config create e2elocal local >/dev/null
mkdir -p "$REMOTE_DIR"

say "Write config"
cat > "$CFG" <<EOF
runtipi:
  path: $RUNTIPI_DIR
  apps: [e2etest]
backup:
  work_dir: $WORK/scratch
  local_path: $BACKUP_DIR
  stop_apps: true
  sleep_duration: 0
  schedules:
    daily:
      retention: 2
  remotes:
    - name: cloud
      rclone_remote: "e2elocal:$REMOTE_DIR"
      schedules:
        daily:
          retention: 1
security:
  tailscale_only:
    enabled: true
EOF

"${RC[@]}" config validate --config "$CFG"
"${RC[@]}" config show --config "$CFG" >/dev/null

say "Setup wizard (dry-run)"
"${RC[@]}" setup wizard --config "$CFG" --yes

say "Seed two old archives to prove retention pruning"
mkdir -p "$BACKUP_DIR/migrated/e2etest"
tar -czf "$BACKUP_DIR/migrated/e2etest/e2etest-daily-2020-01-01.tar.gz" -T /dev/null
tar -czf "$BACKUP_DIR/migrated/e2etest/e2etest-daily-2020-01-02.tar.gz" -T /dev/null

say "Backup: dry-run preview, then apply"
"${RC[@]}" backup run --type daily --config "$CFG"
"${RC[@]}" backup run --type daily --apply --config "$CFG"

TODAY=$(date +%F)
ARCHIVE="e2etest-daily-$TODAY.tar.gz"
test -f "$BACKUP_DIR/migrated/e2etest/$ARCHIVE" || fail "local archive was not created"
test ! -f "$BACKUP_DIR/migrated/e2etest/e2etest-daily-2020-01-01.tar.gz" \
  || fail "local retention (2) did not prune the oldest archive"
[ "$(find "$BACKUP_DIR" -name '*.tar.gz' | wc -l)" -eq 2 ] || fail "expected exactly 2 local archives"

say "Verify remote sync + remote-specific retention (1)"
test -f "$REMOTE_DIR/migrated/e2etest/$ARCHIVE" || fail "archive was not synced to the rclone remote"
[ "$(find "$REMOTE_DIR" -name '*.tar.gz' | wc -l)" -eq 1 ] \
  || fail "remote retention (1) should leave exactly 1 archive on the remote"

say "backup list (local + remote)"
"${RC[@]}" backup list e2etest --config "$CFG" | grep -q "$ARCHIVE" || fail "'backup list' missing local archive"
"${RC[@]}" backup list e2etest --remote cloud --config "$CFG" | grep -q "$ARCHIVE" || fail "'backup list --remote' missing archive"

say "Restore from local backup"
echo "corrupted" > "$RUNTIPI_DIR/app-data/migrated/e2etest/data.txt"
"${RC[@]}" restore run e2etest "$ARCHIVE" --store migrated --config "$CFG" --apply --yes
grep -q "precious-data-v1" "$RUNTIPI_DIR/app-data/migrated/e2etest/data.txt" \
  || fail "local restore did not bring app-data back"
grep -q "USER_SETTING=1" "$RUNTIPI_DIR/user-config/migrated/e2etest/app.env" \
  || fail "local restore did not bring user-config back"

say "Restore from the rclone remote"
echo "corrupted again" > "$RUNTIPI_DIR/app-data/migrated/e2etest/data.txt"
"${RC[@]}" restore run e2etest "migrated/e2etest/$ARCHIVE" --from-remote cloud --config "$CFG" --apply --yes
grep -q "precious-data-v1" "$RUNTIPI_DIR/app-data/migrated/e2etest/data.txt" \
  || fail "remote restore did not bring app-data back"

say "Update commands (dry-run)"
"${RC[@]}" update apps --config "$CFG"
"${RC[@]}" update appstores --config "$CFG"

say "Security hardening plan (dry-run, all items incl. tailscale-security)"
OUT=$("${RC[@]}" security harden --all --config "$CFG")
echo "$OUT"
echo "$OUT" | grep -q "Tailscale-only access plan" || fail "'security harden --all' did not include the tailscale-security plan"

say "Security hardening: tailscale-security in isolation (dry-run)"
"${RC[@]}" security harden --tailscale-security --config "$CFG"

say "Security hardening: interactive TUI selection (dry-run)"
OUT=$(printf '2\n' | "${RC[@]}" security harden --interactive --config "$CFG")
echo "$OUT"
echo "$OUT" | grep -q "UFW firewall plan" \
  || fail "'security harden --interactive' did not apply the selected item"

if command -v tailscale >/dev/null; then
  say "Tailscale (status + dry-run install plan)"
  "${RC[@]}" tailscale status
  "${RC[@]}" tailscale install --config "$CFG"
else
  say "Tailscale not installed, skipping tailscale checks"
fi

say "E2E PASSED (work dir: $WORK)"
