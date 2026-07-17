from runtipi_companion.backup.retention import parse_backup_filename, select_prunable


def test_parse_backup_filename():
    parsed = parse_backup_filename("jellyfin-daily-2026-07-01.tar.gz")
    assert parsed == {"app": "jellyfin", "schedule": "daily", "date": "2026-07-01", "seq": None}


def test_parse_backup_filename_no_match():
    assert parse_backup_filename("not-a-backup.txt") is None


def test_parse_backup_filename_app_with_dashes():
    parsed = parse_backup_filename("code-server-weekly-2026-01-05.tar.gz")
    assert parsed["app"] == "code-server"
    assert parsed["schedule"] == "weekly"


def test_select_prunable_keeps_n_most_recent():
    files = [
        "jellyfin-daily-2026-07-01.tar.gz",
        "jellyfin-daily-2026-07-02.tar.gz",
        "jellyfin-daily-2026-07-03.tar.gz",
        "jellyfin-daily-2026-07-04.tar.gz",
    ]
    prunable = select_prunable(files, "jellyfin", "daily", keep=2)
    assert set(prunable) == {
        "jellyfin-daily-2026-07-01.tar.gz",
        "jellyfin-daily-2026-07-02.tar.gz",
    }


def test_select_prunable_ignores_other_apps_and_schedules():
    files = [
        "jellyfin-daily-2026-07-01.tar.gz",
        "sonarr-daily-2026-07-01.tar.gz",
        "jellyfin-weekly-2026-06-29.tar.gz",
    ]
    prunable = select_prunable(files, "jellyfin", "daily", keep=0)
    assert prunable == ["jellyfin-daily-2026-07-01.tar.gz"]


def test_select_prunable_keep_more_than_exists():
    files = ["jellyfin-daily-2026-07-01.tar.gz"]
    assert select_prunable(files, "jellyfin", "daily", keep=5) == []


def test_select_prunable_negative_keep_treated_as_zero():
    files = ["jellyfin-daily-2026-07-01.tar.gz"]
    assert select_prunable(files, "jellyfin", "daily", keep=-1) == files


def test_select_latest_picks_newest_across_schedules():
    from runtipi_companion.backup.retention import select_latest

    names = [
        "app-daily-2026-07-01.tar.gz",
        "app-weekly-2026-07-15.tar.gz",
        "app-daily-2026-07-10.tar.gz",
        "junk.txt",
    ]
    assert select_latest(names) == "app-weekly-2026-07-15.tar.gz"
    assert select_latest(["junk.txt"]) is None
