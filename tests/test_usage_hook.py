"""Tests for hooks/claude-code-usage-hook.py (no network; temp dirs only).

Every path the hook writes to (state dir, alert log, DB) is redirected into
tmp_path so the real ~/.ruflo is never touched.
"""
import importlib.util
import io
import json
import math
import sqlite3
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "claude-code-usage-hook.py"


@pytest.fixture
def hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("usage_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(mod, "ALERT_LOG", str(tmp_path / "logs" / "alerts.log"))
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "usage.db"))
    return mod


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE token_usage (id INTEGER PRIMARY KEY, timestamp TEXT, task_id TEXT,
           task_name TEXT, model TEXT, input_tokens INT, output_tokens INT, total_tokens INT,
           cost REAL, market TEXT, status TEXT, duration_seconds REAL)"""
    )
    conn.commit()
    conn.close()


def _write_transcript(path, lines):
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _assistant(msg_id, model="claude-sonnet-4-6", inp=10, out=5, cread=0, ccreate=0,
               ts="2026-10-25T10:00:00.123Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": msg_id, "model": model,
                        "usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_read_input_tokens": cread,
                                  "cache_creation_input_tokens": ccreate}}}


def _tool_result(is_error):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t", "is_error": is_error, "content": "x"}]}}


# ── pricing ──────────────────────────────────────────────────────────────

def test_price_for_unknown_model_falls_back_to_default(hook):
    assert hook._price_for("") == hook.DEFAULT_PRICE
    assert hook._price_for(None) == hook.DEFAULT_PRICE
    assert hook._price_for("gpt-x") == hook.DEFAULT_PRICE


def test_estimate_cost_cache_multipliers(hook):
    # 1M of each token kind on a fixed price: in*1 + create*1.25 + read*0.10 + out
    p = hook._price_for("claude-haiku-4-5")
    cost = hook._estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    expected = 1e6 * p["in"] * (1 + 1.25 + 0.10) + 1e6 * p["out"]
    assert math.isclose(cost, expected)


@pytest.mark.parametrize("model,in_per_m,out_per_m", [
    # Anthropic first-party list prices (USD per 1M tokens), pricing table cached 2026-10-06
    ("claude-opus-5-5", 4.0, 20.0),
    ("claude-opus-5", 5.0, 25.0),
    ("claude-opus-4-8", 5.0, 25.0),
    ("claude-opus-4-6", 5.0, 25.0),
    ("claude-sonnet-5-5", 2.0, 10.0),
    ("claude-sonnet-4-6", 3.0, 15.0),
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-haiku-4-5", 1.0, 5.0),
    ("claude-fable-5-1", 10.0, 50.0),
    ("claude-opus-4-1", 15.0, 75.0),
])
def test_price_for_current_models_matches_list_price(hook, model, in_per_m, out_per_m):
    p = hook._price_for(model)
    assert math.isclose(p["in"] * 1e6, in_per_m), (model, p)
    assert math.isclose(p["out"] * 1e6, out_per_m), (model, p)


# ── transcript parsing ───────────────────────────────────────────────────

def test_iter_usage_missing_file_yields_nothing(hook, tmp_path):
    assert list(hook._iter_assistant_usage(str(tmp_path / "nope.jsonl"))) == []


def test_iter_usage_skips_bad_lines_and_messages_without_usage_or_id(hook, tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("not json\n\n" + "\n".join(json.dumps(x) for x in [
        {"type": "assistant", "message": {"id": "m0"}},                     # no usage
        {"type": "assistant", "message": {"usage": {"input_tokens": 1}}},  # no id, no uuid
        _assistant("m1"),
    ]) + "\n")
    rows = list(hook._iter_assistant_usage(str(t)))
    assert [r[0] for r in rows] == ["m1"]
    assert rows[0][3] == "2026-10-25 10:00:00"  # ISO -> sqlite format, fraction dropped


def test_iter_usage_uses_uuid_when_message_id_missing(hook, tmp_path):
    t = tmp_path / "t.jsonl"
    _write_transcript(t, [{"type": "assistant", "uuid": "u-1",
                           "message": {"model": "m", "usage": {"input_tokens": 3}}}])
    assert list(hook._iter_assistant_usage(str(t)))[0][0] == "u-1"


def test_tool_error_flag_attaches_to_next_assistant_message_only(hook, tmp_path):
    t = tmp_path / "t.jsonl"
    _write_transcript(t, [
        _assistant("a1"),
        _tool_result(is_error=False),
        _assistant("a2"),
        _tool_result(is_error=True),
        _assistant("a3"),
        _assistant("a4"),
    ])
    flags = {r[0]: r[4] for r in hook._iter_assistant_usage(str(t))}
    assert flags == {"a1": False, "a2": False, "a3": True, "a4": False}


# ── state ────────────────────────────────────────────────────────────────

def test_state_key_is_per_transcript(hook):
    assert hook._state_key("/a.jsonl") == hook._state_key("/a.jsonl")
    assert hook._state_key("/a.jsonl") != hook._state_key("/b.jsonl")
    assert len(hook._state_key(None)) == 16


def test_state_roundtrip_and_corrupt_file(hook, tmp_path):
    hook._save_state("k", {"x", "y"})
    assert hook._load_state("k") == {"x", "y"}
    Path(hook.STATE_DIR, "bad.json").write_text("{not json")
    assert hook._load_state("bad") == set()
    assert hook._load_state("missing") == set()


# ── main() end-to-end ────────────────────────────────────────────────────

def _run_main(hook, monkeypatch, event):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(hook, "_infer_market", lambda e: "repo-x")
    with pytest.raises(SystemExit) as ex:
        hook.main()
    assert ex.value.code == 0


def test_main_inserts_rows_once_and_marks_tool_error(hook, tmp_path, monkeypatch):
    _make_db(hook.DB_PATH)
    t = tmp_path / "t.jsonl"
    _write_transcript(t, [_assistant("a1", inp=100, out=50, cread=1000, ccreate=10),
                          _tool_result(True),
                          _assistant("a2", out=7),
                          _assistant("a0", inp=0, out=0)])  # zero-token row skipped
    ev = {"session_id": "s", "transcript_path": str(t), "hook_event_name": "SubagentStop"}
    _run_main(hook, monkeypatch, ev)
    _run_main(hook, monkeypatch, ev)  # second Stop must not duplicate
    conn = sqlite3.connect(hook.DB_PATH)
    rows = conn.execute("SELECT msg_id, task_id, input_tokens, output_tokens, total_tokens, "
                        "market, status, timestamp FROM token_usage ORDER BY msg_id").fetchall()
    conn.close()
    assert rows == [
        ("a1", "claude_code_subagent", 1110, 50, 1160, "repo-x", "success", "2026-10-25 10:00:00"),
        ("a2", "claude_code_subagent", 10, 7, 17, "repo-x", "tool_error", "2026-10-25 10:00:00"),
    ]


def test_main_dedups_via_db_even_if_state_lost(hook, tmp_path, monkeypatch):
    _make_db(hook.DB_PATH)
    t = tmp_path / "t.jsonl"
    _write_transcript(t, [_assistant("a1")])
    ev = {"session_id": "s", "transcript_path": str(t), "hook_event_name": "Stop"}
    _run_main(hook, monkeypatch, ev)
    for f in Path(hook.STATE_DIR).iterdir():
        f.unlink()
    _run_main(hook, monkeypatch, ev)
    conn = sqlite3.connect(hook.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 1
    conn.close()


def test_main_without_transcript_or_bad_stdin_exits_zero(hook, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    with pytest.raises(SystemExit) as ex:
        hook.main()
    assert ex.value.code == 0
    assert not Path(hook.DB_PATH).exists()


def test_main_writes_critical_alert_over_85_percent(hook, tmp_path, monkeypatch):
    _make_db(hook.DB_PATH)
    t = tmp_path / "t.jsonl"
    # timestamp must be "today" for the daily sum; use no timestamp -> hook uses now()
    line = _assistant("big", inp=180_000, out=0)
    del line["timestamp"]
    _write_transcript(t, [line])
    _run_main(hook, monkeypatch, {"transcript_path": str(t)})
    alerts = [json.loads(x) for x in Path(hook.ALERT_LOG).read_text().splitlines()]
    assert alerts[-1]["level"] == "CRITICAL" and alerts[-1]["tokens_used"] == 180_000
