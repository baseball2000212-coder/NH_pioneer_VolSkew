"""규칙 2(아시아 돌파+ZT 확인)와 규칙 3(미국 개장 모멘텀)을 '하루 하나의 포지션 흐름'으로 합친 통합 전략의 과거 검증.

독립 운영(두 규칙을 따로 1계약씩)과 달리 한 계좌의 포지션 상태로 규칙을 이어 붙인다.
 U0 독립 운영(기준): 규칙 2 와 규칙 3 을 각각 1계약
 U1 합의형: 22:30 에 규칙 2 포지션이 있고 규칙 3 방향과 같으면 그대로 보유(추가 없음), 반대면 22:30 시가에 청산하고 쉼.
            22:30 에 포지션이 없으면 규칙 3 방향으로 진입(23:45 청산). 최대 1계약.
 U2 가산형: U1 과 같되 방향이 같을 때 22:30 에 1계약 추가(규칙 3 레그는 23:45 청산, 규칙 2 레그는 손절/23:50). 최대 2계약.
 U3 필터형: 규칙 2 포지션이 있었던 날은 규칙 3 을 쓰지 않고(방향 같든 다르든), 규칙 2 없는 날만 규칙 3. 최대 1계약.
게이트 변형: 규칙 2 진입 = 20일 변동성≥20, 규칙 3 단독 진입 = 중국장 RV ≥ 직전 확장 중앙값. (규칙 3 이 규칙 2 포지션과 합쳐지는 경우는 게이트 없이 규칙 2 가 연 포지션을 따름)
비용 계약당 왕복 $3.5. 팀원 코드 미실행, 독립 구현. 규칙 3 단독 재현은 팀원 거래기록 csv 와 대조.
실행: uv run python -I week03/work/v3_unified.py > week03/work/v3_unified_out.txt
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
from team_rules_check3 import log_series, to_kst_day, RANGE, ENTRY, EXIT, TICK, B1, ZT_TO_Y2   # noqa: E402

ROOT = HERE.parents[1]
M = ROOT / "week03" / "data" / "team_macro"
DAILY = r"./team_data/금매크로_데이터/일봉/일봉_통합.csv"
MULT, COST = 10, 3.5
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---- 게이트 변수
tab = pd.read_csv(DAILY, encoding="utf-8-sig"); tab.columns = ["date", "gold", "dxy", "eur", "jpy", "y2", "y10", "r10", "broad"]
tab["date"] = pd.to_datetime(tab["date"]); g = np.log(tab.set_index("date")["gold"].dropna()).diff() * 100
vol20 = (g.rolling(20).std() * math.sqrt(252)).shift(1)

gc = load_team(M / "minute" / "GC.csv.gz")
zt = load_team(M / "minute" / "ZT.csv.gz"); zl = log_series(zt)
r1 = np.log(gc["c"]).diff(); r1[(gc["roll"] == 1) | (gc["ct"] != gc["ct"].shift())] = 0.0; r1 = r1.fillna(0.0)
kk = gc.index.tz_convert("Asia/Seoul")
f = pd.DataFrame({"r2": r1.values ** 2, "day": kk.normalize().tz_localize(None), "hm": kk.hour * 60 + kk.minute}, index=gc.index)
cn = f[(f.hm >= 600) & (f.hm < 955)].groupby("day")["r2"].sum(); cn = np.log(cn[cn > 0])
cn_gate = cn >= cn.expanding(120).median().shift(1)


def leg(side, entry, exit_, n=1):
    return (exit_ - entry) * side * MULT * n - COST * n


def rule2_day(x):
    """돌파 진입(+ZT 확인) 후 손절/청산 정보를 반환: (side, entry, i_in, stop_idx or None, hm array 등)"""
    hm, o, h, l, c = (x[k].values for k in ("hm", "o", "h", "l", "c"))
    in_rng = (hm >= RANGE[0]) & (hm < RANGE[1])
    if in_rng.sum() < 200:
        return None
    top, bot = h[in_rng].max(), l[in_rng].min(); mid = (top + bot) / 2
    win = np.where((hm >= ENTRY[0]) & (hm < ENTRY[1]))[0]
    if len(win) == 0:
        return None
    hit = np.where((h[win] >= top + TICK) | (l[win] <= bot - TICK))[0]
    if len(hit) == 0:
        return None
    i = win[hit[0]]
    if h[i] >= top + TICK and l[i] <= bot - TICK:
        return None
    side = 1 if h[i] >= top + TICK else -1
    entry = max(top + TICK, o[i]) if side > 0 else min(bot - TICK, o[i])
    # ZT 확인 (30분, 지연 1분)
    end = x.index[i] - pd.Timedelta(minutes=1); start = end - pd.Timedelta(minutes=30)
    mv = B1 * (zl.reindex([end]).values[0] - zl.reindex([start]).values[0]) * 100 * ZT_TO_Y2 * 10
    if not (np.sign(mv) == side):
        return None
    rest = np.arange(i, len(hm)); rest = rest[hm[rest] < EXIT]
    st = np.where(l[rest] <= mid if side > 0 else h[rest] >= mid)[0]
    stop = None
    if len(st):
        j = rest[st[0]]
        stop = (j, min(mid, o[j]) if side > 0 else max(mid, o[j]))
    return dict(side=side, entry=entry, i=i, stop=stop, last_c=c[rest[-1]], hm=hm, o=o, c=c)


def day_pnl(x, v20, cng):
    """하루의 변형별 손익 (달러). 반환: dict 변형→(pnl, 계약-거래 수)"""
    hm, o, c = x["hm"].values, x["o"].values, x["c"].values
    r2 = rule2_day(x)
    # 규칙 3 신호
    ny = x.index.tz_convert("America/New_York"); nh = (ny.hour * 60 + ny.minute).values   # 규칙 3 은 뉴욕 현지 시계 (팀원 코드와 동일)
    i_open = np.where(nh >= 18 * 60)[0]
    i_sig = np.where(nh <= 8 * 60 + 29)[0]
    i_in = np.where((nh >= 8 * 60 + 30) & (nh < 8 * 60 + 40))[0]
    i_out = np.where((nh >= 9 * 60 + 45) & (nh < 9 * 60 + 55))[0]
    s3 = None; T3 = 1350
    if len(i_open) and len(i_sig) and len(i_in) and len(i_out):
        d = c[i_sig[-1]] - o[i_open[0]]
        if d != 0:
            s3 = int(np.sign(d)); e3, x3, i22 = o[i_in[0]], o[i_out[0]], i_in[0]; T3 = hm[i_in[0]]
    out = {}
    # 규칙 2 PnL (독립)
    p2 = None; pos22 = None; p2_to_2230 = None
    if r2 is not None:
        if r2["stop"] is not None:
            p2 = leg(r2["side"], r2["entry"], r2["stop"][1])
        else:
            p2 = leg(r2["side"], r2["entry"], r2["last_c"])
        stopped_before_22 = r2["stop"] is not None and hm[r2["stop"][0]] < T3
        pos22 = (not stopped_before_22) and hm[r2["i"]] < T3
    p3 = leg(s3, e3, x3) if s3 else None
    for gate in (False, True):
        r2on = r2 is not None and (not gate or v20 >= 20)
        tag = "G" if gate else "N"
        # U0 독립
        a = (p2 if r2on else 0.0) + (p3 if (s3 and (not gate or cng)) else 0.0)
        n = (1 if r2on else 0) + (1 if (s3 and (not gate or cng)) else 0)
        out["U0" + tag] = (a, n)
        # U1 합의형 / U2 가산형 / U3 필터형
        if r2on and pos22 and s3:
            if r2["side"] == s3:
                out["U1" + tag] = (p2, 1)                           # 그대로 보유
                out["U2" + tag] = (p2 + p3, 2)                      # 1계약 추가
            else:
                cut = leg(r2["side"], r2["entry"], o[i22])           # 22:30 시가에 청산
                out["U1" + tag] = (cut, 1); out["U2" + tag] = (cut, 1)
            out["U3" + tag] = (p2, 1)
        elif r2on:
            # 22:30 전에 손절/종료였거나 규칙 3 신호 없음
            extra = p3 if (s3 and not pos22 and (not gate or cng)) else 0.0
            ne = 1 if (s3 and not pos22 and (not gate or cng)) else 0
            out["U1" + tag] = (p2 + extra, 1 + ne); out["U2" + tag] = (p2 + extra, 1 + ne); out["U3" + tag] = (p2, 1)
        elif s3 and (not gate or cng):
            out["U1" + tag] = (p3, 1); out["U2" + tag] = (p3, 1); out["U3" + tag] = (p3, 1)
    return out, p3


rows = {}
p3_check = {}
m = to_kst_day(gc)
for day, x in m.groupby("day", sort=True):
    if day < pd.Timestamp("2020-02-03") or day > pd.Timestamp("2026-10-30"):
        continue
    if x["ct"].nunique() > 1 or x["roll"].sum() > 0:
        continue
    v20 = vol20.reindex([day], method="ffill").values[0]
    cng = bool(cn_gate.get(day, False))
    if np.isnan(v20):
        continue
    o_, p3 = day_pnl(x, v20, cng)
    rows[day] = o_
    if p3 is not None:
        p3_check[day] = p3
vars_ = sorted({k for r in rows.values() for k in r})
P = pd.DataFrame({k: pd.Series({d: r[k][0] for d, r in rows.items() if k in r}) for k in vars_}).fillna(0.0)
N = pd.DataFrame({k: pd.Series({d: r[k][1] for d, r in rows.items() if k in r}) for k in vars_}).fillna(0)

# 규칙 3 단독 재현 대조 (팀원 csv: usd)
r3 = pd.read_csv(M / "code" / "team_rule3.csv", encoding="utf-8-sig"); r3["day"] = pd.to_datetime(r3["day"]); r3 = r3.set_index("day")["usd"]
mine = pd.Series(p3_check).loc["2020-02-03":]
com = pd.concat([mine, r3], axis=1, keys=["mine", "team"]).dropna()
print(f"규칙 3 재현 대조: 공통 {len(com)}일, 상관 {com.corr().iloc[0, 1]:.3f}, 평균 내 {com['mine'].mean():+.2f} vs 팀 {com['team'].mean():+.2f}, 일치(±$1) {(abs(com['mine'] - com['team']) < 1).mean():.0%}")

LAB = {"U0": "독립(기준)", "U1": "합의형", "U2": "가산형", "U3": "필터형"}
PERIODS = {"2020~2023": ("2020-02-03", "2023-12-31"), "2024~2025": ("2024-01-01", "2025-12-31"), "2020~2025": ("2020-02-03", "2025-12-31"), "2026": ("2026-01-01", "2026-10-31")}


def stat(col, a, b):
    x, n = P[col].loc[a:b], N[col].loc[a:b]
    td = x[n > 0]
    if len(td) < 20:
        return f"거래일 {len(td)}"
    cum = x.cumsum(); dd = (cum - cum.cummax()).min()
    t = td.mean() / (td.std() / math.sqrt(len(td)))
    return f"거래일 {len(td):>4} 계약/일 {n[n > 0].mean():.2f} 하루 {td.mean():+7.1f}$ t={t:+5.2f} 승일 {(td > 0).mean():.0%} 누적 {x.sum():+8.0f}$ MDD {dd:+7.0f}$ 최악 {td.min():+6.0f}$"


for tag, title in (("N", "게이트 없음"), ("G", "게이트(규칙 2: 20일 변동성≥20, 규칙 3 단독: 중국장 RV 중앙값 이상)")):
    print("=" * 140 + f"\n{title}")
    for pl, (a, b) in PERIODS.items():
        print(f"[{pl}]")
        for u in LAB:
            print(f"  {u} {LAB[u]:<9} {stat(u + tag, a, b)}")
    # 독립 대비 짝지은 일 차이 (모든 일 평균 기준)
    print("  -- U0 대비 일 손익 차이 (모든 영업일 평균, 짝지은 t)")
    for pl in ("2020~2025", "2026"):
        a, b = PERIODS[pl]
        for u in ("U1", "U2", "U3"):
            dlt = (P[u + tag] - P["U0" + tag]).loc[a:b]
            if dlt.std() == 0:
                print(f"    [{pl}] {u}: 차이 없음"); continue
            print(f"    [{pl}] {u}-U0: 평균 {dlt.mean():+6.2f}$ t={dlt.mean() / (dlt.std() / math.sqrt(len(dlt))):+5.2f} (차이가 있는 날 {(dlt != 0).sum()}일)")

# 같은 날 규칙 2 포지션이 22:30 에 있던 날의 일치/불일치 통계
cnt = {"일치": 0, "불일치": 0}
for d, r in rows.items():
    pass
print("\n(참고) 22:30 에 규칙 2 포지션이 열려 있던 날 수와 규칙 3 방향 일치율은 U1/U2 의 '차이가 있는 날' 수로 대신함")
