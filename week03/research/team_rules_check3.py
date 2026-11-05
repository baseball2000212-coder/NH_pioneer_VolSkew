"""팀원 규칙 2(아시아 돌파 + 공정가치 확인)를 규칙서 문장과 점검본 코드 읽기(실행 아님)를 바탕으로 **독립 재구현**해 재현한다.

실행: uv run python -I week03/work/team_rules_check3.py <GC.csv.gz> <ZT.csv.gz> <6E.csv.gz>
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "week03/work")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from team_rules_check import MULT, COST, load_team, summ  # noqa: E402

TICK = 0.1
B1, B2, ZT_TO_Y2 = -0.675, 0.963, -0.494   # 규칙서 3장
RANGE, ENTRY, EXIT = (8 * 60, 16 * 60), (16 * 60, 22 * 60), 23 * 60 + 50


def to_kst_day(d: pd.DataFrame) -> pd.DataFrame:
    kst = d.index.tz_convert("Asia/Seoul")
    d = d.copy()
    d["day"] = kst.normalize().tz_localize(None)
    d["hm"] = kst.hour * 60 + kst.minute
    return d[d["day"].dt.dayofweek < 5]


def trade_for_day(x: pd.DataFrame):
    hm, o, h, l, c = (x[k].values for k in ("hm", "o", "h", "l", "c"))
    in_rng = (hm >= RANGE[0]) & (hm < RANGE[1])
    if in_rng.sum() < 200:
        return None
    top, bot = h[in_rng].max(), l[in_rng].min()
    mid = (top + bot) / 2
    win = np.where((hm >= ENTRY[0]) & (hm < ENTRY[1]))[0]
    if len(win) == 0:
        return None
    up, dn = h[win] >= top + TICK, l[win] <= bot - TICK
    hit = np.where(up | dn)[0]
    if len(hit) == 0:
        return None
    i = win[hit[0]]
    if h[i] >= top + TICK and l[i] <= bot - TICK:
        return "both"
    side = 1 if h[i] >= top + TICK else -1
    entry = max(top + TICK, o[i]) if side > 0 else min(bot - TICK, o[i])
    rest = np.arange(i, len(hm))
    rest = rest[hm[rest] < EXIT]
    stop = np.where(l[rest] <= mid if side > 0 else h[rest] >= mid)[0]
    if len(stop):
        j = rest[stop[0]]
        out = min(mid, o[j]) if side > 0 else max(mid, o[j])
        return side, entry, out, 1, i
    return side, entry, c[rest[-1]], 0, i


def log_series(d: pd.DataFrame) -> pd.Series:
    """월물이 바뀐 분의 수익률을 0 으로 둔 누적 로그 종가를 1분 격자로."""
    r = np.log(d["c"]).diff().fillna(0.0)
    r[(d["roll"] == 1) | (d["ct"] != d["ct"].shift())] = 0.0
    return pd.Series(r.cumsum().values, index=d.index).resample("1min").last().ffill(limit=10)


def run(gc, zt, e6, confirm: bool, ts_delay=1, window=30, b1=B1, b2=B2):
    m = to_kst_day(gc)
    rows, both = [], 0
    for day, x in m.groupby("day", sort=True):
        if x["ct"].nunique() > 1 or x["roll"].sum() > 0:
            continue
        r = trade_for_day(x)
        if r is None:
            continue
        if r == "both":
            both += 1
            continue
        side, entry, out, stopped, i = r
        rows.append(dict(day=day, side=side, entry=entry, exit=out, stopped=stopped, t_in=x.index[i]))
    t = pd.DataFrame(rows).set_index("day")
    t["usd"] = (t["exit"] - t["entry"]) * t["side"] * MULT - COST
    t["bp"] = t["usd"] / (t["entry"] * MULT) * 1e4
    if confirm:
        end = t["t_in"] - pd.Timedelta(minutes=ts_delay)
        start = end - pd.Timedelta(minutes=window)
        move = b1 * (zt.reindex(end).values - zt.reindex(start).values) * 100 * ZT_TO_Y2 * 10
        move = move + b2 * (e6.reindex(end).values - e6.reindex(start).values) * 100
        keep = np.sign(pd.Series(move, index=t.index)) == t["side"]
        t = t[keep.values]
    return t, both


if __name__ == "__main__":
    gc, zt, e6 = (load_team(p) for p in sys.argv[1:4])
    zl, el = log_series(zt), log_series(e6)
    periods = {"2026.2~6": ("2026-02-01", "2026-06-30"), "2026.7~10": ("2026-07-01", "2026-10-31"), "2026.10": ("2026-10-01", "2026-10-31"), "2020~2024": ("2020", "2024")}
    t_all, both = run(gc, zl, el, confirm=False)
    t_conf, _ = run(gc, zl, el, confirm=True)
    print(f"돌파 전체 {len(t_all)}건, 양방향 동시 돌파로 건너뜀 {both}일, 확인 통과 {len(t_conf)}건")
    print("== 확인 없음 (돌파만)")
    for k, (u, v) in periods.items():
        print(f"  {k}: {summ(t_all.loc[u:v])}")
    print("== 매크로 확인 적용 (ZT+6E, 6E 이전 기간은 0건)")
    for k, (u, v) in periods.items():
        print(f"  {k}: {summ(t_conf.loc[u:v])}")
    # 부호를 뒤집으면? (규칙서: 손실)
    t_flip, _ = run(gc, zl, el, confirm=True, b1=-B1, b2=-B2)
    print("== 부호 반대 (규칙서: 손실)")
    for k in ("2026.2~6", "2026.7~10"):
        u, v = periods[k]
        print(f"  {k}: {summ(t_flip.loc[u:v])}")
    # 22시 이후(규칙 2 진입 시각 분포)와 손절 비율
    x = t_conf.loc["2026-02-01":"2026-10-31"]
    print(f"2026.2~10 확인 통과 {len(x)}건 중 손절 {int(x['stopped'].sum())}건 ({x['stopped'].mean():.0%})")
