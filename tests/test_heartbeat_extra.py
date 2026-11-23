"""Additional tests for scripts/heartbeat_check.py: threshold boundaries, DB errors, notification, wiring shapes."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "heartbeat_check.py"


@pytest.fixture
def hb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("heartbeat_check_extra", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "HOME", str(tmp_path))
    monkeypatch.setattr(mod, "SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "usage.db"))
    monkeypatch.setattr(mod, "LOG_PATH", str(tmp_path / "logs" / "heartbeat.log"))
    monkeypatch.setattr(mod, "USAGE_HOOK", "/h/claude-code-usage-hook.py")
    return mod


def write_db(path, last_ts):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE token_usage (timestamp TEXT)")
    conn.execute("INSERT INTO token_usage VALUES (?)", (last_ts,))
    conn.commit()
    conn.close()


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def touch_transcript(home, hours_ago, name="s.jsonl"):
    d = Path(home) / ".claude" / "projects" / "p"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("{}")
    t = time.time() - hours_ago * 3600
    os.utime(f, (t, t))


def settings(hb, hooks):
    Path(hb.SETTINGS).write_text(json.dumps({"hooks": hooks}))


def entry(cmd):
    return [{"matcher": "", "hooks": [{"type": "command", "command": cmd}]}]


# ---- _hooks_wired ---------------------------------------------------------------------------------

def test_hooks_wired_requires_both_events_pointing_at_the_usage_hook(hb):
    settings(hb, {"Stop": entry("python3 /h/claude-code-usage-hook.py"), "SubagentStop": entry("python3 /h/other.py")})
    ok, detail = hb._hooks_wired()
    assert (ok, detail) == (False, "SubagentStop hook missing from settings.json")
    settings(hb, {"SubagentStop": entry("python3 /h/claude-code-usage-hook.py")})
    assert hb._hooks_wired() == (False, "Stop hook missing from settings.json")        # Stop is checked first


def test_hooks_wired_finds_hook_among_several_entries(hb):
    both = [{"hooks": [{"command": "echo hi"}]}, {"hooks": [{"command": "/h/claude-code-usage-hook.py --x"}, {}]}]
    settings(hb, {"Stop": both, "SubagentStop": both})
    assert hb._hooks_wired() == (True, "ok")


def test_hooks_wired_missing_settings_file_and_no_hooks_key(hb):
    assert hb._hooks_wired() == (False, "settings.json missing or not valid JSON")
    Path(hb.SETTINGS).write_text("{}")
    assert hb._hooks_wired() == (False, "Stop hook missing from settings.json")


# ---- _db_freshness boundaries --------------------------------------------------------------------

def test_row_just_inside_the_threshold_is_fresh(hb):
    write_db(hb.DB_PATH, ago(29.9))
    stale, detail = hb._db_freshness()
    assert stale is False and detail.startswith("last row ")


def test_row_just_past_the_threshold_consults_transcripts(hb):
    write_db(hb.DB_PATH, ago(30.1))
    assert hb._db_freshness() == (False, "no transcripts found, nothing to compare against")


def test_session_gap_must_exceed_the_threshold_to_flag(hb):
    # DB last row 60h ago; newest transcript 31h newer than that row (29h ago) -> stale; 29h newer (31h ago) -> not
    write_db(hb.DB_PATH, ago(60))
    touch_transcript(hb.HOME, 29)
    assert hb._db_freshness()[0] is True
    os.remove(next(Path(hb.HOME).rglob("s.jsonl")))
    touch_transcript(hb.HOME, 31)
    stale, detail = hb._db_freshness()
    assert stale is False and detail.endswith("no newer session activity")


def test_newest_transcript_among_several_is_used(hb):
    write_db(hb.DB_PATH, ago(60))
    touch_transcript(hb.HOME, 55, "old.jsonl")
    touch_transcript(hb.HOME, 1, "new.jsonl")
    (Path(hb.HOME) / ".claude" / "projects" / "p" / "notes.txt").write_text("ignored")
    assert hb._db_freshness()[0] is True


def test_db_error_is_a_stale_signal(hb):
    Path(hb.DB_PATH).write_text("this is not a sqlite database")
    stale, detail = hb._db_freshness()
    assert stale is True and detail.startswith("db error:")
    other = Path(hb.HOME) / "nodb.db"
    sqlite3.connect(other).close()                                   # valid DB but no token_usage table
    hb.DB_PATH = str(other)
    stale, detail = hb._db_freshness()
    assert stale is True and detail.startswith("db error:")


def test_unreadable_transcript_stat_is_ignored(hb, monkeypatch):
    write_db(hb.DB_PATH, ago(60))
    touch_transcript(hb.HOME, 1)

    def boom(path):
        raise OSError("stat failed")
    monkeypatch.setattr(os.path, "getmtime", boom)
    assert hb._db_freshness() == (False, "no transcripts found, nothing to compare against")


# ---- _notify / _log / main ----------------------------------------------------------------------

def test_notify_builds_a_quoted_applescript(hb, monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    hb._notify('Title "x"', 'a "quoted" msg')
    assert seen["cmd"][:2] == ["osascript", "-e"]
    assert seen["cmd"][2] == 'display notification "a \\"quoted\\" msg" with title "Title \\"x\\"" sound name "Basso"'
    assert seen["kw"]["timeout"] == 5 and seen["kw"]["capture_output"] is True


def test_notify_swallows_failures(hb, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("osascript", 5)
    monkeypatch.setattr(subprocess, "run", boom)
    hb._notify("t", "m")                                              # must not raise


def test_log_appends_one_json_line_per_call_and_creates_directory(hb):
    hb._log({"a": 1})
    hb._log({"b": 2})
    assert [json.loads(x) for x in Path(hb.LOG_PATH).read_text().splitlines()] == [{"a": 1}, {"b": 2}]


def test_main_reports_both_problems_in_one_notification(hb, monkeypatch):
    notes = []
    monkeypatch.setattr(hb, "_notify", lambda t, m: notes.append((t, m)))
    with pytest.raises(SystemExit) as ex:
        hb.main()                                                     # no settings, no DB
    assert ex.value.code == 0
    last = json.loads(Path(hb.LOG_PATH).read_text().splitlines()[-1])
    assert last["ok"] is False and len(last["problems"]) == 2
    assert last["problems"][0] == "hooks: settings.json missing or not valid JSON"
    assert last["problems"][1] == "tracker: token_usage.db does not exist"
    assert last["hooks_detail"] == "settings.json missing or not valid JSON" and last["db_detail"] == "token_usage.db does not exist"
    assert notes == [("Token tracker heartbeat FAILED", "; ".join(last["problems"]))]
    assert datetime.fromisoformat(last["timestamp"]).tzinfo is not None


def test_stale_threshold_constant(hb):
    assert hb.STALE_AFTER_HOURS == 30
