# systemd timers (cron alternative)

These replace the crontab lines from Runtipi's own auto-backup-apps guide
with systemd timers, which give you `journalctl -u runtipi-companion-backup@daily`
logs and `Persistent=true` (a missed run, e.g. because the box was off,
fires as soon as it's back up).

## Install

```
sudo cp runtipi-companion-backup@.service /etc/systemd/system/
sudo cp runtipi-companion-backup-daily.timer /etc/systemd/system/
sudo cp runtipi-companion-backup-weekly.timer /etc/systemd/system/
sudo cp runtipi-companion-backup-monthly.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now runtipi-companion-backup-daily.timer
sudo systemctl enable --now runtipi-companion-backup-weekly.timer
sudo systemctl enable --now runtipi-companion-backup-monthly.timer
```

## Check

```
systemctl list-timers | grep runtipi-companion
journalctl -u runtipi-companion-backup@daily.service
```

If you'd rather use plain cron, see the crontab example in the top-level
README.md instead -- both call the exact same command.
