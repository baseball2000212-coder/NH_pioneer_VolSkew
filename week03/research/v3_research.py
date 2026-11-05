"""전략 v3 연구 — 중국장 선행 정보 + 런던장 이벤트로 모의 운영시간(07:00~24:00 KST) 안의 구간 수익률을 설명할 수 있는가.

데이터: 팀원 GC 1분봉 2020-01~2026-10 (월물 교체 점프 제거). 모든 시각은 현지 시계(KST/London/New_York)로 정의 → 서머타임 자동 반영.
분할: 발견 2020~2023 / 검증 2024~2025 / 시험 2026 (단, 2026 은 앞선 탐색에서 일부 본 구간 → 진짜 표본외는 이번 주 모의 운영뿐).
사전 고정:
 A) 목표(구간 수익률 bp) × 특징(중국장·아시아·전일 미국·활동 서프라이즈) 회귀 — HAC t, 발견 구간 Holm 보정
 B) 발견 Holm<0.10 이고 검증·시험에서 부호 같고 검증 p<0.05(단측) 인 것만 '후보'
 C) 후보와 무관하게 월별 재추정 릿지(워크포워드) 표본외 R² 와 Diebold-Mariano
 D) HAR-RV 변동성 예측에 중국장 정보가 더하는 표본외 설명력 (사이징·손절용)
실행: uv run python -I week03/work/v3_research.py > week03/work/v3_research_out.txt
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
GC = ROOT / "week03" / "data" / "team_macro" / "minute" / "GC.csv.gz"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
COST_USD = 3.5
MULT = 10
rng = np.random.default_rng(7)

# ---------------------------------------------------------------- 자료
raw = pd.read_csv(GC, usecols=[0, 2, 6, 7, 8], header=0, names=["utc", "ct", "c", "v", "roll"])
raw.index = pd.to_datetime(raw["utc"], utc=True)
raw = raw[~raw.index.duplicated()].sort_index()
r1 = np.log(raw["c"]).diff()
r1[(raw["roll"] == 1) | (raw["ct"] != raw["ct"].shift())] = 0.0
r1 = r1.fillna(0.0)
LV = r1.cumsum().values                       # 로그가격 (롤 점프 제외)
CV = raw["v"].astype(float).cumsum().values
CR = (r1 ** 2).cumsum().values
PX = raw["c"].values
TS = raw.index.asi8
print(f"GC 1분봉 {len(raw):,}개 {raw.index[0]:%Y-%m-%d} ~ {raw.index[-1]:%Y-%m-%d}")

DAYS = pd.bdate_range("2020-01-06", raw.index[-1].tz_localize(None).normalize() - pd.Timedelta(days=1))
TOL = 6 * 60 * 10 ** 9


def anchor(tz: str, hh: int, mm: int, shift_days: int = 0) -> np.ndarray:
    idx = (DAYS + pd.Timedelta(days=shift_days) + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz, nonexistent="shift_forward", ambiguous="NaT")
    return idx.tz_convert("UTC").asi8


def at(arr, a, tol=True):
    i = np.searchsorted(TS, a, side="right") - 1
    j = np.clip(i, 0, None)
    ok = (i >= 0) & ((a - TS[j]) <= TOL if tol else True)
    return np.where(ok, arr[j], np.nan)


def win(t0, t1):
    """창 (t0,t1] 의 수익률(bp), 거래량, RV"""
    ret = (at(LV, t1) - at(LV, t0)) * 1e4
    vol = at(CV, t1, False) - at(CV, t0, False)
    rv = at(CR, t1, False) - at(CR, t0, False)
    return ret, vol, rv


A = lambda tz, h, m, s=0: anchor(tz, h, m, s)
K, LO, NY = "Asia/Seoul", "Europe/London", "America/New_York"

# 중국장 (KST 고정): 상하이 선물 주간 09:00~15:00 CST = 10:00~16:00 KST
cn_ret, cn_vol, cn_rv = win(A(K, 10, 0), A(K, 15, 55))
ce_ret, _, _ = win(A(K, 10, 0), A(K, 11, 30))                   # 오전 초반
cl_ret, _, _ = win(A(K, 14, 30), A(K, 15, 55))                  # 점심 후 재개
as_ret, as_vol, as_rv = win(A(K, 8, 5), A(K, 10, 0))             # 중국 개장 전 아시아
# 전일 미국 (뉴욕 08:30~16:00) — 전 영업일
us_ret_d, _, _ = win(A(NY, 8, 30), A(NY, 16, 0))
prev_us = pd.Series(us_ret_d).shift(1).values
# 런던 현지
lo_open = A(LO, 8, 0)
lo_ret, lo_vol, lo_rv = win(A(LO, 8, 0), A(LO, 12, 0))
# 목표
Y = {
    "런던 개장 08:00→12:00": win(A(LO, 8, 0), A(LO, 12, 0))[0],
    "런던 08:00→10:00": win(A(LO, 8, 0), A(LO, 10, 0))[0],
    "고시 전 10:00→10:30": win(A(LO, 10, 0), A(LO, 10, 30))[0],
    "고시 후 10:30→11:00": win(A(LO, 10, 30), A(LO, 11, 0))[0],
    "런던 12:00→미국지표 전(ET 08:25)": win(A(LO, 12, 0), A(NY, 8, 25))[0],
    "미국지표 ET 08:30→09:50": win(A(NY, 8, 30), A(NY, 9, 50))[0],
}
ENTRY_PX = {k: at(PX, a) for k, a in {
    "런던 개장 08:00→12:00": A(LO, 8, 0), "런던 08:00→10:00": A(LO, 8, 0), "고시 전 10:00→10:30": A(LO, 10, 0),
    "고시 후 10:30→11:00": A(LO, 10, 30), "런던 12:00→미국지표 전(ET 08:25)": A(LO, 12, 0), "미국지표 ET 08:30→09:50": A(NY, 8, 30)}.items()}

df = pd.DataFrame({"cn_ret": cn_ret, "cn_early": ce_ret, "cn_late": cl_ret, "as_ret": as_ret, "prev_us": prev_us,
                   "cn_vol": cn_vol, "cn_rv": cn_rv, "as_rv": as_rv, "lo_ret": lo_ret, "lo_vol": lo_vol, "lo_rv": lo_rv},
                  index=DAYS)
for k, v in Y.items():
    df["Y:" + k] = v
    df["PX:" + k] = ENTRY_PX[k]
# 활동 서프라이즈 = 로그(오늘 / 직전 40일 중앙값)
for c in ("cn_vol", "cn_rv", "lo_vol", "lo_rv", "as_rv"):
    s = np.log(df[c].where(df[c] > 0))
    df["act_" + c] = s - s.rolling(40, min_periods=20).median().shift(1)
df["cn_x_act"] = df["cn_ret"] * df["act_cn_vol"]
df["lo_x_act"] = df["lo_ret"] * df["act_lo_vol"]
df = df[~((df["cn_vol"] <= 0) | df["cn_vol"].isna())]
print(f"일 표본 {len(df)}일 ({df.index[0]:%Y-%m-%d}~{df.index[-1]:%Y-%m-%d})")

F_LON = ["cn_ret", "cn_early", "cn_late", "as_ret", "prev_us", "act_cn_vol", "act_cn_rv", "cn_x_act"]
F_US = F_LON + ["lo_ret", "act_lo_vol", "lo_x_act"]
NAME = {"cn_ret": "중국장 수익률", "cn_early": "중국 오전 초반", "cn_late": "중국 점심후", "as_ret": "아시아 오전", "prev_us": "전일 미국",
        "act_cn_vol": "중국 거래량 서프라이즈", "act_cn_rv": "중국 변동성 서프라이즈", "cn_x_act": "중국수익률×거래량서프",
        "lo_ret": "런던 오전 수익률", "act_lo_vol": "런던 거래량 서프", "lo_x_act": "런던수익률×거래량서프"}
TARGETS = {t: (F_US if ("ET 08" in t) else F_LON) for t in Y}
# 런던 12:00 이후 목표는 런던 오전 정보 사용 가능
for t in Y:
    if t.startswith("런던 12:00") or t.startswith("미국지표"):
        TARGETS[t] = F_US
    elif t.startswith("고시 후"):
        TARGETS[t] = F_LON
DISC = (None, "2023-12-31"); VAL = ("2024-01-01", "2025-12-31"); TEST = ("2026-01-01", None)


def z(s):
    return (s - s.expanding(60).mean().shift(1)) / s.expanding(60).std().shift(1)


for f in set(sum(TARGETS.values(), [])):
    df["z_" + f] = z(df[f])


def ols_hac(y, X, L=3):
    n, k = X.shape
    XtXi = np.linalg.pinv(X.T @ X)
    b = XtXi @ X.T @ y
    e = y - X @ b
    u = X * e[:, None]
    S = u.T @ u
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        G = u[l:].T @ u[:-l]
        S += w * (G + G.T)
    V = XtXi @ S @ XtXi
    return b, np.sqrt(np.diag(V))


def pnorm2(t):
    return math.erfc(abs(t) / math.sqrt(2))


def sl(d, span):
    a, b = span
    return d.loc[a:b] if (a or b) else d


# ---------------------------------------------------------------- A) 단일 특징 회귀
rows = []
for t, feats in TARGETS.items():
    for f in ["(상수)"] + feats:
        cols = ["Y:" + t] + (["z_" + f] if f != "(상수)" else [])
        d = df[cols].dropna()
        out = {"target": t, "feat": f}
        for lab, span in (("disc", DISC), ("val", VAL), ("test", TEST)):
            x = sl(d, span)
            if len(x) < 100:
                out[lab] = (np.nan, np.nan, len(x))
                continue
            X = np.ones((len(x), 1)) if f == "(상수)" else np.column_stack([np.ones(len(x)), x["z_" + f].values])
            b, se = ols_hac(x["Y:" + t].values, X)
            i = 0 if f == "(상수)" else 1
            out[lab] = (b[i], b[i] / se[i], len(x))
        rows.append(out)
# Holm (발견 구간)
pd_ = {(r["target"], r["feat"]): pnorm2(r["disc"][1]) for r in rows if not math.isnan(r["disc"][1])}
order = sorted(pd_.items(), key=lambda kv: kv[1])
m = len(order); adj = {}; run = 0
for i, (k, p) in enumerate(order):
    run = max(run, min(1, (m - i) * p)); adj[k] = run
print(f"\n== A) 검정 {m}개 (목표 {len(TARGETS)} × 특징·상수). 발견 구간 Holm 보정. 계수=특징 1σ당 bp (상수는 평균 bp)")
print(f"{'목표':<30}{'특징':<22}{'발견 b(t)':>16}{'Holm':>7}{'검증 b(t)':>16}{'시험 b(t)':>16}")
cands = []
for r in sorted(rows, key=lambda r: adj.get((r["target"], r["feat"]), 9)):
    k = (r["target"], r["feat"])
    if k not in adj:
        continue
    dsc, val, tst = r["disc"], r["val"], r["test"]
    pass_ = adj[k] < 0.10 and np.sign(val[0]) == np.sign(dsc[0]) and abs(val[1]) > 1.645 and np.sign(tst[0]) == np.sign(dsc[0])
    nm = NAME.get(r["feat"], r["feat"])
    if adj[k] < 0.9 or pass_:
        print(f"{r['target']:<30}{nm:<22}{dsc[0]:+7.2f}({dsc[1]:+5.1f}){adj[k]:7.3f}{val[0]:+7.2f}({val[1]:+5.1f}){tst[0]:+7.2f}({tst[1]:+5.1f})  {'★후보' if pass_ else ''}")
    if pass_:
        cands.append(r)
print(f"→ 후보(발견 Holm<0.10 & 검증 p<0.05 & 시험 부호 일치): {len(cands)}개")

# ---------------------------------------------------------------- B) 후보 거래 평가
def cost_bp(px):
    return COST_USD / (px * MULT) * 1e4


def evalrule(t, f, label):
    d = df[["Y:" + t, "z_" + f if f != "(상수)" else "Y:" + t, "PX:" + t]].dropna() if f != "(상수)" else df[["Y:" + t, "PX:" + t]].dropna()
    # 방향은 발견 구간 부호로 고정
    base = sl(d, DISC)
    if f == "(상수)":
        side_all = np.sign(base["Y:" + t].mean()) * np.ones(len(d))
    else:
        bsl = np.polyfit(base["z_" + f], base["Y:" + t], 1)[0]
        side_all = np.sign(bsl) * np.sign(d["z_" + f].values)
    net = side_all * d["Y:" + t].values - cost_bp(d["PX:" + t].values)
    s = pd.Series(net, index=d.index)
    print(f"  [{label}] 비용 ${COST_USD} 왕복 반영(건당 bp):")
    for lab, span in (("발견", DISC), ("검증", VAL), ("시험", TEST)):
        x = sl(s, span)
        tt = x.mean() / (x.std() / math.sqrt(len(x)))
        print(f"    {lab} n={len(x):>4} 평균 {x.mean():+6.2f}bp  t={tt:+5.2f}  승률 {(x > 0).mean():.0%}  총 {x.sum():+8.0f}bp")
    return s


print("\n== B) 후보 거래 평가 (방향·부호는 발견 구간에서 고정)")
for r in cands:
    evalrule(r["target"], r["feat"], f"{r['target']} / {NAME.get(r['feat'], r['feat'])}")

# 참고: 고시 전후 상수 규칙 (프로파일 효과) — 후보 여부와 무관하게 비용 대비 크기 출력
print("\n== B') 상수(캘린더) 효과의 비용 대비 크기")
for t in Y:
    d = df[["Y:" + t, "PX:" + t]].dropna()
    mu = d["Y:" + t].mean(); c = cost_bp(d["PX:" + t]).mean()
    print(f"  {t:<34} 평균 {mu:+6.2f}bp / 평균 비용 {c:5.2f}bp")

# ---------------------------------------------------------------- C) 워크포워드 릿지
def ridge_fit(X, y, lam):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
    return mu, sd, w, y.mean()


print("\n== C) 워크포워드(월별 재추정, 2022-01~) 릿지 표본외 — 평균 예측 대비 R²(OS), DM 검정, |예측|>비용일 때만 거래")
for t, feats in TARGETS.items():
    d = df[["Y:" + t, "PX:" + t] + ["z_" + f for f in feats]].dropna()
    d = d.loc["2020-06-01":]
    preds, base, actual, px = [], [], [], []
    months = sorted(set(d.loc["2022-01-01":].index.to_period("M")))
    for mth in months:
        tr = d[d.index < mth.start_time]
        te = d[(d.index >= mth.start_time) & (d.index <= mth.end_time)]
        if len(tr) < 300 or len(te) == 0:
            continue
        mu, sd, w, ym = ridge_fit(tr[["z_" + f for f in feats]].values, tr["Y:" + t].values, lam=len(tr) * 0.5)
        p = ((te[["z_" + f for f in feats]].values - mu) / sd) @ w + ym
        preds += list(p); base += [ym] * len(te); actual += list(te["Y:" + t].values); px += list(te["PX:" + t].values)
    p, b0, a, px = map(np.array, (preds, base, actual, px))
    e1, e0 = (a - p) ** 2, (a - b0) ** 2
    r2 = 1 - e1.sum() / e0.sum()
    dd = e0 - e1
    L = 5; dm = dd.mean(); var = dd.var() + 2 * sum((1 - l / (L + 1)) * np.cov(dd[l:], dd[:-l])[0, 1] for l in range(1, L + 1))
    dmt = dm / math.sqrt(max(var, 1e-12) / len(dd))
    cb = cost_bp(px)
    sel = np.abs(p) > cb
    net = np.where(sel, np.sign(p) * a - cb, 0.0)
    hit = (np.sign(p) == np.sign(a))[sel].mean() if sel.sum() else float("nan")
    print(f"  {t:<34} n={len(a):>4} R²_OS={r2*100:+6.2f}% DM t={dmt:+5.2f} | 비용초과 예측 {sel.sum():>3}건 방향적중 {hit:.0%} 건당 순 {net[sel].mean() if sel.sum() else float('nan'):+6.2f}bp")

# ---------------------------------------------------------------- D) HAR-RV
print("\n== D) 변동성 예측(HAR-RV): 목표 = 로그 RV (KST 16:00~23:55, 런던·미국 시간대), 2022-01~ 표본외")
rv_t, _, _ = None, None, None
_, _, rv_post = win(A(K, 16, 0), A(K, 23, 55))
_, _, rv_full = win(A(K, 8, 5), A(K, 23, 55))
h = pd.DataFrame({"post": np.log(np.where(rv_post > 0, rv_post, np.nan)), "full": np.log(np.where(rv_full > 0, rv_full, np.nan)),
                  "cn": np.log(df["cn_rv"].reindex(DAYS).where(lambda s: s > 0)).values, "as": np.log(df["as_rv"].reindex(DAYS).where(lambda s: s > 0)).values,
                  "cnv": np.log(df["cn_vol"].reindex(DAYS).where(lambda s: s > 0)).values}, index=DAYS)
h["l1"] = h["full"].shift(1); h["l5"] = h["full"].rolling(5).mean().shift(1); h["l22"] = h["full"].rolling(22).mean().shift(1)
h["pl1"] = h["post"].shift(1)
h = h.dropna()
def wf(cols, tag):
    pr, ac, bs = [], [], []
    for mth in sorted(set(h.loc["2022-01-01":].index.to_period("M"))):
        tr = h[h.index < mth.start_time]; te = h[(h.index >= mth.start_time) & (h.index <= mth.end_time)]
        if len(tr) < 300 or len(te) == 0:
            continue
        X = np.column_stack([np.ones(len(tr))] + [tr[c] for c in cols]); b = np.linalg.lstsq(X, tr["post"], rcond=None)[0]
        pr += list(np.column_stack([np.ones(len(te))] + [te[c] for c in cols]) @ b); ac += list(te["post"]); bs += [tr["post"].mean()] * len(te)
    return np.array(pr), np.array(ac), np.array(bs)
base_cols = ["l1", "l5", "l22"]
p0, a0, b0 = wf(base_cols, "HAR")
p1, a1, _ = wf(base_cols + ["cn", "cnv", "as"], "HAR+중국·아시아")
r2 = lambda p, a, b: 1 - ((a - p) ** 2).sum() / ((a - b) ** 2).sum()
print(f"  HAR(전일·5일·22일 전체 RV)              R²_OS={r2(p0,a0,b0)*100:5.1f}%")
print(f"  HAR + 당일 중국 RV·거래량·아시아 RV      R²_OS={r2(p1,a1,b0)*100:5.1f}%")
dd = (a0 - p0) ** 2 - (a1 - p1) ** 2
L = 5; var = dd.var() + 2 * sum((1 - l / (L + 1)) * np.cov(dd[l:], dd[:-l])[0, 1] for l in range(1, L + 1))
print(f"  증분 DM t={dd.mean() / math.sqrt(var / len(dd)):+.2f} (양수=중국 정보가 오차를 줄임)")
X = np.column_stack([np.ones(len(h)), h[base_cols + ["cn", "cnv", "as"]]])
b, se = ols_hac(h["post"].values, X, 5)
print("  전체표본 계수(t): " + ", ".join(f"{n}={bb:+.2f}({bb/s:+.1f})" for n, bb, s in zip(["상수"] + base_cols + ["cn_rv", "cn_vol", "as_rv"], b, se)))


# ---------------------------------------------------------------- E) 변동성 구간별 세션 내 추세/반전
print("\n== E) 변동성 예측 구간(전 기간 중앙값 기준 높음/낮음)별 세션 내 '앞부분 → 뒷부분' 수익률 관계 (사전 등록 3세션 × 전체·고·저)")
def har_series(cols):
    out = pd.Series(np.nan, index=h.index)
    for mth in sorted(set(h.loc["2021-07-01":].index.to_period("M"))):
        tr = h[h.index < mth.start_time]; te = h[(h.index >= mth.start_time) & (h.index <= mth.end_time)]
        if len(tr) < 300 or len(te) == 0:
            continue
        X = np.column_stack([np.ones(len(tr))] + [tr[c] for c in cols]); b = np.linalg.lstsq(X, tr["post"], rcond=None)[0]
        out.loc[te.index] = np.column_stack([np.ones(len(te))] + [te[c] for c in cols]) @ b
    return out
pr_lon = har_series(base_cols + ["cn", "cnv", "as"])      # 런던·미국 세션용 (중국장 정보 포함, KST 16:00 이후 사용)
pr_cn = har_series(base_cols)                              # 중국 세션용 (당일 정보 제외)
sess = {
    "런던 08~09시→09~12시": (win(A(LO, 8, 0), A(LO, 9, 0))[0], win(A(LO, 9, 0), A(LO, 12, 0))[0], A(LO, 9, 0), pr_lon),
    "미국지표 08:30~08:50→08:50~09:50": (win(A(NY, 8, 30), A(NY, 8, 50))[0], win(A(NY, 8, 50), A(NY, 9, 50))[0], A(NY, 8, 50), pr_lon),
    "중국 10:00~11:30→11:30~15:55": (ce_ret, win(A(K, 11, 30), A(K, 15, 55))[0], A(K, 11, 30), pr_cn),
}
E = {}
for nm, (er, lr, ea, pr) in sess.items():
    d = pd.DataFrame({"e": er, "l": lr, "px": at(PX, ea), "pr": pr.reindex(DAYS).values}, index=DAYS).dropna()
    d["z"] = z(d["e"]); d = d.dropna()
    med = d["pr"].expanding(60).median().shift(1)
    d["hi"] = d["pr"] > med
    E[nm] = d.dropna()
rowsE = []
for nm, d in E.items():
    for g, sub in (("전체", d), ("고변동", d[d["hi"]]), ("저변동", d[~d["hi"]])):
        o = {"nm": nm, "g": g}
        for lab, span in (("disc", ("2022-01-01", "2023-12-31")), ("val", VAL), ("test", TEST)):
            x = sub.loc[span[0]:span[1]]
            if len(x) < 60:
                o[lab] = (np.nan, np.nan, len(x)); continue
            b, se = ols_hac(x["l"].values, np.column_stack([np.ones(len(x)), x["z"].values]), 3)
            o[lab] = (b[1], b[1] / se[1], len(x))
        rowsE.append(o)
pE = sorted([(pnorm2(o["disc"][1]), i) for i, o in enumerate(rowsE) if not math.isnan(o["disc"][1])])
adjE = {}; run = 0
for j, (p, i) in enumerate(pE):
    run = max(run, min(1, (len(pE) - j) * p)); adjE[i] = run
print(f"{'세션':<34}{'구간':<6}{'발견 b(t)':>16}{'Holm':>7}{'검증 b(t)':>16}{'시험 b(t)':>16}   (b: 앞부분 1σ당 뒷부분 bp)")
for i, o in enumerate(rowsE):
    if i in adjE:
        print(f"{o['nm']:<34}{o['g']:<6}{o['disc'][0]:+7.2f}({o['disc'][1]:+5.1f}){adjE[i]:7.3f}{o['val'][0]:+7.2f}({o['val'][1]:+5.1f}){o['test'][0]:+7.2f}({o['test'][1]:+5.1f}) n={o['disc'][2]}/{o['val'][2]}/{o['test'][2]}")
print("  평균 비용 bp:", {nm: round(float(cost_bp(d['px']).mean()), 2) for nm, d in E.items()})


# ---------------------------------------------------------------- F) 팀 규칙 1·3 을 활동·변동성 구간으로 나눠 보기 (게이트가 근거가 있는가)
print("\n== F) 팀 규칙(비용 포함 거래 기록)을 '중국장 거래량 서프라이즈'·'예측 변동성' 높음/낮음으로 나눈 성과 (건당 순 bp)")
TD = ROOT / "week03" / "data" / "team_macro" / "code"
for nm, fn in (("규칙1", "team_rule1.csv"), ("규칙3", "team_rule3.csv")):
    t = pd.read_csv(TD / fn, encoding="utf-8-sig")
    t["day"] = pd.to_datetime(t["day"]); t = t.set_index("day")
    g = pd.DataFrame({"bp": t["bp"], "act": df["act_cn_vol"].reindex(t.index), "pr": pr_lon.reindex(t.index)}).dropna()
    am = df["act_cn_vol"].expanding(60).median().shift(1).reindex(g.index)
    pm = pr_lon.expanding(60).median().shift(1).reindex(g.index)
    g["act_hi"], g["pr_hi"] = g["act"] > am, g["pr"] > pm
    for lab, mask in (("전체", np.ones(len(g), bool)), ("중국 활동 높음", g["act_hi"].values), ("중국 활동 낮음", ~g["act_hi"].values),
                      ("예측변동성 높음", g["pr_hi"].values), ("예측변동성 낮음", ~g["pr_hi"].values)):
        for plab, span in (("2022~2023", ("2022-01-01", "2023-12-31")), ("2024~2025", VAL), ("2026", TEST)):
            x = g[mask].loc[span[0]:span[1], "bp"]
            if len(x) < 15:
                continue
            tt = x.mean() / (x.std() / math.sqrt(len(x)))
            print(f"  {nm} {lab:<10} {plab:<10} n={len(x):>3} 건당 {x.mean():+6.2f}bp t={tt:+5.2f}")
