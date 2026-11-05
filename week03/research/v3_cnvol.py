"""중국장 변동성(KST 10:00~15:55 의 1분 RV)이 규칙 2·3 성과를 설명하는가, 그리고 20일 변동성 위에 더하는 정보가 있는가.

중국장 변동성은 16:00 전에 확정되므로 규칙 2(16~22시 진입)·규칙 3(22:30 진입) 모두 룩어헤드 없음.
변수(표준화): lvl  = log(중국장 RV)            sur = log(중국장 RV) − 직전 40일 중앙값(shift 1)
              share = 중국장 RV / (08:05~15:55 RV)  (아시아 변동 중 중국 시간대 비중)
검정: 성과(bp) ~ 변수 단독 (9개, Holm) / 성과 ~ vol20 + 변수 (증분, HAC L=3) / 중국장 변동성 구간별 성과
실행: uv run python -I week03/work/v3_cnvol.py > week03/work/v3_cnvol_out.txt
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
pn = lambda t: math.erfc(abs(t) / math.sqrt(2))


def ols_hac(y, X, L=3):
    XtXi = np.linalg.pinv(X.T @ X)
    b = XtXi @ X.T @ y
    u = X * (y - X @ b)[:, None]
    S = u.T @ u
    for l in range(1, L + 1):
        G = u[l:].T @ u[:-l]
        S += (1 - l / (L + 1)) * (G + G.T)
    return b, np.sqrt(np.diag(XtXi @ S @ XtXi))


# 중국장 RV
raw = pd.read_csv(M / "minute" / "GC.csv.gz", usecols=[0, 2, 6, 8], header=0, names=["utc", "ct", "c", "roll"])
raw.index = pd.to_datetime(raw["utc"], utc=True)
raw = raw[~raw.index.duplicated()].sort_index()
r1 = np.log(raw["c"]).diff()
r1[(raw["roll"] == 1) | (raw["ct"] != raw["ct"].shift())] = 0.0
r1 = r1.fillna(0.0)
k = raw.index.tz_convert("Asia/Seoul")
f = pd.DataFrame({"r2": r1.values ** 2, "day": k.normalize().tz_localize(None), "hm": k.hour * 60 + k.minute}, index=raw.index)
cn = f[(f.hm >= 600) & (f.hm < 955)].groupby("day")["r2"].sum()
asia = f[(f.hm >= 485) & (f.hm < 955)].groupby("day")["r2"].sum()
cv = pd.DataFrame({"rv": cn, "asia": asia}).dropna()
cv = cv[(cv.rv > 0) & (cv.asia > 0)]
cv["lvl"] = np.log(cv.rv)
cv["sur"] = cv["lvl"] - cv["lvl"].rolling(40, min_periods=20).median().shift(1)
cv["share"] = cv.rv / cv.asia

# vol20 (일봉, 전날까지)
tab = pd.read_csv(DAILY, encoding="utf-8-sig"); tab.columns = ["date", "gold", "dxy", "eur", "jpy", "y2", "y10", "r10", "broad"]
tab["date"] = pd.to_datetime(tab["date"]); g = np.log(tab.set_index("date")["gold"].dropna()).diff() * 100
vol20 = (g.rolling(20).std() * math.sqrt(252)).shift(1)

gc, zt = load_team(M / "minute" / "GC.csv.gz"), load_team(M / "minute" / "ZT.csv.gz")
zl = log_series(zt); zero = pd.Series(0.0, index=zl.index)
t_all, _ = run(gc, zl, zero, confirm=False)
t_zt, _ = run(gc, zl, zero, confirm=True, b2=0.0)
r3 = pd.read_csv(M / "code" / "team_rule3.csv", encoding="utf-8-sig"); r3["day"] = pd.to_datetime(r3["day"]); r3 = r3.set_index("day")
SER = {"규칙2 돌파만": t_all["bp"], "규칙2 ZT확인": t_zt["bp"], "규칙3": r3["bp"]}
VARS = {"lvl": "중국장 RV 수준", "sur": "중국장 RV 서프라이즈", "share": "중국 변동 비중"}
panel = {}
for name, s in SER.items():
    d = pd.DataFrame({"bp": s}).join(cv[list(VARS)]).join(pd.DataFrame({"vol20": vol20.reindex(s.index, method="ffill")}))
    d = d.loc["2020-02-01":].dropna()
    for v in list(VARS) + ["vol20"]:
        d["z_" + v] = (d[v] - d[v].mean()) / d[v].std()
    panel[name] = d
print("중국장 RV 와 vol20 상관(일):", round(float(pd.DataFrame({"a": cv["lvl"], "b": vol20.reindex(cv.index, method="ffill")}).dropna().corr().iloc[0, 1]), 2))
yr = cv.loc["2020":].groupby(lambda d: d.year)["rv"].mean() ** 0.5 * math.sqrt(252) * 100
print("연평균 중국장 RV(일 환산 변동성 %, 참고):", {int(a): round(float(b), 2) for a, b in yr.items()})

print("\n== 단독 회귀 (성과 bp ~ 변수 1σ), HAC t, 전반(2020~23)/후반(2024~26)")
rows = []
for name, d in panel.items():
    for v in VARS:
        b, se = ols_hac(d["bp"].values, np.column_stack([np.ones(len(d)), d["z_" + v]]))
        hs = []
        for a, bb in (("2020", "2023"), ("2024", "2026")):
            x = d.loc[a:bb]; bh, sh = ols_hac(x["bp"].values, np.column_stack([np.ones(len(x)), x["z_" + v]])); hs.append((bh[1], bh[1] / sh[1]))
        rows.append((name, v, b[1], b[1] / se[1], hs))
order = sorted(range(len(rows)), key=lambda i: pn(rows[i][3])); adj = {}; run_ = 0
for j, i in enumerate(order):
    run_ = max(run_, min(1, (len(rows) - j) * pn(rows[i][3]))); adj[i] = run_
for i in order:
    n, v, b, t, h = rows[i]
    print(f"  {n:<10}{VARS[v]:<18} b={b:+6.2f} t={t:+5.2f} Holm={adj[i]:.3f} | 전반 {h[0][0]:+6.2f}({h[0][1]:+4.1f}) 후반 {h[1][0]:+6.2f}({h[1][1]:+4.1f})")

print("\n== 증분: 성과 ~ vol20 + 중국장 변수 (두 계수 모두 표시)")
for name, d in panel.items():
    for v in VARS:
        b, se = ols_hac(d["bp"].values, np.column_stack([np.ones(len(d)), d["z_vol20"], d["z_" + v]]))
        print(f"  {name:<10}{VARS[v]:<18} vol20 {b[1]:+6.2f}(t{b[1] / se[1]:+5.2f})  중국장 {b[2]:+6.2f}(t{b[2] / se[2]:+5.2f})")

print("\n== 중국장 RV 서프라이즈 3분위별 성과(건당 bp)")
for name, d in panel.items():
    q = pd.qcut(d["sur"], 3, labels=["낮음", "중간", "높음"])
    print(f"  {name:<10} " + " | ".join(f"{lab} n={int((q == lab).sum()):>3} {d['bp'][q == lab].mean():+6.1f}" for lab in ("낮음", "중간", "높음")))
print("== vol20≥20 이면서 중국장 서프라이즈가 높음/낮음 (규칙2 ZT확인)")
d = panel["규칙2 ZT확인"]; hi = d[d.vol20 >= 20]
for lab, x in (("서프라이즈 ≥0", hi[hi.sur >= 0]), ("서프라이즈 <0", hi[hi.sur < 0])):
    print(f"  {lab}: n={len(x)} 건당 {x['bp'].mean():+6.1f}bp")
