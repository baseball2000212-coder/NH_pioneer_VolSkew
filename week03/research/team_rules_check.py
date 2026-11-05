"""팀원 전략(MGC 매크로 매매 규칙 PDF)의 규칙 1·3을 **PDF 문장만 보고 독립 재구현**해 재현한다. 팀원 코드는 실행하지 않는다.

입력: (a) 팀원 데이터 GC 1분봉(금선물_GC.csv.gz), ZT 1분봉 — 열: 시각_UTC,시각_KST,월물,시가,고가,저가,종가,거래량,롤
      (b) 우리 NH 모의 서버 MGCZ26 1분봉(week03/data/minute_MGCZ26.json) — 규칙 3 만
실행: uv run python -I week03/work/team_rules_check.py <GC.csv.gz> <ZT.csv.gz>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
MULT, COST = 10, 3.5
OPEN, CLOSE = 18 * 60, 17 * 60


def load_team(path: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    d.index = pd.to_datetime(d["시각_UTC"], utc=True)
    d = d.rename(columns={"월물": "ct", "시가": "o", "고가": "h", "저가": "l", "종가": "c", "롤": "roll"})[["ct", "o", "h", "l", "c", "roll"]]
    return d


def load_nh(sym: str = "MGCZ26") -> pd.DataFrame:
    raw = json.loads((ROOT / "week03" / "data" / f"minute_{sym}.json").read_text(encoding="utf-8"))["rows"]
    d = pd.DataFrame(raw)
    d.index = pd.to_datetime(d["ref_dt"] + d["exch_tm"], format="%Y%m%d%H%M%S").dt.tz_localize("Asia/Seoul").dt.tz_convert("UTC")
    d = d[~d.index.duplicated()].sort_index()
    for c in ("open_pric", "high_pric", "low_pric", "close_pric"):
        d[c] = pd.to_numeric(d[c])
    d = d.rename(columns={"open_pric": "o", "high_pric": "h", "low_pric": "l", "close_pric": "c"})
    d["ct"] = sym
    d["roll"] = 0
    return d[["ct", "o", "h", "l", "c", "roll"]]


def add_session(d: pd.DataFrame) -> pd.DataFrame:
    et = d.index.tz_convert("America/New_York")
    d = d.copy()
    d["hm"] = et.hour * 60 + et.minute
    d["sess"] = (et + pd.to_timedelta((d["hm"] >= OPEN).astype(int).values, unit="D")).normalize().tz_localize(None)
    return d[d["sess"].dt.dayofweek < 5]


def first_at(x: pd.DataFrame, hm: int):
    y = x[(x["hm"] >= hm) & (x["hm"] < hm + 10)]
    return y.iloc[0] if len(y) else None


def rule3(d: pd.DataFrame) -> pd.DataFrame:
    """22:29 KST(08:29 ET) 종가가 세션 시가(ET 18:00 첫 봉 시가)보다 높으면 매수, 낮으면 매도. 08:30 봉 시가 진입, 09:45 봉 시가 청산."""
    rows = []
    for sess, x in add_session(d).groupby("sess"):
        if x["ct"].nunique() > 1 or x["roll"].sum() > 0:
            continue
        night, morning = x[x["hm"] >= OPEN], x[x["hm"] < OPEN]
        if night.empty or morning.empty:
            continue
        sig = morning[morning["hm"] <= 8 * 60 + 29]
        ent, ext = first_at(morning, 8 * 60 + 30), first_at(morning, 9 * 60 + 45)
        if sig.empty or ent is None or ext is None:
            continue
        side = int(np.sign(sig["c"].iloc[-1] - night["o"].iloc[0]))
        if side == 0:
            continue
        usd = (ext["o"] - ent["o"]) * side * MULT - COST
        rows.append(dict(day=sess, side=side, entry=ent["o"], exit=ext["o"], usd=usd, bp=usd / (ent["o"] * MULT) * 1e4,
                         gross_ticks=(ext["o"] - ent["o"]) * side / 0.1))
    return pd.DataFrame(rows).set_index("day")


def rule1(gc: pd.DataFrame, zt: pd.DataFrame) -> pd.DataFrame:
    """전날 세션(직전 세션)의 금 변화와 ZT 변화(공정가치)가 같은 부호면 오늘 08:05 KST(ET 18:05)에 금 반대 방향 진입, ET 09:45 청산."""
    def sessions(m):
        m = add_session(m)
        g = m[(m["hm"] >= OPEN) | (m["hm"] < CLOSE)].groupby("sess")
        return m, pd.DataFrame({"open": g["o"].first(), "close": g["c"].last(), "n": g["ct"].nunique()})
    gm, gs = sessions(gc)
    _, zs = sessions(zt)
    rows = []
    days = gs.index
    for k in range(1, len(days)):
        s, p = days[k], days[k - 1]
        if p not in zs.index or gs.loc[s, "n"] > 1 or gs.loc[p, "n"] > 1 or zs.loc[p, "n"] > 1:
            continue
        dg = (gs.loc[p, "close"] / gs.loc[p, "open"] - 1) * 100
        dz = (zs.loc[p, "close"] / zs.loc[p, "open"] - 1) * 100   # ZT 가격 변화율: 오르면 금리 하락 → 금에 +
        if dg == 0 or dz == 0 or np.sign(dg) != np.sign(dz):
            continue
        x = gm[gm["sess"] == s]
        night, morning = x[x["hm"] >= OPEN], x[x["hm"] < OPEN]
        ent, ext = first_at(night, OPEN + 5), first_at(morning, 9 * 60 + 45)
        if ent is None or ext is None:
            continue
        side = -int(np.sign(dg))
        usd = (ext["o"] - ent["o"]) * side * MULT - COST
        rows.append(dict(day=s, side=side, entry=ent["o"], exit=ext["o"], usd=usd, bp=usd / (ent["o"] * MULT) * 1e4))
    return pd.DataFrame(rows).set_index("day")


def summ(t: pd.DataFrame) -> str:
    if len(t) < 3:
        return f"n={len(t)}"
    eq = t["usd"].cumsum()
    tt = t["bp"].mean() / (t["bp"].std() / np.sqrt(len(t)))
    return f"n={len(t)} 건당 {t['bp'].mean():+.1f}bp (${t['usd'].mean():+.1f}) t={tt:+.2f} 승률 {(t['usd']>0).mean():.0%} 누적 ${eq.iloc[-1]:+.0f} MDD ${(eq-eq.cummax().clip(lower=0)).min():.0f} 최악 ${t['usd'].min():.0f}"


if __name__ == "__main__":
    gc = load_team(sys.argv[1]); zt = load_team(sys.argv[2])
    periods = {"2020~2025": ("2020", "2025-12-31"), "2026.2~6": ("2026-02-01", "2026-06-30"), "2026.7~10": ("2026-07-01", "2026-10-31"), "2026.10": ("2026-10-01", "2026-10-31")}
    r3 = rule3(gc)
    print("== 규칙 3 (팀원 GC 데이터, 내 구현)")
    for k, (u, v) in periods.items():
        print(f"  {k}: {summ(r3.loc[u:v])}")
    r1 = rule1(gc, zt)
    print("== 규칙 1 (팀원 GC+ZT 데이터, 내 구현)")
    for k, (u, v) in periods.items():
        print(f"  {k}: {summ(r1.loc[u:v])}")
    nh = rule3(load_nh())
    print("== 규칙 3 (우리 NH 모의 서버 MGCZ26 분봉, 내 구현)")
    print(f"  {nh.index[0].date()} ~ {nh.index[-1].date()}: {summ(nh)}")
    print(f"  총이익 틱(비용 전) 평균 {nh['gross_ticks'].mean():+.1f}틱, 월별 건당bp:", {k: round(v.mean(), 1) for k, v in nh["bp"].groupby(nh.index.strftime('%Y-%m'))})
    # 같은 기간 비교: 팀원 GC 데이터 vs NH MGC (7.29~10.30)
    cmp = pd.concat([r3.loc[nh.index[0]:nh.index[-1], "bp"].rename("팀GC"), nh["bp"].rename("NH_MGC")], axis=1).dropna()
    print(f"  같은 날 {len(cmp)}일 비교: 상관 {cmp.corr().iloc[0,1]:+.2f}, 평균 bp 팀GC {cmp['팀GC'].mean():+.1f} / NH {cmp['NH_MGC'].mean():+.1f}, 부호 일치 {(np.sign(cmp['팀GC'])==np.sign(cmp['NH_MGC'])).mean():.0%}")
