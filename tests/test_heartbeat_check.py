"""Tests for scripts/heartbeat_check.py with every path redirected to tmp."""
import importlib.util
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "heartbeat_check.py"


@pytest.fixture
def hb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("heartbeat_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "HOME", str(tmp_path))
    monkeypatch.setattr(mod, "SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "usage.db"))
    monkeypatch.setattr(mod, "LOG_PATH", str(tmp_path / "logs" / "heartbeat.log"))
    monkeypatch.setattr(mod, "USAGE_HOOK", "/h/claude-code-usage-hook.py")
    notes = []
    monkeypatch.setattr(mod, "_notify", lambda t, m: notes.append((t, m)))
    mod._notes = notes
    return mod


def _settings(path, events):
    hooks = {e: [{"matcher": "", "hooks": [{"type": "command",
                                            "command": "python3 /h/claude-code-usage-hook.py"}]}]
             for e in events}
    Path(path).write_text(json.dumps({"hooks": hooks}))


def _db(path, last_ts):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE token_usage (timestamp TEXT)")
    if last_ts is not None:
        conn.execute("INSERT INTO token_usage VALUES (?)", (last_ts,))
    conn.commit()
    conn.close()


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _transcript(home, hours_ago):
    d = Path(home) / ".claude" / "projects" / "p"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s.jsonl"
    f.write_text("{}")
    t = time.time() - hours_ago * 3600
    os.utime(f, (t, t))


def test_hooks_wired_both_present(hb):
    _settings(hb.SETTINGS, ["Stop", "SubagentStop"])
    assert hb._hooks_wired() == (True, "ok")


def test_hooks_wired_detects_missing_subagentstop(hb):
    _settings(hb.SETTINGS, ["Stop"])
    ok, detail = hb._hooks_wired()
    assert not ok and "SubagentStop" in detail


def test_hooks_wired_invalid_settings(hb):
    Path(hb.SETTINGS).write_text("{oops")
    assert hb._hooks_wired()[0] is False


def test_freshness_missing_db_and_empty_table(hb):
    assert hb._db_freshness() == (True, "token_usage.db does not exist")
    _db(hb.DB_PATH, None)
    assert hb._db_freshness() == (True, "token_usage table is empty")


def test_freshness_recent_row_is_fresh(hb):
    _db(hb.DB_PATH, _ago(2))
    assert hb._db_freshness()[0] is False


def test_freshness_unparseable_timestamp_is_flagged(hb):
    _db(hb.DB_PATH, "2026-10-25T10:00:00Z")
    stale, detail = hb._db_freshness()
    assert stale and "unparseable" in detail


def test_freshness_old_row_but_no_sessions_is_quiet_day(hb):
    _db(hb.DB_PATH, _ago(48))
    assert hb._db_freshness() == (False, "no transcripts found, nothing to compare against")
    _transcript(hb.HOME, 47)  # session activity no newer than the threshold gap
    assert hb._db_freshness()[0] is False


def test_freshness_old_row_with_recent_session_is_stale(hb):
    _db(hb.DB_PATH, _ago(48))
    _transcript(hb.HOME, 1)
    stale, detail = hb._db_freshness()
    assert stale and "sessions ran" in detail


def test_main_logs_and_notifies_only_on_problems(hb):
    _settings(hb.SETTINGS, ["Stop", "SubagentStop"])
    _db(hb.DB_PATH, _ago(1))
    with pytest.raises(SystemExit) as ex:
        hb.main()
    assert ex.value.code == 0
    entry = json.loads(Path(hb.LOG_PATH).read_text().splitlines()[-1])
    assert entry["ok"] is True and hb._notes == []

    _settings(hb.SETTINGS, [])
    with pytest.raises(SystemExit) as ex:
        hb.main()
    assert ex.value.code == 0  # never blocks
    entry = json.loads(Path(hb.LOG_PATH).read_text().splitlines()[-1])
    assert entry["ok"] is False and len(hb._notes) == 1
