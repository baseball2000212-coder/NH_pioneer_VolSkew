"""그래프: results/ 의 기록만 읽고, 그림마다 환경 표기·근거 파일·'화면 캡처 아님'이 들어가며, 파일이 없으면 건너뛴다."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import chartkit as ck
import charts
from feeds import save_csv
from helpers import breakout_up, flat_day, kst
from strategy_core import Bar

HERE = Path(__file__).resolve().parents[1]
DAY = (2026, 11, 4)


def test_누적과_낙폭_계산():
    cum = ck.cumulative([10, -5, -10, 20])
    assert cum == [10, 5, -5, 15]
    assert ck.drawdown(cum) == [0, -5, -15, 0]
    assert ck.money(-3.4) == "-$3" and ck.money(1234.5) == "+$1,234"


def test_제출_폴더의_결과로_그래프를_그린다(tmp_path):
    made = charts.plot_all(results=HERE / "results", out=tmp_path)
    names = {p.name for p in made}
    assert {"rule_timeline.png", "backtest_overview.png", "gate_distribution.png", "smoke_orders.png"} <= names
    assert all(p.stat().st_size > 10_000 for p in made)


def test_파일이_없으면_건너뛴다(tmp_path):
    made = charts.plot_all(only={"backtest", "smoke"}, results=tmp_path, out=tmp_path / "c")
    assert made == []


def test_본_실행_그래프는_기록과_분봉으로_그린다(tmp_path):
    bars = flat_day()
    breakout_up(bars)
    save_csv(tmp_path / "sim-x_bars.csv.gz", [b for _, b in sorted(bars.items())])
    rows = [
        {"run_id": "sim-x", "ts": kst(*DAY, 15, 59).isoformat(), "mode": "paper-sim", "kind": "run", "phase": "start"},
        {"run_id": "sim-x", "ts": kst(*DAY, 16, 0).isoformat(), "mode": "paper-sim", "kind": "event", "name": "gate", "gate_pass": True},
        {"run_id": "sim-x", "ts": kst(*DAY, 17, 0).isoformat(), "mode": "paper-sim", "kind": "fill", "source": "paper-sim", "action": "enter", "price": 4000.6, "bar_ts": kst(*DAY, 17, 0).isoformat()},
        {"run_id": "sim-x", "ts": kst(*DAY, 23, 50).isoformat(), "mode": "paper-sim", "kind": "fill", "source": "paper-sim", "action": "exit", "price": 4001.0, "bar_ts": kst(*DAY, 23, 50).isoformat()},
        {"run_id": "sim-x", "ts": kst(*DAY, 23, 50).isoformat(), "mode": "paper-sim", "kind": "trade", "usd": 0.5},
        {"run_id": "sim-x", "ts": kst(*DAY, 15, 59).isoformat(), "mode": "paper-sim", "kind": "event", "name": "replay", "missed": [{"entry_ts": kst(*DAY, 16, 0).isoformat(), "entry": 4000.0}]},
    ]
    (tmp_path / "sim-x.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    made = charts.plot_all(only={"live"}, results=tmp_path, out=tmp_path / "c")
    assert [p.name for p in made] == ["live_sim-x.png"] and made[0].stat().st_size > 10_000


def test_그림마다_환경_표기와_캡처_아님을_적는다():
    from unittest import mock

    class FakeFig:
        texts = []

        def text(self, *a, **k):
            self.texts.append(a[2])

    fig = FakeFig()
    ck.stamp(fig, "paper-order", ["a.jsonl"], "메모")
    assert "[모의계좌 주문]" in fig.texts[0] and "a.jsonl" in fig.texts[0] and "화면 캡처가 아닙니다" in fig.texts[0] and "메모" in fig.texts[0]
    fig2 = FakeFig()
    fig2.texts = []
    ck.stamp(fig2, "paper-sim", ["b"])
    assert "[자체 시뮬레이션(종이 체결)]" in fig2.texts[0]


def test_그래프는_screenshots_에도_번호_이름으로_복사된다(tmp_path):
    shots = tmp_path / "shots"
    made = charts.plot_all(only={"timeline", "smoke"}, results=HERE / "results", out=tmp_path / "c", shots=shots)
    assert {p.name for p in shots.iterdir()} == {"01-rule_timeline.png", "04-smoke_orders.png"}
    assert len(made) == 2
