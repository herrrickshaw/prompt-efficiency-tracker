"""Additional tests for hooks/claude-code-usage-hook.py: alert thresholds, market inference, DB failure
isolation and state bounding. Every path is redirected into tmp_path."""
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "claude-code-usage-hook.py"


@pytest.fixture
def hook(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("usage_hook_extra", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(mod, "ALERT_LOG", str(tmp_path / "logs" / "alerts.log"))
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "usage.db"))
    return mod


def make_db(path, with_table=True):
    conn = sqlite3.connect(path)
    if with_table:
        conn.execute("""CREATE TABLE token_usage (id INTEGER PRIMARY KEY, timestamp TEXT, task_id TEXT,
                        task_name TEXT, model TEXT, input_tokens INT, output_tokens INT, total_tokens INT,
                        cost REAL, market TEXT, status TEXT, duration_seconds REAL)""")
    conn.commit()
    conn.close()


def transcript(path, *msgs):
    lines = []
    for mid, inp in msgs:
        lines.append({"type": "assistant", "message": {"id": mid, "model": "claude-sonnet-4-6",
                                                       "usage": {"input_tokens": inp, "output_tokens": 0}}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def run_main(hook, monkeypatch, event, market="repo"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event) if not isinstance(event, str) else event))
    monkeypatch.setattr(hook, "_infer_market", lambda e: market)
    with pytest.raises(SystemExit) as ex:
        hook.main()
    return ex.value.code


def alerts(hook):
    p = Path(hook.ALERT_LOG)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- alert thresholds ---------------------------------------------------------------------------

@pytest.mark.parametrize("tokens,level", [
    (139_999, None), (140_000, "WARNING"), (169_999, "WARNING"), (170_000, "CRITICAL"), (250_000, "CRITICAL"),
])
def test_alert_thresholds_are_70_and_85_percent_of_the_daily_limit(hook, tmp_path, monkeypatch, tokens, level):
    make_db(hook.DB_PATH)
    t = tmp_path / "t.jsonl"
    transcript(t, ("m", tokens))
    assert run_main(hook, monkeypatch, {"transcript_path": str(t)}) == 0
    got = alerts(hook)
    if level is None:
        assert got == []
    else:
        assert [a["level"] for a in got] == [level]
        a = got[0]
        assert a["tokens_used"] == tokens and a["tokens_remaining"] == 200_000 - tokens
        assert a["percentage_used"] == round(tokens / 200_000 * 100) and a["daily_limit"] == 200_000
        assert a["task_id"] == "claude_code"


def test_alert_sums_the_whole_day_not_just_this_turn(hook, tmp_path, monkeypatch):
    make_db(hook.DB_PATH)
    t1, t2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    transcript(t1, ("m1", 100_000))
    transcript(t2, ("m2", 50_000))
    run_main(hook, monkeypatch, {"transcript_path": str(t1)})
    assert alerts(hook) == []
    run_main(hook, monkeypatch, {"transcript_path": str(t2)})
    assert [a["level"] for a in alerts(hook)] == ["WARNING"] and alerts(hook)[0]["tokens_used"] == 150_000


def test_write_alert_creates_log_dir_and_appends_json_lines(hook):
    hook._write_alert("WARNING", "tid", 10, 190, 5.4)
    hook._write_alert("CRITICAL", "tid", 20, 180, 10.0)
    got = alerts(hook)
    assert [a["level"] for a in got] == ["WARNING", "CRITICAL"]
    assert got[0]["percentage_used"] == 5 and got[0]["timestamp"].endswith("+00:00")


# ---- main(): task labels, failures --------------------------------------------------------------

def test_stop_vs_subagent_task_ids(hook, tmp_path, monkeypatch):
    make_db(hook.DB_PATH)
    t1, t2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    transcript(t1, ("a", 5))
    transcript(t2, ("b", 5))
    run_main(hook, monkeypatch, {"transcript_path": str(t1), "hook_event_name": "Stop"})
    run_main(hook, monkeypatch, {"transcript_path": str(t2), "hook_event_name": "SubagentStop"})
    conn = sqlite3.connect(hook.DB_PATH)
    rows = dict((r[0], r[1:]) for r in conn.execute("SELECT msg_id, task_id, task_name FROM token_usage"))
    conn.close()
    assert rows == {"a": ("claude_code", "Claude Code turn"), "b": ("claude_code_subagent", "Claude Code subagent turn")}


def test_missing_transcript_path_or_file_exits_zero_without_touching_db(hook, tmp_path, monkeypatch):
    assert run_main(hook, monkeypatch, {}) == 0
    assert run_main(hook, monkeypatch, {"transcript_path": str(tmp_path / "nope.jsonl")}) == 0
    assert run_main(hook, monkeypatch, "   ") == 0
    assert not Path(hook.DB_PATH).exists() and not Path(hook.STATE_DIR).exists()


