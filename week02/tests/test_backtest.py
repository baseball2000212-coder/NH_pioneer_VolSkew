"""backtest.py: 합성 자료 규칙 검증 + 제출 폴더의 실제 입력표 재현."""
import json
from decimal import Decimal as D
from pathlib import Path

import backtest as bt
from strategy_core import StrategyConfig

ROOT = Path(__file__).resolve().parents[1]
CFG = StrategyConfig.from_dict(json.loads((ROOT / "strategy.json").read_text(encoding="utf-8")))


def row(prev="1000.0", p1519="1010.0", o1520="1010.0", c1545="1013.0", lo="1009.0", hi="1011.0", date="20261028"):
    r = {"date": date, "code": "A06612", "prev_close": prev, "p1519": p1519, "o1520": o1520, "c1545": c1545}
    for k in bt.CONT_BARS:
        r.update({f"c{k}": c1545, f"hi_{k}": hi, f"lo_{k}": lo})
    return r


KW = dict(threshold=D("3.0"), cost=D("0.2"), exit_bar="1545", stop_pt=D("10"))


def test_long_win_after_cost():
    t = bt.simulate_day(row(), **KW)
    assert (t.side, t.gross_pt, t.net_pt, t.exit_reason) == ("buy", D("3.0"), D("2.8"), "scheduled")


def test_short_when_down_and_profit_when_price_falls():
    t = bt.simulate_day(row(p1519="995.0", o1520="995.0", c1545="990.0", lo="989.0", hi="996.0"), **KW)
    assert t.side == "sell" and t.gross_pt == D("5.0")


def test_skip_over_threshold_and_exactly_three_percent():
    assert bt.simulate_day(row(p1519="1030.0"), **KW) is None
    assert bt.simulate_day(row(p1519="1029.9", o1520="1029.9", c1545="1030.0"), **KW) is not None


def test_stop_hit_exits_at_stop_level_and_is_flagged():
    t = bt.simulate_day(row(lo="999.5", c1545="1020.0"), **KW)          # 진입 1010 → 손절 1000, 저가 999.5
    assert t.exit_reason == "stop" and t.exit == D("1000.0") and t.gross_pt == D("-10.0")


def test_stop_not_hit_when_touching_only_above_and_short_side_symmetric():
    assert bt.simulate_day(row(lo="1000.1"), **KW).exit_reason == "scheduled"
    t = bt.simulate_day(row(p1519="995.0", o1520="995.0", c1545="980.0", hi="1005.0", lo="979.0"), **KW)
    assert t.exit_reason == "stop" and t.exit == D("1005.0")


def test_stop_slippage_makes_it_worse():
    t = bt.simulate_day(row(lo="999.5"), stop_slip_pt=D("1.0"), **KW)
    assert t.exit == D("999.0")


def test_stats_basics():
    trades = [bt.simulate_day(row(c1545=c), **{**KW, "stop_pt": None}) for c in ("1013.0", "1008.0", "1014.0")]
    s = bt.stats(trades, D("10000"))
    assert s["n"] == 3 and s["win_rate"] == round(2 / 3, 4) and s["max_drawdown_pt"] == 2.2 and s["worst_pt"] == -2.2


def test_shipped_input_reproduces_team_records_and_is_deterministic():
    rows = bt.read_input(ROOT / "results" / "backtest_input_daily.csv")
    result, trades = bt.run_backtest(rows, CFG)
    check = bt.compare_with_team(rows, ROOT / "results" / "team_trades_reference.csv", CFG)
    assert check["team_entries"] == check["my_entries"] == check["same_days"] == check["same_pnl"] == 167
    assert result["main"]["n"] == len(trades) == 167
    again, _ = bt.run_backtest(rows, CFG)
    assert again == result
    a = result["assumptions"]
    assert a["cost_pt_roundtrip"] == "0.5" and "낙관적" in a["stop_model"] and "만기일" in a["exclusions"]
