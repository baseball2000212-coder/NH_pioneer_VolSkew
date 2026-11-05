"""규칙 2(ZT 확인)와 규칙 3 을 한 계좌에서 같이 돌릴 때의 과거 성과 (각 1계약, 왕복 비용 $3.5 포함, 일 단위 합산).

구성 (사전 고정):
 A 둘 다 게이트 없음
 B 규칙 2 만 20일 변동성≥20 게이트, 규칙 3 은 없음
 C B + 규칙 3 은 중국장 RV 수준이 '직전까지의 중앙값' 이상일 때만 (확장 중앙값, 룩어헤드 없음)
 D 규칙 2 게이트만 (규칙 3 끔)
 E 규칙 3 만 / F 규칙 2 만 (게이트 없음)
게이트 임계값 20 은 앞선 분석(5-J)에서 전체 표본을 보고 정한 값이므로 18·22 도 함께 보여 준다.
실행: uv run python -I week03/work/v3_combo.py > week03/work/v3_combo_out.txt
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from team_rules_check import load_team   # noqa: E402
from team_rules_check3 import log_series, run   # noqa: E402

ROOT = HERE.parents[1]
M = ROOT / "week03" / "data" / "team_macro"
DAILY = r"./team_data/금매크로_데이터/일봉/일봉_통합.csv"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---- 국면 변수
tab = pd.read_csv(DAILY, encoding="utf-8-sig"); tab.columns = ["date", "gold", "dxy", "eur", "jpy", "y2", "y10", "r10", "broad"]
tab["date"] = pd.to_datetime(tab["date"]); g = np.log(tab.set_index("date")["gold"].dropna()).diff() * 100
vol20 = (g.rolling(20).std() * math.sqrt(252)).shift(1)

raw = pd.read_csv(M / "minute" / "GC.csv.gz", usecols=[0, 2, 6, 8], header=0, names=["utc", "ct", "c", "roll"])
raw.index = pd.to_datetime(raw["utc"], utc=True); raw = raw[~raw.index.duplicated()].sort_index()
r1 = np.log(raw["c"]).diff(); r1[(raw["roll"] == 1) | (raw["ct"] != raw["ct"].shift())] = 0.0; r1 = r1.fillna(0.0)
k = raw.index.tz_convert("Asia/Seoul")
f = pd.DataFrame({"r2": r1.values ** 2, "day": k.normalize().tz_localize(None), "hm": k.hour * 60 + k.minute}, index=raw.index)
cn = f[(f.hm >= 600) & (f.hm < 955)].groupby("day")["r2"].sum()
cn = np.log(cn[cn > 0])
cn_gate = cn >= cn.expanding(120).median().shift(1)

# ---- 거래
gc, zt = load_team(M / "minute" / "GC.csv.gz"), load_team(M / "minute" / "ZT.csv.gz")
zl = log_series(zt); zero = pd.Series(0.0, index=zl.index)
t2, _ = run(gc, zl, zero, confirm=True, b2=0.0)
r3 = pd.read_csv(M / "code" / "team_rule3.csv", encoding="utf-8-sig"); r3["day"] = pd.to_datetime(r3["day"]); r3 = r3.set_index("day")
u2, u3 = t2["usd"], r3["usd"]
v2 = vol20.reindex(u2.index, method="ffill")
c3 = cn_gate.reindex(u3.index).fillna(False)

DAYS = pd.bdate_range("2020-02-03", "2026-10-30")


def daily(*parts):
    out = pd.Series(0.0, index=DAYS); n = pd.Series(0, index=DAYS)
    for s in parts:
        s = s.reindex(DAYS).dropna(); out[s.index] += s; n[s.index] += 1
    return out, n


def stats(p, n, a, b, label):
    x, m = p.loc[a:b], n.loc[a:b]
    td = x[m > 0]
    if len(td) < 20:
        return f"  {label:<26} (거래일 {len(td)})"
    cum = x.cumsum(); dd = (cum - cum.cummax()).min()
    t = td.mean() / (td.std() / math.sqrt(len(td)))
    return (f"  {label:<26} 거래일 {len(td):>3} 건수/일 {m[m > 0].mean():.2f} 하루 {td.mean():+7.1f}$ t={t:+5.2f} 승일 {(td > 0).mean():.0%} 누적 {x.sum():+8.0f}$ MDD {dd:+7.0f}$ 최악의 하루 {td.min():+6.0f}$")


def build(th=20):
    g2 = u2[v2 >= th]
    return {
        "A 2·3 둘 다 무게이트": daily(u2, u3),
        f"B 2 게이트(≥{th})+3": daily(g2, u3),
        f"C 2 게이트+3 중국장게이트": daily(g2, u3[c3]),
        f"D 2 게이트만(≥{th})": daily(g2),
        "E 규칙 3 만": daily(u3),
        "F 규칙 2 만(무게이트)": daily(u2),
    }


PERIODS = {"2020~2023": ("2020-02-03", "2023-12-31"), "2024~2025": ("2024-01-01", "2025-12-31"), "2020~2025": ("2020-02-03", "2025-12-31"), "2026": ("2026-01-01", "2026-10-31")}
for th in (20, 18, 22):
    print("=" * 130 + f"\n게이트 임계값 vol20 ≥ {th}")
    S = build(th)
    for pl, (a, b) in PERIODS.items():
        print(f"[{pl}]")
        for name, (p, n) in S.items():
            print(stats(p, n, a, b, name))
    if th == 20:
        # 규칙 간 일손익 상관
        both = pd.DataFrame({"r2": u2.reindex(DAYS), "r3": u3.reindex(DAYS)}).dropna()
        print(f"\n규칙 2·3 같은 날 거래 {len(both)}일, 일손익 상관(2020~2025) {both.loc[:'2025'].corr().iloc[0, 1]:+.2f}, 2026 {both.loc['2026':].corr().iloc[0, 1]:+.2f}")
        g2 = u2[v2 >= 20]
        print(f"규칙 2 게이트 통과 비율 {len(g2) / len(u2):.0%} (2020~2025 {len(g2.loc[:'2025']) / len(u2.loc[:'2025']):.0%}, 2026 {len(g2.loc['2026':]) / len(u2.loc['2026':]):.0%})")
