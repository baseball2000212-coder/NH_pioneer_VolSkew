import pandas as pd

from backtest import AssetSpec, simulate, stats
from helpers import CFG, make_bars
from signals import compute_features

SPEC = AssetSpec(tick=0.25, tick_value=1.25, spread_ticks=1, fee_roundtrip_usd=4.0)
WIN = ("21:30", "23:40")


def setup(side_at=10, n=60, start="2026-11-10 21:30"):
    bars = make_bars(n, start=start)
    feats = compute_features(bars, CFG)
    feats["rv_bp"] = 5.0
    side = pd.Series(0, index=bars.index)
    side.iloc[side_at] = 1
    return bars, feats, side


def test_cost_formulas():
    assert abs(SPEC.fee_pt - 0.8) < 1e-9
    assert abs(SPEC.cost_pt("opposite") - 1.3) < 1e-9 and abs(SPEC.cost_pt("touch") - 1.05) < 1e-9


def test_entry_at_next_open_and_time_exit():
    bars, feats, side = setup()
    tr = simulate(bars, feats, side, h=3, spec=SPEC, cfg={**CFG, "stop_k": 1e6}, fill_model="opposite", window=WIN)
    t = tr.iloc[0]
    assert t.entry == bars["open"].iloc[11] and t.exit == bars["close"].iloc[13] and t.reason == "time"
    assert abs(t.net_pt - (t.gross_pt - 1.3)) < 1e-9


def test_stop_first_and_gap_exit():
    bars, feats, side = setup()
    bars = bars.copy()
    entry = bars["open"].iloc[11]
    bars.iloc[12, bars.columns.get_loc("low")] = entry - 50          # 다음 봉에서 크게 하락
    bars.iloc[12, bars.columns.get_loc("high")] = entry + 50         # 같은 봉에서 이익도 가능 → 손절 먼저
    tr = simulate(bars, feats, side, h=3, spec=SPEC, cfg=CFG, fill_model="opposite", window=WIN)
    t = tr.iloc[0]
    assert t.reason == "stop" and t.exit < entry
    bars.iloc[12, bars.columns.get_loc("open")] = entry - 100         # 시가 갭이 손절선 아래 → 시가 청산
    tr = simulate(bars, feats, side, h=3, spec=SPEC, cfg=CFG, fill_model="opposite", window=WIN)
    assert tr.iloc[0].exit == bars["open"].iloc[12]


def test_no_entry_that_crosses_flatten_or_has_gap():
    bars, feats, side = setup(side_at=3, n=40, start="2026-11-10 23:15")  # 신호 23:30 → 진입 23:35, 청산 23:50 > 23:40
    tr = simulate(bars, feats, side, h=3, spec=SPEC, cfg=CFG, fill_model="opposite", window=WIN)
    assert tr.empty
    bars2, feats2, side2 = setup()
    bars2 = bars2.drop(bars2.index[12])                                   # 보유 구간에 공백 → 진입 안 함
    tr = simulate(bars2, feats2.loc[bars2.index], side2.loc[bars2.index], h=3, spec=SPEC, cfg=CFG, fill_model="opposite", window=WIN)
    assert tr.empty


def test_one_position_at_a_time_and_stats():
    bars, feats, side = setup()
    side.iloc[10:14] = 1                                                  # 연속 신호 → 보유 중에는 무시
    tr = simulate(bars, feats, side, h=3, spec=SPEC, cfg={**CFG, "stop_k": 1e6}, fill_model="touch", window=WIN)
    assert len(tr) >= 1
    if len(tr) > 1:
        assert (tr.entry_ts.iloc[1:].reset_index(drop=True) >= tr.exit_ts.iloc[:-1].reset_index(drop=True) - pd.Timedelta(minutes=5)).all()
    st = stats(tr, reps=50)
    assert st["n"] == len(tr) and 0 <= st["win_rate"] <= 1
    assert stats(pd.DataFrame()) == {"n": 0}
