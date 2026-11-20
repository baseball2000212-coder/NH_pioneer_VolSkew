import numpy as np
import pandas as pd

from helpers import CFG, make_bars
from signals import FEATURES, SignalModel, T2Chart, compute_features, decide, forward_pnl, in_blackout, in_window, make_labels


def test_features_do_not_look_ahead():
    bars = make_bars(300)
    full = compute_features(bars, CFG)
    part = compute_features(bars.iloc[:200], CFG)
    cols = list(FEATURES)
    pd.testing.assert_frame_equal(full.iloc[:200][cols], part[cols])      # 뒤 봉이 늘어도 앞 봉의 특징은 같다


def test_session_gap_resets_window():
    bars = make_bars(120)
    idx = bars.index.to_series()
    idx.iloc[60:] = idx.iloc[60:] + pd.Timedelta(hours=3)
    gap = bars.copy()
    gap.index = pd.DatetimeIndex(idx.values, name="ts")
    f = compute_features(gap, CFG)
    assert not f["ok"].iloc[60]                                           # 공백 뒤 첫 봉은 창이 비어 신호 제외
    assert f["ok"].iloc[59]


def test_forward_pnl_requires_contiguity_and_labels():
    bars = make_bars(30)
    fwd = forward_pnl(bars, 3, CFG)
    assert fwd.iloc[0] == bars["close"].iloc[3] - bars["open"].iloc[1]
    assert fwd.iloc[-3:].isna().all()
    lab = make_labels(pd.Series([5.0, -5.0, 0.5, np.nan]), 2.0)
    assert lab.tolist()[:3] == [1, -1, 0] and np.isnan(lab.iloc[3])


def test_t2_contributions_sum_to_t2_and_ucl_quantile():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(500, 6))
    chart = T2Chart.fit(x, 0.95)
    t2 = chart.t2(x)
    assert np.allclose(chart.contributions(x).sum(axis=1), t2)
    assert abs((t2 <= chart.ucl).mean() - 0.95) < 0.01


def test_model_json_roundtrip_same_predictions(tmp_path):
    rng = np.random.default_rng(1)
    x = rng.normal(size=(600, 6))
    y = np.where(x[:, 0] > 0.5, 1, np.where(x[:, 0] < -0.5, -1, 0))
    m = SignalModel.fit(x, y, CFG, {"h": 3})
    path = tmp_path / "m.json"
    m.to_json(path)
    m2 = SignalModel.from_json(path)
    assert (m.predict(x) == m2.predict(x)).all() and np.allclose(m.t2(x), m2.t2(x))
    assert (m.predict(x) == y).mean() > 0.8


def test_windows_and_blackout():
    idx = pd.DatetimeIndex(["2026-11-10 21:25", "2026-11-10 21:30", "2026-11-10 23:39", "2026-11-10 23:40"])
    assert in_window(idx, "21:30", "23:40").tolist() == [False, True, True, False]
    # 표준시(11월): 08:30 ET = 22:30 KST. 서머타임(8월)에는 1시간 앞당겨진다 (21:30 KST).
    b = pd.DatetimeIndex(["2026-11-10 22:20", "2026-11-10 22:30", "2026-11-10 22:39", "2026-11-10 22:40", "2026-08-10 21:30", "2026-08-10 22:30"])
    assert in_blackout(b, CFG).tolist() == [False, True, True, False, True, True]


def test_decide_gate_blocks_out_of_control():
    bars = make_bars(250)
    feats = compute_features(bars, CFG)
    ok = feats["ok"].to_numpy()
    x = feats.loc[ok, list(FEATURES)].to_numpy()
    y = np.where(x[:, 4] > 1, -1, np.where(x[:, 4] < -1, 1, 0))
    m = SignalModel.fit(x, y, CFG)
    m.chart.ucl = -1.0                                                    # 모든 점이 관리 상한 밖
    assert (decide(m, feats, CFG, use_gate=True)["side"] == 0).all()
    assert (decide(m, feats, CFG, use_gate=False)["svm_side"] != 0).any()
