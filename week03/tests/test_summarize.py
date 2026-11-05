"""요약 스크립트: 읽기만 하고, run_id 로 거르고, 체결 출처를 섞지 않는다."""
from __future__ import annotations

import json

import summarize_journal as sj


def write(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


ROWS = [
    {"run_id": "a", "ts": "t1", "kind": "run", "mode": "paper-order", "phase": "start"},
    {"run_id": "a", "ts": "t2", "kind": "event", "mode": "paper-order", "name": "api_call", "api_id": "overseas_order", "http_status": 200, "msg_cd": "", "latency_ms": 120.0},
    {"run_id": "a", "ts": "t3", "kind": "event", "mode": "paper-order", "name": "api_call", "api_id": "overseas_order", "http_status": 200, "msg_cd": "E1", "latency_ms": 80.0},
    {"run_id": "a", "ts": "t4", "kind": "order", "mode": "paper-order", "stage": "ack"},
    {"run_id": "a", "ts": "t5", "kind": "fill", "mode": "paper-order", "source": "mock-account", "price": 4000},
    {"run_id": "a", "ts": "t6", "kind": "trade", "mode": "paper-order", "usd": 10.5, "source": "mock-account"},
    {"run_id": "a", "ts": "t7", "kind": "error", "mode": "paper-order", "category": "poll", "msg": "timeout"},
    {"run_id": "b", "ts": "t8", "kind": "trade", "mode": "paper-sim", "usd": -3.0, "source": "paper-sim"},
]


def test_run_id_로_거르고_출처를_나눠_요약(tmp_path):
    path = tmp_path / "j.jsonl"
    write(path, ROWS)
    recs = list(sj.read(path, "a"))
    s = sj.summarize(recs)
    assert s["run_ids"] == ["a"] and s["trades"] == 1 and s["trades_usd_total"] == 10.5
    assert s["api_calls"]["overseas_order"]["calls"] == 2 and s["api_calls"]["overseas_order"]["errors"] == 1
    assert s["fills_by_source"] == {"mock-account": 1} and s["trades_by_source"] == {"mock-account": {"n": 1, "usd_total": 10.5}}
    assert len(s["errors"]) == 1


def test_기록_파일을_고치지_않는다(tmp_path):
    path = tmp_path / "j.jsonl"
    write(path, ROWS)
    before = path.read_bytes()
    assert sj.main([str(path), "--run", "a", "--json", str(tmp_path / "s.json")]) == 0
    assert path.read_bytes() == before and (tmp_path / "s.json").exists()


def test_깨진_줄은_건너뛴다(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text('{"run_id":"a","kind":"run"}\n{깨진\n', encoding="utf-8")
    assert len(list(sj.read(path, None))) == 1
