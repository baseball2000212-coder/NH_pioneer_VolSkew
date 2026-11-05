"""국면(시장 상태) 변수로 규칙 2·3 의 성과를 설명할 수 있는가, 그리고 '2026 과 닮은 과거'에서 규칙이 작동했는가.

국면 변수 (모두 거래일 전날까지의 일봉으로만 계산 → 룩어헤드 없음, 수익률을 쓰지 않는 유사도):
 vol20  최근 20일 금 일수익률 표준편차(연율 %)       trend  최근 60일 효율비 |Σr|/Σ|r| (1에 가까울수록 한 방향 추세)
 ret60  최근 60일 금 로그수익률(%)                      macR2  최근 120일 금~(Δ2년물, ΔDXY) 회귀 R² (매크로 설명력)
 b2y    최근 120일 Δ2년물(10bp) 계수 (금리 민감도)
성과 계열: 규칙2 돌파만 / 규칙2 ZT 확인 통과 / 규칙3 (팀원 거래기록 csv, 비용 포함 bp)
사전 고정 검정:
 G1 성과 ~ 국면변수(표준화) 단일 회귀 15개 (HAC L=3), 전체 표본 Holm + 전반(2020~23)/후반(2024~26) 부호 일치
 G2 2026 상반기(1~6월) 국면 중심과의 거리 (유클리드, 변수는 전 기간 표준화) → 2020~2025 에서 가까운 20% vs 나머지 성과 (Welch t)
 G3 같은 거리가 2026 하반기(7~10월)에서도 성과와 관계가 있는가 (유사도 점수 대비 성과의 순위상관)
실행: uv run python -I week03/work/v3_regime.py > week03/work/v3_regime_out.txt
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


def pn(t):
    return math.erfc(abs(t) / math.sqrt(2))


def ols_hac(y, X, L=3):
    n = len(y)
    XtXi = np.linalg.pinv(X.T @ X)
    b = XtXi @ X.T @ y
    u = X * (y - X @ b)[:, None]
    S = u.T @ u
    for l in range(1, L + 1):
        G = u[l:].T @ u[:-l]
        S += (1 - l / (L + 1)) * (G + G.T)
    return b, np.sqrt(np.diag(XtXi @ S @ XtXi))


# ------------------------------------------------------------ 국면 변수 (일봉)
tab = pd.read_csv(DAILY, encoding="utf-8-sig")
tab.columns = ["date", "gold", "dxy", "eur", "jpy", "y2", "y10", "r10", "broad"]
tab["date"] = pd.to_datetime(tab["date"]); tab = tab.set_index("date").loc["2018-06-01":].dropna(subset=["gold", "dxy", "y2"])
r = pd.DataFrame({"g": np.log(tab["gold"]).diff() * 100, "d": np.log(tab["dxy"]).diff() * 100, "y": tab["y2"].diff() * 100 / 10}).dropna()
reg = pd.DataFrame(index=r.index)
reg["vol20"] = r["g"].rolling(20).std() * math.sqrt(252)
reg["trend"] = r["g"].rolling(60).sum().abs() / r["g"].abs().rolling(60).sum()
reg["ret60"] = r["g"].rolling(60).sum()
macR2, b2y = [], []
for i in range(len(r)):
    if i < 120:
        macR2.append(np.nan); b2y.append(np.nan); continue
    w = r.iloc[i - 120:i]            # 당일 제외 직전 120일
    X = np.column_stack([np.ones(120), w["y"], w["d"]])
    b = np.linalg.lstsq(X, w["g"], rcond=None)[0]
    res = w["g"] - X @ b
    macR2.append(1 - res.var() / w["g"].var()); b2y.append(b[1])
reg["macR2"], reg["b2y"] = macR2, b2y
reg = reg.shift(1)                     # 거래일 전날까지만 사용
VARS = ["vol20", "trend", "ret60", "macR2", "b2y"]
NAME = {"vol20": "20일 변동성", "trend": "60일 추세효율", "ret60": "60일 수익률", "macR2": "매크로 설명력(120일 R²)", "b2y": "금리 민감도(120일)"}

print("== 국면 변수 연도별 평균")
yr = reg.dropna().loc["2020":].groupby(lambda d: d.year).mean()
print(yr.round(2).to_string())

# ------------------------------------------------------------ 성과 계열
gc, zt = load_team(M / "minute" / "GC.csv.gz"), load_team(M / "minute" / "ZT.csv.gz")
zl = log_series(zt); zero = pd.Series(0.0, index=zl.index)
t_all, _ = run(gc, zl, zero, confirm=False)
t_zt, _ = run(gc, zl, zero, confirm=True, b2=0.0)
r3 = pd.read_csv(M / "code" / "team_rule3.csv", encoding="utf-8-sig"); r3["day"] = pd.to_datetime(r3["day"]); r3 = r3.set_index("day")
SER = {"규칙2 돌파만": t_all["bp"], "규칙2 ZT확인": t_zt["bp"], "규칙3": r3["bp"]}
panel = {}
for k, s in SER.items():
    d = pd.DataFrame({"bp": s})
    d = d.join(reg.reindex(d.index, method="ffill"))
    panel[k] = d.loc["2020-02-01":].dropna()
    print(f"  {k}: 거래 {len(panel[k])}건 ({panel[k].index[0]:%Y-%m-%d}~{panel[k].index[-1]:%Y-%m-%d})")

# 표준화는 전 기간 일봉 국면변수 분포 기준 (수익률 미사용)
ref = reg.dropna().loc["2020-02-01":]
mu, sd = ref.mean(), ref.std()
for k in panel:
    for v in VARS:
        panel[k]["z_" + v] = (panel[k][v] - mu[v]) / sd[v]

# ------------------------------------------------------------ G1
print("\n== G1 성과(건당 bp) ~ 국면변수(1σ당 bp), HAC t / 전반(2020~23)·후반(2024~26) 부호")
rows = []
for k, d in panel.items():
    for v in VARS:
        X = np.column_stack([np.ones(len(d)), d["z_" + v]])
        b, se = ols_hac(d["bp"].values, X)
        halves = []
        for a, bb in (("2020", "2023"), ("2024", "2026")):
            x = d.loc[a:bb]
            bh, sh = ols_hac(x["bp"].values, np.column_stack([np.ones(len(x)), x["z_" + v]]))
            halves.append((bh[1], bh[1] / sh[1]))
        rows.append((k, v, b[1], b[1] / se[1], halves))
order = sorted(range(len(rows)), key=lambda i: pn(rows[i][3]))
adj = {}; run_ = 0
for j, i in enumerate(order):
    run_ = max(run_, min(1, (len(rows) - j) * pn(rows[i][3]))); adj[i] = run_
for i in order:
    k, v, b, t, h = rows[i]
    same = "일치" if np.sign(h[0][0]) == np.sign(h[1][0]) else "불일치"
    print(f"  {k:<10}{NAME[v]:<22} b={b:+6.2f} t={t:+5.2f} Holm={adj[i]:.3f} | 전반 {h[0][0]:+6.2f}({h[0][1]:+4.1f}) 후반 {h[1][0]:+6.2f}({h[1][1]:+4.1f}) 부호 {same}")

# ------------------------------------------------------------ G2/G3
print("\n== G2 2026 상반기(1~6월) 국면 중심과의 거리 → 2020~2025 에서 가까운 20% vs 나머지")
zr = ((reg.loc["2020-02-01":] - mu) / sd).dropna()
cent = zr.loc["2026-01-01":"2026-06-30"].mean()
print("  2026 상반기 국면 중심(z):", {NAME[v]: round(float(cent[v]), 2) for v in VARS})
dist = np.sqrt(((zr - cent) ** 2).sum(axis=1))
for k, d in panel.items():
    dd = dist.reindex(d.index, method="ffill")
    pre = d.loc["2020":"2025"]; dpre = dd.loc["2020":"2025"]
    cut = dpre.quantile(0.2)
    near, far = pre[dpre <= cut]["bp"], pre[dpre > cut]["bp"]
    w = (near.mean() - far.mean()) / math.sqrt(near.var() / len(near) + far.var() / len(far))
    print(f"  {k:<10} 가까운 20% n={len(near):>3} {near.mean():+6.1f}bp (t{near.mean() / (near.std() / math.sqrt(len(near))):+5.2f}) | 나머지 n={len(far):>3} {far.mean():+6.1f}bp | 차이 Welch t={w:+5.2f}")
    # 거리 5분위
    q = pd.qcut(dpre, 5, labels=False)
    print("      거리 5분위(가까움→멂) 건당 bp:", " ".join(f"{pre['bp'][q == i].mean():+6.1f}" for i in range(5)))
print("\n== G3 같은 거리가 2026 하반기(7~10월) 성과와도 관계가 있는가 (거리 vs 성과 순위상관, 음수=가까울수록 좋음)")
for k, d in panel.items():
    dd = dist.reindex(d.index, method="ffill")
    x = d.loc["2026-07-01":]; dx = dd.loc["2026-07-01":]
    rho = pd.Series(dx.values).rank().corr(pd.Series(x["bp"].values).rank())
    print(f"  {k:<10} n={len(x)} 순위상관 {rho:+.3f} (t≈{rho * math.sqrt((len(x) - 2) / (1 - rho ** 2)):+.2f})")

print("\n== 2026 과 가장 닮은 과거 월 (월평균 거리 하위 8개) 과 그 달 규칙 성과(건당 bp)")
mon = dist.loc[:"2025-12-31"].groupby(lambda d: (d.year, d.month)).mean().sort_values().head(8)
for (y, m), dv in mon.items():
    cells = []
    for k, d in panel.items():
        x = d[(d.index.year == y) & (d.index.month == m)]["bp"]
        cells.append(f"{k} {x.mean():+6.1f}(n={len(x)})" if len(x) else f"{k} -")
    print(f"  {y}-{m:02d} 거리 {dv:.2f} | " + " | ".join(cells))

print("\n== H 20일 변동성 구간별(연율 %) 성과 — 용량-반응과 게이트 후보")
bins = [0, 12, 16, 20, 25, 100]
for k, d in panel.items():
    print(f"  {k}")
    b = pd.cut(d["vol20"], bins)
    for iv, x in d.groupby(b, observed=True):
        s = x["bp"]
        pre, post = x.loc[:"2023"]["bp"], x.loc["2024":]["bp"]
        print(f"    vol20 {str(iv):<10} n={len(s):>4} 건당 {s.mean():+6.1f}bp (t{s.mean() / (s.std() / math.sqrt(len(s))) if len(s) > 4 else float('nan'):+5.2f}) | 2020~23 {pre.mean() if len(pre) else float('nan'):+6.1f}(n={len(pre)}) | 2024~26 {post.mean() if len(post) else float('nan'):+6.1f}(n={len(post)})")
print("  게이트 vol20>=20 / 규칙별 연도별 건당 bp:")
for k, d in panel.items():
    g = d[d["vol20"] >= 20]
    print(f"    {k:<10} " + " ".join(f"{y}:{g.loc[str(y)]['bp'].mean():+.1f}(n={len(g.loc[str(y)])})" for y in range(2020, 2027) if len(g.loc[str(y)]) > 0))
