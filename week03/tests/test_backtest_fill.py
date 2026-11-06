"""백테스트 체결 모델(level·close·next_open), 기본값, 판단이 필요한 동작의 설정 플래그 — API 없이."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest import BarFillExecutor, fill_model_of, run_backtest, summarize
from feeds import ZtSeries, save_csv
from gate_store import GateStore
from nhapi.risk import RiskLimits, RiskManager
from strategy_core import Bar, DayEngine, Intent
from helpers import PARAMS, breakout_up, flat_day, kst, lift, put

DAY = (2026, 11, 4)
P, COSTS = PARAMS["params"], PARAMS["costs"]


def bars_pair():
    b0 = Bar(kst(*DAY, 17, 0), 4000.5, 4000.7, 4000.2, 4000.9)
    b1 = Bar(kst(*DAY, 17, 1), 4001.0, 4001.2, 4000.8, 4001.1)
    return [b0, b1]


@pytest.mark.parametrize("model, expected", [("level", 4000.6), ("close", 4000.9), ("next_open", 4001.0)])
def test_돌파_진입_체결가는_모델마다_다르다(model, expected):
    ex = BarFillExecutor(0.1, 0, model)
    ex.set_day(bars_pair())
    it = Intent("enter", 2, 1, 4000.6, "돌파", kst(*DAY, 17, 0), at_level=True)
    assert ex.execute(it, 1).price == pytest.approx(expected)


def test_슬리피지는_모델_위에_불리하게_더한다():
    ex = BarFillExecutor(0.1, 1, "close")
    ex.set_day(bars_pair())
    long_stop = Intent("exit", 2, 1, 4000.0, "손절", kst(*DAY, 17, 0), at_level=True)
    assert ex.execute(long_stop, 1).price == pytest.approx(4000.9 - 0.1)       # 롱 청산(매도)은 싸게


def test_봉_시가_기준가는_어느_모델이든_그대로():
    for model in ("level", "close", "next_open"):
        ex = BarFillExecutor(0.1, 0, model)
        ex.set_day(bars_pair())
        it = Intent("exit", 3, 1, 4000.5, "시간 청산", kst(*DAY, 17, 0))       # at_level=False
        assert ex.execute(it, 1).price == pytest.approx(4000.5)


def test_next_open_은_그날_마지막_봉이면_종가():
    ex = BarFillExecutor(0.1, 0, "next_open")
    ex.set_day(bars_pair())
    it = Intent("enter", 2, 1, 4001.0, "돌파", kst(*DAY, 17, 1), at_level=True)
    assert ex.execute(it, 1).price == pytest.approx(4001.1)


def test_엔진은_수준가인지_시가인지_표시한다():
    bars = flat_day()
    breakout_up(bars)                                         # 시가 4000.5 < 돌파선 4000.6 → 수준가 체결
    eng = DayEngine(kst(*DAY, 0).date(), P, COSTS, threshold=0.0, zt_move=lambda e, m: 0.001)
    out = []
    for t in sorted(bars):
        out += eng.on_bar(bars[t], True)
        if out and out[-1].kind == "exit":
            break
    assert out[0].at_level is True and out[1].at_level is False   # 23:50 시간 청산은 봉 시가
    bars = flat_day()
    lift(bars, kst(*DAY, 17, 0), 2.0)                          # 17:00 시가 4002 로 갭 → 시가 체결
    eng = DayEngine(kst(*DAY, 0).date(), P, COSTS, threshold=0.0, zt_move=lambda e, m: 0.001)
    first = next(it for t in sorted(bars) for it in eng.on_bar(bars[t], True))
    assert first.ref_price == pytest.approx(4002.0) and first.at_level is False


def _cli(tmp_path, extra):
    import run as cli
    bars = flat_day()
    breakout_up(bars)
    put(bars, kst(*DAY, 17, 0), c=4000.9)
    save_csv(tmp_path / "bars.csv", sorted(bars.values(), key=lambda b: b.ts))
    save_csv(tmp_path / "zt.csv", [Bar(kst(*DAY, 16, 0) + timedelta(minutes=i), 100, 100, 100, 100 + i * 0.01) for i in range(-60, 400)])
    seed = tmp_path / "seed.csv"
    seed.write_text("day,cn_rv\n" + "".join(f"{date(2025, 1, 1) + timedelta(days=i)},1e-12\n" for i in range(130)), encoding="utf-8")
    out = tmp_path / "res"
    assert cli.main(["--env", "paper", "--mode", "backtest", "--run-id", "t", "--bars", str(tmp_path / "bars.csv"), "--zt", str(tmp_path / "zt.csv"),
                     "--seed", str(seed), "--out-dir", str(out)] + extra) == 0
    trades = (out / "t_trades.csv").read_text(encoding="utf-8").splitlines()
    return dict(zip(trades[0].split(","), trades[1].split(","))), json.loads((out / "t_assumptions.json").read_text(encoding="utf-8"))


def test_CLI_기본은_수준가와_1틱(tmp_path):
    trade, assumptions = _cli(tmp_path, [])
    assert float(trade["entry"]) == pytest.approx(4000.6 + 0.1)
    assert assumptions["config"]["costs"]["slippage_ticks"] == 1 and assumptions["fill_model"].startswith("[level]")


def test_CLI_close_면_감지봉_종가에_1틱(tmp_path):
    trade, assumptions = _cli(tmp_path, ["--fill-model", "close"])
    assert float(trade["entry"]) == pytest.approx(4000.9 + 0.1) and assumptions["fill_model"].startswith("[close]")


def test_CLI_level_0틱이면_원래_방식(tmp_path):
    trade, assumptions = _cli(tmp_path, ["--fill-model", "level", "--slippage-ticks", "0"])
    assert float(trade["entry"]) == pytest.approx(4000.6) and assumptions["fill_model"].startswith("[level]")


def test_설정이_없으면_level_이고_모르는_모델은_거부():
    assert fill_model_of({}) == "level"
    with pytest.raises(ValueError):
        fill_model_of({"backtest": {"fill_model": "mid"}})


def test_요약에_규칙2_건너뜀_사유와_규칙3_늦은_진입():
    daily = [dict(day="d1", pnl=0.0, n_trades=0, gate_pass=True, notes="규칙 2: 범위 봉 120개 < 200 → 건너뜀"),
             dict(day="d2", pnl=0.0, n_trades=0, gate_pass=True, notes="규칙 2: 매크로(ZT) 확인 불일치 → 건너뜀 (변화 None)"),
             dict(day="d3", pnl=0.0, n_trades=0, gate_pass=True, notes="규칙 2: 매크로(ZT) 확인 불일치 → 건너뜀 (변화 -0.001)"),
             dict(day="d4", pnl=10.0, n_trades=2, gate_pass=True, notes="")]
    trades = [dict(rule=3, usd=5.0, entry_ts=kst(*DAY, 22, 30).isoformat()), dict(rule=3, usd=5.0, entry_ts=kst(*DAY, 22, 34).isoformat())]
    d = summarize(trades, daily)["diagnostics"]
    assert d["rule2_skip_days"] == dict(range_bars_short=1, zt_missing=1, zt_mismatch=1, both_sides=0)
    assert d["rule3_late_entries"] == 1 and d["rule3_trades"] == 2


# ------------------------------------------------------------------ 판단이 필요한 동작: 플래그, 기본은 원래 동작


def test_기본_설정은_원래_동작을_유지한다():
    assert P["rule3"]["skip_us_winter"] is False and P["gate"]["label"] == "아시아 시간대 금 RV"
    assert PARAMS["risk"]["stop_gap_buffer_usd"] == 0


def _rule3_day(day):
    bars = flat_day(day=day, start=(7, 0))
    ny_entry = (22, 29) if day[1] in (11, 12, 1, 2) else (21, 29)
    put(bars, kst(*day, *ny_entry), c=4002.0)
    return bars


def _run(eng, bars):
    log = []
    for t in sorted(bars):
        for it in eng.on_bar(bars[t], True):
            log.append(it)
            if it.kind == "exit":
                eng.apply_exit(it, it.ref_price, it.ts)
    return log


def test_skip_us_winter_를_켜면_겨울_규칙3만_쉰다():
    p = json.loads(json.dumps(P))
    p["rule3"]["skip_us_winter"] = True
    winter = DayEngine(date(*DAY), p, COSTS, threshold=0.0, zt_move=lambda e, m: 0.0)
    assert _run(winter, _rule3_day(DAY)) == [] and any("미국 겨울" in n for n in winter.notes)
    summer = DayEngine(date(2026, 7, 15), p, COSTS, threshold=0.0, zt_move=lambda e, m: 0.0)
    assert [i.rule for i in _run(summer, _rule3_day((2026, 7, 15)))] == [3, 3]
    default = DayEngine(date(*DAY), P, COSTS, threshold=0.0, zt_move=lambda e, m: 0.0)
    assert [i.rule for i in _run(default, _rule3_day(DAY))] == [3, 3]


def test_게이트_이름은_기록용_문구만_바꾼다():
    p = json.loads(json.dumps(P))
    p["gate"]["label"] = "변동성 수준 필터(금 RV)"
    eng = DayEngine(date(*DAY), p, COSTS, threshold=0.0, zt_move=lambda e, m: 0.0)
    _run(eng, flat_day())
    assert eng.gate_note.startswith("변동성 수준 필터") and eng.gate_pass is True


def test_갭_여유를_주면_일일_한도_점검이_엄해진다():
    limits = RiskLimits(max_position=1, daily_loss_limit=Decimal(800), session_end_flat_minutes=5)
    for buffer, trades in ((0, 2), (100, 0)):
        eng = DayEngine(date(*DAY), P, COSTS, threshold=0.0, zt_move=lambda e, m: 0.0, risk=RiskManager(limits),
                        rule3_emergency_stop_usd=300, stop_gap_buffer_usd=buffer)
        eng.realized = -500.0                                   # 남은 한도 300 = 비상 손절 300
        assert len(_run(eng, _rule3_day(DAY))) == trades
