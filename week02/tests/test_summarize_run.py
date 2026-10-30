import json
import subprocess
import sys
from pathlib import Path

import summarize_journal as sj

ROOT = Path(__file__).resolve().parents[1]


def write(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n\n{broken\n", encoding="utf-8")


def rec(kind, run="r1", **kw):
    return {"run_id": run, "ts": "2026-10-28T15:20:00.000+09:00", "seq": 1, "env": "paper", "mode": "paper-sim", "sym": "A066C000", "kind": kind, **kw}


def test_summary_counts_filters_run_and_skips_broken_lines(tmp_path):
    p = tmp_path / "j.jsonl"
    write(p, [
        rec("signal", side="buy", reason="enter", price="1010.0", prev_close="1000.0", ret_pct="1.0"),
        rec("trade", side="buy", qty=1, entry_price="1010.1", exit_price="1012.0", gross_pt="1.9", net_pt="1.7", net_won="17000", source="sim"),
        rec("error", category="order_rejected", msg="x", action="retry"),
        rec("event", name="api_call", api_id="domestic_price", http_status=200, msg_cd="", latency_ms=120.0),
        rec("event", name="api_call", api_id="domestic_price", http_status=200, msg_cd="", latency_ms=80.0),
        rec("signal", run="other", side="sell", reason="enter"),
    ])
    s = sj.summarize(list(sj.read_lines(p, "r1")))
    assert s["run_ids"] == ["r1"] and s["lines"] == 5 and s["labels"] == ["[자체 시뮬레이션(종이 체결)]"]
    assert s["trade_stats"]["sum_net_pt"] == 1.7 and s["api_calls"]["domestic_price"]["n"] == 2
    assert s["api_calls"]["domestic_price"]["latency_ms_median"] == 100.0 and len(s["errors"]) == 1


def test_main_returns_1_when_no_such_run(tmp_path):
    p = tmp_path / "j.jsonl"
    write(p, [rec("signal")])
    assert sj.main([str(p), "--run", "nope"]) == 1


def test_run_py_refuses_orders_without_confirmation():
    proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "--env", "paper", "--mode", "paper-order", "--run-id", "x"], capture_output=True, text=True, encoding="utf-8", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 2 and "--confirm-orders" in proc.stderr


def test_run_py_rejects_real_env():
    proc = subprocess.run([sys.executable, str(ROOT / "run.py"), "--env", "real", "--mode", "backtest"], capture_output=True, text=True)
    assert proc.returncode != 0