def test_transcript_with_only_zero_token_messages_writes_nothing(hook, tmp_path, monkeypatch):
    make_db(hook.DB_PATH)
    t = tmp_path / "t.jsonl"
    transcript(t, ("z", 0))
    run_main(hook, monkeypatch, {"transcript_path": str(t)})
    conn = sqlite3.connect(hook.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 0
    conn.close()
    assert not Path(hook.STATE_DIR).exists()                       # nothing logged -> no cursor saved either


def test_db_error_is_reported_on_stderr_and_does_not_block(hook, tmp_path, monkeypatch, capsys):
    make_db(hook.DB_PATH, with_table=False)                       # no token_usage table: ALTER TABLE fails
    t = tmp_path / "t.jsonl"
    transcript(t, ("m", 5))
    assert run_main(hook, monkeypatch, {"transcript_path": str(t)}) == 0
    assert "[ruflo-usage-hook] db error:" in capsys.readouterr().err
    assert not Path(hook.STATE_DIR).exists()                       # cursor NOT advanced, so the turn is retried
    assert alerts(hook) == []


def test_unopenable_db_path_is_reported_not_raised(hook, tmp_path, monkeypatch, capsys):
    Path(hook.DB_PATH).mkdir()                                     # a directory where the DB file should be
    t = tmp_path / "t.jsonl"
    transcript(t, ("m", 5))
    assert run_main(hook, monkeypatch, {"transcript_path": str(t)}) == 0
    assert "db error" in capsys.readouterr().err


def test_retry_after_db_error_still_logs_the_messages(hook, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    transcript(t, ("m", 5))
    make_db(hook.DB_PATH, with_table=False)
    run_main(hook, monkeypatch, {"transcript_path": str(t)})
    Path(hook.DB_PATH).unlink()
    make_db(hook.DB_PATH)
    run_main(hook, monkeypatch, {"transcript_path": str(t)})
    conn = sqlite3.connect(hook.DB_PATH)
    assert [r[0] for r in conn.execute("SELECT msg_id FROM token_usage")] == ["m"]
    conn.close()


def test_cost_and_row_values_for_a_known_model(hook, tmp_path, monkeypatch):
    make_db(hook.DB_PATH)
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-25T10:00:00Z", "message": {
        "id": "m", "model": "claude-opus-5-5-20261001",
        "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                  "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}}}) + "\n")
    run_main(hook, monkeypatch, {"transcript_path": str(t)})
    conn = sqlite3.connect(hook.DB_PATH)
    model, inp, out, total, cost = conn.execute("SELECT model, input_tokens, output_tokens, total_tokens, cost FROM token_usage").fetchone()
    conn.close()
    assert (inp, out, total) == (3_000_000, 1_000_000, 4_000_000)            # cache tokens count towards input
    assert cost == pytest.approx(4 * (1 + 1.25 + 0.10) + 20)                 # Opus 5.5: $4 in / $20 out per MTok
    assert model == "claude-opus-5-5-20261001"


@pytest.mark.parametrize("model,in_price", [("claude-opus-5-5", 4e-6), ("claude-opus-5-1", 5e-6), ("claude-sonnet-5-5", 2e-6),
                                             ("claude-sonnet-4-5", 3e-6), ("CLAUDE-HAIKU-5-5", 0.1e-6), ("claude-haiku-4-5", 1e-6),
                                             ("claude-mythos-1", 10e-6)])
def test_price_table_prefers_the_most_specific_entry(hook, model, in_price):
    assert hook._price_for(model)["in"] == in_price


def test_estimate_cost_zero_tokens_is_zero(hook):
    assert hook._estimate_cost("claude-opus-5-5", 0, 0, 0, 0) == 0


# ---- state ----------------------------------------------------------------------------------------

def test_save_state_is_bounded_and_leaves_no_tmp_file(hook):
    hook._save_state("k", {f"id{i}" for i in range(6000)})
    assert len(hook._load_state("k")) == 5000
    assert sorted(p.name for p in Path(hook.STATE_DIR).iterdir()) == ["k.json"]
    hook._save_state("k", {"only"})
    assert hook._load_state("k") == {"only"}


# ---- _infer_market -------------------------------------------------------------------------------

class Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout


def test_infer_market_uses_git_toplevel_name(hook, monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or Proc(0, "/work/market-pipeline\n"))
    assert hook._infer_market({"cwd": "/work/market-pipeline/sub"}) == "market-pipeline"
    assert seen["cmd"] == ["git", "-C", "/work/market-pipeline/sub", "rev-parse", "--show-toplevel"]
    assert seen["kw"]["timeout"] == 3


@pytest.mark.parametrize("proc", [Proc(128, ""), Proc(0, "  \n")])
def test_infer_market_falls_back_to_cwd_name_when_git_has_no_answer(hook, monkeypatch, proc):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    assert hook._infer_market({"cwd": "/work/some-repo/"}) == "some-repo"          # trailing slash tolerated


def test_infer_market_falls_back_when_git_is_missing_or_times_out(hook, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", boom)
    assert hook._infer_market({"cwd": "/work/x"}) == "x"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 3)))
    assert hook._infer_market({"cwd": "/work/y"}) == "y"


def test_infer_market_unknown_for_root_and_default_cwd(hook, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc(1, ""))
    assert hook._infer_market({"cwd": "/"}) == "unknown"
    monkeypatch.chdir("/")
    assert hook._infer_market({}) == "unknown"
    assert hook._infer_market({"cwd": ""}) == "unknown"
