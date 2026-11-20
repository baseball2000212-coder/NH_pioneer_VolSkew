"""신호 계산: 지표(특징) → Hotelling T² 관리도(국면 게이트) → 선형 SVM 롱/숏/관망.

백테스트와 라이브 엔진이 **같은 함수**를 쓴다. 학습(sklearn)과 추론(numpy 만)을 나눠서, 학습한 모형은
JSON(`model_*.json`)으로 저장하고 엔진은 JSON 만 읽어 내적한다 → 신호 재현·라이브-리플레이 일치 검사가 쉽다.

특징(5분봉 완성 시점에 알 수 있는 값만):
  r1        직전 5분 로그수익률(bp)          log_rv    직전 n_rv 봉 r1 표준편차의 로그
  range_rel 이 봉 고저폭 / 최근 n_ref 봉 중앙값의 로그   vol_rel  이 봉 거래량 / 최근 n_ref 봉 중앙값의 로그
  z         (종가 − n_z 봉 평균) / 표준편차  mom      n_mom 봉 누적수익률 / (σ·√n_mom)
세션 사이 공백(`reset_gap_min` 초과)은 롤링 창을 끊는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FEATURES = ("r1", "log_rv", "range_rel", "vol_rel", "z", "mom")
LABELS = (-1, 0, 1)  # 숏, 관망, 롱


# ------------------------------------------------------------------ 특징

def compute_features(bars: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    """5분봉 → 특징표. `ok` 열이 False 인 줄은 신호에 쓰지 않는다(창이 덜 찼거나 세션 첫 구간)."""
    f = cfg["features"]
    seg = (bars.index.to_series().diff().dt.total_seconds().div(60) > f["reset_gap_min"]).cumsum()
    out = pd.DataFrame(index=bars.index)
    close = bars["close"]
    logc = np.log(close)
    r1 = logc.groupby(seg).diff() * 1e4
    out["r1"] = r1

    def roll(series: pd.Series, n: int, fn: str) -> pd.Series:
        return getattr(series.groupby(seg).rolling(n, min_periods=n), fn)().reset_index(level=0, drop=True)

    rv = roll(r1, f["n_rv"], "std")
    out["log_rv"] = np.log(rv.clip(lower=1e-6))
    rng = (bars["high"] - bars["low"]) / close * 1e4 + 1e-6
    out["range_rel"] = np.log(rng) - np.log(roll(rng, f["n_ref"], "median"))
    lv = np.log1p(bars["volume"])
    out["vol_rel"] = lv - roll(lv, f["n_ref"], "median")
    mean_z, std_z = roll(close, f["n_z"], "mean"), roll(close, f["n_z"], "std")
    out["z"] = (close - mean_z) / std_z.replace(0, np.nan)
    out["mom"] = roll(r1, f["n_mom"], "sum") / (rv * np.sqrt(f["n_mom"])).replace(0, np.nan)
    out["rv_bp"] = rv                      # 손절 거리 계산용 (특징 아님)
    out["ok"] = out[list(FEATURES)].notna().all(axis=1)
    return out


def forward_pnl(bars: pd.DataFrame, h: int, cfg: dict[str, Any]) -> pd.Series:
    """신호 봉 t 에서 진입(t+1 시가) → t+h 종가까지 보유했을 때의 롱 기준 총손익(pt). 연속하지 않으면 NaN."""
    step = pd.Timedelta(minutes=cfg["bar_minutes"])
    idx = bars.index
    open_next = bars["open"].shift(-1)
    close_h = bars["close"].shift(-h)
    contiguous = (idx.to_series().shift(-h) - idx.to_series()) == step * h
    return (close_h - open_next).where(contiguous)


def make_labels(fwd: pd.Series, thr_pt: float) -> pd.Series:
    """롱 +1: 총손익 > thr, 숏 −1: < −thr, 그 사이 0(관망). 라벨은 진입 규칙과 같은 시각 기준."""
    lab = pd.Series(0, index=fwd.index, dtype="float")
    lab[fwd > thr_pt] = 1
    lab[fwd < -thr_pt] = -1
    return lab.where(fwd.notna())


# ------------------------------------------------------------------ Hotelling T² 관리도

@dataclass
class T2Chart:
    mean: np.ndarray
    precision: np.ndarray
    ucl: float

    @classmethod
    def fit(cls, x: np.ndarray, quantile: float) -> "T2Chart":
        """in-sample 평소 상태에서 평균·공분산(Ledoit-Wolf 수축)을 추정하고, UCL = T² 의 경험적 분위수.

        분봉 특징은 자기상관·비정규라 이론 F 분포 상한 대신 경험적 분위수를 쓴다.
        """
        from sklearn.covariance import LedoitWolf
        lw = LedoitWolf().fit(x)
        chart = cls(mean=x.mean(axis=0), precision=lw.precision_, ucl=0.0)
        chart.ucl = float(np.quantile(chart.t2(x), quantile))
        return chart

    def t2(self, x: np.ndarray) -> np.ndarray:
        d = np.atleast_2d(x) - self.mean
        return np.einsum("ij,jk,ik->i", d, self.precision, d)

    def contributions(self, x: np.ndarray) -> np.ndarray:
        """T² 를 변수별 몫으로 분해 (합 = T²). 음수 몫도 있을 수 있다(상관 때문)."""
        d = np.atleast_2d(x) - self.mean
        return d * (d @ self.precision)


# ------------------------------------------------------------------ 선형 SVM

@dataclass
class SignalModel:
    """학습한 표준화·SVM·T² 를 한 묶음으로. `predict` 는 numpy 만 쓴다."""

    features: tuple[str, ...]
    mu: np.ndarray
    sd: np.ndarray
    classes: np.ndarray
    coef: np.ndarray
    intercept: np.ndarray
    chart: T2Chart
    meta: dict[str, Any]

    @classmethod
    def fit(cls, x: np.ndarray, y: np.ndarray, cfg: dict[str, Any], meta: dict[str, Any] | None = None) -> "SignalModel":
        from sklearn.svm import LinearSVC
        mu, sd = x.mean(axis=0), x.std(axis=0)
        sd = np.where(sd == 0, 1.0, sd)
        z = (x - mu) / sd
        m = cfg["model"]
        clf = LinearSVC(C=m["C"], class_weight="balanced", max_iter=m["max_iter"], random_state=m["seed"], dual="auto")
        clf.fit(z, y)
        chart = T2Chart.fit(x, cfg["t2"]["ucl_quantile"])
        return cls(tuple(FEATURES), mu, sd, clf.classes_.astype(int), clf.coef_, clf.intercept_, chart, meta or {})

    def scores(self, x: np.ndarray) -> np.ndarray:
        return ((np.atleast_2d(x) - self.mu) / self.sd) @ self.coef.T + self.intercept

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.classes[np.argmax(self.scores(x), axis=1)]

    def t2(self, x: np.ndarray) -> np.ndarray:
        return self.chart.t2(x)

    def to_json(self, path: str | Path) -> None:
        payload = {"features": list(self.features), "mu": self.mu.tolist(), "sd": self.sd.tolist(), "classes": self.classes.tolist(),
                   "coef": self.coef.tolist(), "intercept": self.intercept.tolist(),
                   "t2": {"mean": self.chart.mean.tolist(), "precision": self.chart.precision.tolist(), "ucl": self.chart.ucl},
                   "meta": self.meta}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> "SignalModel":
        p = json.loads(Path(path).read_text(encoding="utf-8"))
        chart = T2Chart(np.array(p["t2"]["mean"]), np.array(p["t2"]["precision"]), float(p["t2"]["ucl"]))
        return cls(tuple(p["features"]), np.array(p["mu"]), np.array(p["sd"]), np.array(p["classes"]),
                   np.array(p["coef"]), np.array(p["intercept"]), chart, p.get("meta", {}))


# ------------------------------------------------------------------ 시간대 규칙

def et_minutes(index: pd.DatetimeIndex) -> np.ndarray:
    """KST(naive) 시각 → 미국 동부 시각의 '하루 중 분'. 서머타임 자동 반영."""
    et = index.tz_localize("Asia/Seoul").tz_convert("America/New_York")
    return (et.hour * 60 + et.minute).to_numpy()


def in_blackout(index: pd.DatetimeIndex, cfg: dict[str, Any]) -> np.ndarray:
    """미국 지표(08:30 ET)·정규장 개장(09:30 ET) 앞뒤에는 신규 진입 금지."""
    m = et_minutes(index)
    out = np.zeros(len(index), dtype=bool)
    for center in cfg["blackout"]["et_centers"]:
        hh, mm = map(int, center.split(":"))
        c = hh * 60 + mm
        out |= (m >= c - cfg["blackout"]["before_min"]) & (m < c + cfg["blackout"]["after_min"])
    return out


def in_window(index: pd.DatetimeIndex, start: str, end: str) -> np.ndarray:
    """KST 시계 기준 거래 창 [start, end). 시작 > 끝이면 자정을 넘는 창."""
    s, e = (dtime(*map(int, t.split(":"))) for t in (start, end))
    t = index.time
    if s <= e:
        return np.array([s <= x < e for x in t])
    return np.array([x >= s or x < e for x in t])


def decide(model: SignalModel, feats: pd.DataFrame, cfg: dict[str, Any], *, use_gate: bool) -> pd.DataFrame:
    """특징표 → 신호표(side: +1 롱 / −1 숏 / 0 관망, t2, in_control). ok 가 False 이면 side=0."""
    out = pd.DataFrame(index=feats.index)
    ok = feats["ok"].to_numpy()
    x = feats.loc[:, list(FEATURES)].to_numpy()
    side = np.zeros(len(feats), dtype=int)
    t2 = np.full(len(feats), np.nan)
    if ok.any():
        t2[ok] = model.t2(x[ok])
        side[ok] = model.predict(x[ok])
    out["t2"] = t2
    out["in_control"] = t2 <= model.chart.ucl
    out["svm_side"] = side
    gate = out["in_control"].to_numpy() if use_gate else np.ones(len(out), dtype=bool)
    out["side"] = np.where(ok & gate & ~in_blackout(feats.index, cfg), side, 0)
    return out
