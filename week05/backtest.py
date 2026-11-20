"""백테스트 체결 시뮬레이터와 성과 지표. [백테스트] 출처의 체결만 만든다 (모의계좌 체결 아님).

가정(결과 JSON `assumptions` 에 그대로 저장한다):
- 신호: 5분봉이 완성되는 순간(봉 종가) 계산 → 체결은 **다음 봉 시가**(지연 1봉).
- 비용: 왕복 수수료 + 호가 폭. `opposite`(비관) = 진입·청산 모두 맞은편 호가로 체결해 호가 폭 2번,
  `touch`(낙관) = 진입은 지정가가 닿아 호가 폭 0, 청산만 시장가로 호가 폭 1번.
- 손절: 진입가 ∓ stop_k × (직전 σ5m × √h). **봉 안 가격 경로를 모르므로 한 봉에서 손절과 이익이 같이 가능하면 손절 먼저**(보수적).
  시가가 손절선을 이미 넘었으면 시가에서 청산(갭).
- 시간 청산: 진입 후 h 봉째 종가. 거래 창 끝(flatten)을 넘기는 진입은 하지 않는다(세션 정리).
- 한 번에 한 포지션, 수량 1. 일일 최대 거래 수 제한.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from signals import in_window


@dataclass(frozen=True)
class AssetSpec:
    tick: float
    tick_value: float
    spread_ticks: float
    fee_roundtrip_usd: float

    @property
    def point_value(self) -> float:
        return self.tick_value / self.tick

    @property
    def fee_pt(self) -> float:
        return self.fee_roundtrip_usd / self.point_value

    @property
    def spread_pt(self) -> float:
        return self.spread_ticks * self.tick

    def cost_pt(self, fill_model: str) -> float:
        return self.fee_pt + (2 if fill_model == "opposite" else 1) * self.spread_pt


def asset_spec(cfg: dict[str, Any], sym: str) -> AssetSpec:
    a = cfg["assets"][sym]
    return AssetSpec(a["tick"], a["tick_value"], a["spread_ticks"], a["fee_roundtrip_usd"])


def simulate(bars: pd.DataFrame, feats: pd.DataFrame, side: pd.Series, *, h: int, spec: AssetSpec, cfg: dict[str, Any],
             fill_model: str, window: tuple[str, str], max_per_day: int | None = None) -> pd.DataFrame:
    """신호표(side) → 거래 목록. `window` 는 진입 시각(KST 시계) 허용 구간."""
    step = pd.Timedelta(minutes=cfg["bar_minutes"])
    idx = bars.index
    o, hi, lo, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    rv = feats["rv_bp"].to_numpy()
    sd = side.reindex(idx).fillna(0).to_numpy().astype(int)
    entry_ok = in_window(idx + step, *window)
    flatten_h, flatten_m = map(int, cfg["flatten"].split(":"))
    cost = spec.cost_pt(fill_model)
    trades: list[dict[str, Any]] = []
    per_day: dict[Any, int] = {}
    free = 0
    n = len(idx)
    for i in range(n - h):
        if i < free or sd[i] == 0 or not entry_ok[i]:
            continue
        if (idx[i + h] - idx[i]) != step * h:      # 이후 h 봉이 이어지지 않으면(공백·세션 끝) 진입하지 않음
            continue
        t_entry = idx[i] + step
        t_exit = idx[i + h] + step
        flat = t_entry.normalize() + pd.Timedelta(hours=flatten_h, minutes=flatten_m)
        if t_exit > flat:
            continue
        day = t_entry.normalize()
        if max_per_day is not None and per_day.get(day, 0) >= max_per_day:
            continue
        s = int(sd[i])
        entry = o[i + 1]
        stop_dist = cfg["stop_k"] * rv[i] / 1e4 * c[i] * np.sqrt(h)
        stop = entry - s * stop_dist
        exit_px, reason, exit_i = c[i + h], "time", i + h
        for k in range(i + 1, i + h + 1):
            if s == 1 and lo[k] <= stop:
                exit_px, reason, exit_i = min(o[k], stop), "stop", k
                break
            if s == -1 and hi[k] >= stop:
                exit_px, reason, exit_i = max(o[k], stop), "stop", k
                break
        gross = s * (exit_px - entry)
        trades.append({"signal_ts": idx[i], "entry_ts": t_entry, "exit_ts": idx[exit_i] + step, "side": s, "entry": entry,
                       "exit": exit_px, "reason": reason, "gross_pt": gross, "cost_pt": cost, "net_pt": gross - cost,
                       "gross_usd": gross * spec.point_value, "net_usd": (gross - cost) * spec.point_value, "day": day})
        per_day[day] = per_day.get(day, 0) + 1
        free = exit_i
    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame, *, reps: int = 2000, seed: int = 11) -> dict[str, Any]:
    """거래 목록 → 지표. 신뢰구간은 '일' 단위 블록 부트스트랩(같은 날 거래는 같이 뽑는다)."""
    if trades.empty:
        return {"n": 0}
    net = trades["net_usd"].to_numpy()
    wins, losses = net[net > 0], net[net <= 0]
    equity = np.cumsum(net)
    dd = float((equity - np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]).min())
    by_day = trades.groupby("day")["net_usd"].apply(np.array)
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(reps):
        pick = rng.integers(0, len(by_day), len(by_day))
        sample = np.concatenate([by_day.iloc[j] for j in pick])
        means.append(sample.mean())
    lo, hi = np.quantile(means, [0.025, 0.975])
    return {
        "n": int(len(net)), "days": int(len(by_day)),
        "net_total_usd": round(float(net.sum()), 2), "net_mean_usd": round(float(net.mean()), 3),
        "net_mean_ci95_usd": [round(float(lo), 3), round(float(hi), 3)],
        "gross_mean_usd": round(float(trades["gross_usd"].mean()), 3),
        "win_rate": round(float((net > 0).mean()), 3),
        "payoff": round(float(wins.mean() / abs(losses.mean())), 3) if len(wins) and len(losses) and losses.mean() != 0 else None,
        "max_drawdown_usd": round(dd, 2),
        "stop_ratio": round(float((trades["reason"] == "stop").mean()), 3),
        "long_ratio": round(float((trades["side"] == 1).mean()), 3),
    }
