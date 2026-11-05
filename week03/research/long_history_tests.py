"""중국장·런던장과 금의 연관성 — 가능한 한 긴 과거 자료(팀원 GC 1분봉 2020-01~2026-10, 일봉 1995~)로 한 번에 검정.

사전 고정한 검정 (결과를 보기 전에 정함):
 [중국] C1 이벤트 거래량 점프(개장·점심·재개) 연도별 안정성 / C2 중국 휴장일 DID — 중국 거래시간 거래량 비중 / C3 같은 DID — 변동성 비중
        C4 장기 연휴(≥3일) 직후 첫 영업일 중국 거래시간 절대수익 / C5 연휴 직전 영업일 절대수익
 [런던] L1 이벤트 거래량 점프(개장·오전 고시·오후 고시) 연도별 / L2 영국 단독 휴장일 DID — 런던 오전 거래량 비중 / L3 같은 DID — 변동성
        L4 LBMA 오전 고시 전후 30분 수익률 / L5 오후 고시 전후 30분 수익률
 [대조] 미국 휴장일 DID(미국 시간대 비중 — 방법이 맞는지 확인), 중국 휴장일이 런던 비중에 주는 영향(특이성)
 [장기 일봉] 달러 광의지수의 'DXY 로 설명 안 되는 부분'(위안·멕시코·캐나다 등)이 금 일수익률을 더 설명하는가 (2006~)
다중비교: 주요 11개 검정을 Holm 방식으로 보정.
실행: uv run --with holidays python -I week03/work/long_history_tests.py
"""
from __future__ import annotations

import glob
import math
import sys
from collections import OrderedDict
from pathlib import Path

import holidays
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
GC = ROOT / "week03" / "data" / "team_macro" / "minute" / "GC.csv.gz"
DAILY_DIR = r"./team_data/"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PVALS: "OrderedDict[str, float]" = OrderedDict()


def norm_p(t: float) -> float:
    return math.erfc(abs(t) / math.sqrt(2))


def holm(pvals: dict) -> dict:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    adj, running = {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


# ------------------------------------------------------------------ 자료 적재
print("자료 적재 중 ...", flush=True)
raw = pd.read_csv(GC, usecols=[0, 2, 6, 7, 8], header=0, names=["utc", "ct", "c", "v", "roll"])
raw.index = pd.to_datetime(raw["utc"], utc=True)
raw = raw[~raw.index.duplicated()].sort_index()
r1 = np.log(raw["c"]).diff()
r1[(raw["roll"] == 1) | (raw["ct"] != raw["ct"].shift())] = 0.0
r1 = r1.fillna(0.0)
L = r1.cumsum()                                         # 월물 교체 점프를 뺀 누적 로그가격
bars = pd.DataFrame({"v": raw["v"].astype(float), "absr": r1.abs()}, index=raw.index)
k = (raw.index.tz_localize(None) + pd.Timedelta(hours=9))
bars["kd"], bars["km"] = k.normalize(), k.hour * 60 + k.minute
lt = raw.index.tz_convert("Europe/London").tz_localize(None)
bars["ld"], bars["lm"] = lt.normalize(), lt.hour * 60 + lt.minute
print(f"GC 1분봉 {len(bars):,}개 {raw.index[0]:%Y-%m-%d} ~ {raw.index[-1]:%Y-%m-%d}", flush=True)

yrs = range(2020, 2027)
CN = holidays.country_holidays("CN", years=yrs)
UK = holidays.country_holidays("GB", subdiv="ENG", years=yrs)
US = holidays.country_holidays("US", years=yrs)
HK = holidays.country_holidays("HK", years=yrs)
JP = holidays.country_holidays("JP", years=yrs)
KR = holidays.country_holidays("KR", years=yrs)
S = lambda cal: {pd.Timestamp(d) for d in cal}
SET = {"CN": S(CN), "UK": S(UK), "US": S(US), "HK": S(HK), "JP": S(JP), "KR": S(KR)}


# ------------------------------------------------------------------ 도구
def win_vol(daycol, mincol, a, b):
    m = (bars[mincol] >= a) & (bars[mincol] < b)
    f = bars.loc[m]
    return f.groupby(f[daycol])["v"].sum() / (b - a)


def jump(label, daycol, mincol, ev, base, stats=True):
    e, b = win_vol(daycol, mincol, *ev), win_vol(daycol, mincol, *base)
    r = pd.concat([e, b], axis=1, keys=["e", "b"]).dropna()
    r = r[(r["e"] > 0) & (r["b"] > 0) & (r.index.dayofweek < 5)]
    lr = np.log(r["e"] / r["b"])
    t = lr.mean() / (lr.std() / math.sqrt(len(lr)))
    by = lr.groupby(lr.index.year).agg(["mean", "size"])
    ty = lr.groupby(lr.index.year).apply(lambda s: s.mean() / (s.std() / math.sqrt(len(s))) if len(s) > 5 else np.nan)
    yr = " ".join(f"{y % 100:02d}:{math.exp(by.loc[y, 'mean']):.2f}" for y in by.index)
    pos = int((by["mean"] > 0).sum())
    print(f"  {label:<44} 전체 n={len(lr):>4} {math.exp(lr.mean()):.2f}배 t={t:+7.2f} | 연도별 {yr} | 양(+)인 해 {pos}/{len(by)}")
    return t


def ols_hc(y, X):
    XtXi = np.linalg.pinv(X.T @ X)
    b = XtXi @ X.T @ y
    e = y - X @ b
    n, kk = X.shape
    meat = (X * e[:, None]).T @ (X * e[:, None]) * n / max(n - kk, 1)
    V = XtXi @ meat @ XtXi
    return b, np.sqrt(np.diag(V))


def day_frame(daycol, mincol, windows, min_bars=900):
    d = bars[bars[daycol].dt.dayofweek < 5]
    g = d.groupby(daycol)
    out = pd.DataFrame({"nb": g.size(), "vtot": g["v"].sum(), "atot": g["absr"].sum()})
    m = np.zeros(len(d), dtype=bool)
    for a, b in windows:
        m |= ((d[mincol] >= a) & (d[mincol] < b)).values
    gw = d[m].groupby(daycol)
    out["vwin"], out["awin"] = gw["v"].sum(), gw["absr"].sum()
    out = out[(out["nb"] >= min_bars) & (out["vtot"] > 0) & (out["vwin"] > 0)]
    out["share"] = out["vwin"] / out["vtot"]
    out["ashare"] = out["awin"] / out["atot"]
    return out


def did(frame, treat_cols, ctrl_cols, outcome, label, key=None):
    f = frame.copy()
    y = np.log(f[outcome] / (1 - f[outcome])).values            # 로짓 변환한 비중
    ym = pd.get_dummies(f.index.year.astype(str) + "-" + f.index.month.astype(str).str.zfill(2), drop_first=True).astype(float).values
    wd = pd.get_dummies(f.index.dayofweek, drop_first=True).astype(float).values
    T = f[treat_cols].astype(float).values
    C = f[ctrl_cols].astype(float).values if ctrl_cols else np.zeros((len(f), 0))
    X = np.column_stack([np.ones(len(f)), T, C, ym, wd])
    b, se = ols_hc(y, X)
    msg = []
    for i, c in enumerate(treat_cols):
        j = 1 + i
        n_t = int(f[c].sum())
        msg.append(f"{c}(n={n_t}) 계수 {b[j]:+.3f}(비중 {math.exp(b[j]) - 1:+.0%}) t={b[j] / se[j]:+.2f}")
        if key and i == 0:
            PVALS[key] = norm_p(b[j] / se[j])
    print(f"  {label:<40} " + " | ".join(msg) + f"  [일수 {len(f)}]")


# ------------------------------------------------------------------ 중국
print("\n" + "=" * 110 + "\n[중국] C1. 이벤트 거래량 점프 — 하루 단위 짝지은 로그비 (연도별 배수, 2자리 연도:배수)")
jump("상하이 개장 10:00~10:30 / 09:00~10:00 KST", "kd", "km", (600, 630), (540, 600))
jump("중국 점심 12:30~14:30 / 오전장 10:00~12:30", "kd", "km", (750, 870), (600, 750))
jump("오후 재개 14:30~15:00 / 점심 12:30~14:30", "kd", "km", (870, 900), (750, 870))

cn_hours = [(600, 750), (870, 960)]
fk = day_frame("kd", "km", cn_hours)
for name in ("CN", "UK", "US", "HK", "JP", "KR"):
    fk[name.lower()] = fk.index.isin(SET[name])
fk["cn_sf"] = fk["cn"] & fk.index.month.isin([1, 2])
fk["cn_nd"] = fk["cn"] & (fk.index.month == 10) & (fk.index.day <= 8)
fk["cn_other"] = fk["cn"] & ~fk["cn_sf"] & ~fk["cn_nd"]
fk["hk_only"] = fk["hk"] & ~fk["cn"]

print("\n[중국] C2. 중국 휴장일 DID — 중국 거래시간 거래량 비중 (로짓, 연-월·요일 고정효과, HC 표준오차). 통제: 미국·영국·일본·한국·홍콩단독 휴장")
did(fk, ["cn"], ["us", "uk", "jp", "kr", "hk_only"], "share", "전체 중국 휴장일", key="C2 중국휴장→중국시간대 거래량 비중")
did(fk, ["cn_sf", "cn_nd", "cn_other"], ["us", "uk", "jp", "kr", "hk_only"], "share", "휴장 종류별(설·국경절·그 밖)")
print("[중국] C3. 같은 DID — 중국 거래시간 변동성(|1분 수익률| 합) 비중")
did(fk, ["cn"], ["us", "uk", "jp", "kr", "hk_only"], "ashare", "전체 중국 휴장일(변동성)", key="C3 중국휴장→중국시간대 변동성 비중")
did(fk, ["cn_sf", "cn_nd", "cn_other"], ["us", "uk", "jp", "kr", "hk_only"], "ashare", "종류별(변동성)")

# C4/C5: 장기 연휴 전후
cal = pd.date_range("2019-12-01", "2026-11-30")
closed = pd.Series([(d.dayofweek >= 5) or (d in SET["CN"]) for d in cal], index=cal)
runs, start = [], None
for d, c in closed.items():
    if c and start is None:
        start = d
    if (not c) and start is not None:
        runs.append((start, d - pd.Timedelta(days=1))); start = None
long_runs = [(a, b) for a, b in runs if (b - a).days + 1 >= 3 and any(x in SET["CN"] for x in pd.date_range(a, b))]
post = {b + pd.Timedelta(days=1) for a, b in long_runs}
pre = {a - pd.Timedelta(days=1) for a, b in long_runs}
# 휴장 직후/직전이 '영업일'이 되도록 보정(다음/이전 평일)
def next_open(d):
    while closed.get(d, False): d += pd.Timedelta(days=1)
    return d
def prev_open(d):
    while closed.get(d, False): d -= pd.Timedelta(days=1)
    return d
post = {next_open(b + pd.Timedelta(days=1)) for a, b in long_runs}
pre = {prev_open(a - pd.Timedelta(days=1)) for a, b in long_runs}


def lvl(ts):
    idx = L.index.searchsorted(ts, side="right") - 1
    ok = (idx >= 0)
    idx = np.clip(idx, 0, len(L) - 1)
    lag = ts - L.index[idx]
    ok &= (lag <= pd.Timedelta("10min")) & ~pd.isna(ts)
    out = L.values[idx].astype(float)
    out[~ok] = np.nan
    return out


def kst_ts(days, minute):
    return pd.DatetimeIndex(days + pd.to_timedelta(minute, "m")).tz_localize("Asia/Seoul").tz_convert("UTC")


def ldn_ts(days, minute):
    return pd.DatetimeIndex(days + pd.to_timedelta(minute, "m")).tz_localize("Europe/London", ambiguous="NaT", nonexistent="NaT").tz_convert("UTC")


days_k = pd.DatetimeIndex(sorted(fk.index))
rcn = pd.Series((lvl(kst_ts(days_k, 960)) - lvl(kst_ts(days_k, 600))) * 1e4, index=days_k)   # 중국 낮장 수익률(bp)
rcn = rcn.dropna()
near = set()
for a, b in long_runs:
    for dd in pd.date_range(a - pd.Timedelta(days=3), b + pd.Timedelta(days=3)):
        near.add(dd)
normal = rcn[~rcn.index.isin(near)]
print(f"\n[중국] C4/C5. 장기 연휴(≥3일, {len(long_runs)}회) 전후 영업일의 중국 거래시간(10:00~16:00 KST) 수익률(bp) — 일반일 {len(normal)}일과 비교")


def welch(a, b):
    na, nb = len(a), len(b)
    t = (a.mean() - b.mean()) / math.sqrt(a.var(ddof=1) / na + b.var(ddof=1) / nb)
    return t, norm_p(t)


for lab, S_ in (("연휴 직후 첫 영업일", post), ("연휴 직전 영업일", pre)):
    x = rcn[rcn.index.isin(S_)]
    t_abs, p_abs = welch(x.abs(), normal.abs())
    t_sg, p_sg = welch(x, normal)
    print(f"  {lab:<20} n={len(x):>3} |수익률| {x.abs().mean():5.1f}bp vs 일반 {normal.abs().mean():5.1f}bp (Welch t={t_abs:+.2f}, p={p_abs:.3f}) | 부호 있는 평균 {x.mean():+5.1f}bp vs {normal.mean():+5.1f}bp (t={t_sg:+.2f}, p={p_sg:.3f}) | 상승한 날 {(x > 0).mean():.0%}")
    PVALS["C4 연휴 직후 |중국시간대 수익률|" if "직후" in lab else "C5 연휴 직전 |중국시간대 수익률|"] = p_abs

# ------------------------------------------------------------------ 런던
print("\n" + "=" * 110 + "\n[런던] L1. 이벤트 거래량 점프 (런던 현지시각, 연도별 배수)")
jump("런던 개장 08:00~08:30 / 07:00~08:00", "ld", "lm", (480, 510), (420, 480))
jump("LBMA 오전 고시 10:25~10:35 / 10:00~10:25", "ld", "lm", (625, 635), (600, 625))
jump("LBMA 오후 고시 14:55~15:05 / 14:30~14:55", "ld", "lm", (895, 905), (870, 895))
jump("(대조) 미국 지표 13:30~14:00 / 13:00~13:30", "ld", "lm", (810, 840), (780, 810))

lon_hours = [(480, 720)]
fl = day_frame("ld", "lm", lon_hours)
for name in ("CN", "UK", "US", "HK", "JP", "KR"):
    fl[name.lower()] = fl.index.isin(SET[name])
good_fri = {pd.Timestamp(d) for d, n in UK.items() if "Good Friday" in n}
xmas = lambda d: (d.month == 12 and d.day >= 24) or (d.month == 1 and d.day <= 2)
fl["uk_clean"] = fl["uk"].values & ~fl["us"].values & ~fl.index.isin(good_fri) & np.array([not xmas(d) for d in fl.index])
fl["uk_any"] = fl["uk"]
print(f"\n[런던] L2. 영국(잉글랜드) 단독 휴장일 DID — 런던 오전(08~12시) 거래량 비중. 처리일 {int(fl['uk_clean'].sum())}일 = 영국 휴장 중 미국 휴장·성금요일·12/24~1/2 제외")
did(fl, ["uk_clean"], ["us", "cn", "hk", "jp"], "share", "영국 단독 휴장일", key="L2 영국단독휴장→런던오전 거래량 비중")
did(fl, ["uk_any"], ["us", "cn", "hk", "jp"], "share", "영국 휴장 전체(참고)")
print("[런던] L3. 같은 DID — 런던 오전 변동성 비중")
did(fl, ["uk_clean"], ["us", "cn", "hk", "jp"], "ashare", "영국 단독 휴장일(변동성)", key="L3 영국단독휴장→런던오전 변동성 비중")

print("\n[대조] 방법 점검 (이 DID 가 실제 효과를 잡아내는가 / 특이성)")
us_hours = [(780, 1260)]   # 런던 13:00~21:00 = 미국 08:00~16:00 ET 근처
fu = day_frame("ld", "lm", us_hours)
fu["us"] = fu.index.isin(SET["US"]); fu["uk"] = fu.index.isin(SET["UK"]); fu["cn"] = fu.index.isin(SET["CN"])
did(fu, ["us"], ["uk", "cn"], "share", "미국 휴장일 → 미국 시간대 비중(양성 대조)")
did(fl, ["cn"], ["us", "uk", "hk", "jp"], "share", "중국 휴장일 → 런던 오전 비중(특이성)")
did(fk, ["uk_clean" if "uk_clean" in fk else "uk"], ["us", "cn", "jp", "kr", "hk_only"], "share", "영국 휴장일 → 중국 시간대 비중(특이성)")

print("\n[런던] L4/L5. LBMA 고시 전후 30분 수익률(bp), 런던 현지 평일 — 일 단위 t, 연도별 평균 부호")
days_l = pd.DatetimeIndex(sorted(fl.index))
allret = {}
for lab, fix in (("오전 고시 10:30", 630), ("오후 고시 15:00", 900)):
    pre_r = (lvl(ldn_ts(days_l, fix)) - lvl(ldn_ts(days_l, fix - 30))) * 1e4
    post_r = (lvl(ldn_ts(days_l, fix + 30)) - lvl(ldn_ts(days_l, fix))) * 1e4
    for w, arr in (("전 30분", pre_r), ("후 30분", post_r)):
        s = pd.Series(arr, index=days_l).dropna()
        t = s.mean() / (s.std() / math.sqrt(len(s)))
        yr = s.groupby(s.index.year).mean()
        print(f"  {lab} {w}: n={len(s):>4} 평균 {s.mean():+6.2f}bp t={t:+6.2f} p={norm_p(t):.3f} | 연도별 평균 " + " ".join(f"{y % 100:02d}:{v:+.1f}" for y, v in yr.items()) + f" | 양(+)인 해 {(yr > 0).sum()}/{len(yr)}")
        PVALS[f"L{'4' if fix == 630 else '5'} LBMA {lab[:2]} 고시 {w}"] = norm_p(t)

# ------------------------------------------------------------------ 다중비교
print("\n" + "=" * 110 + "\n다중비교 — 주요 검정 Holm 보정 (유의수준 0.05)")
adj = holm(PVALS)
for kk in PVALS:
    print(f"  {kk:<44} p={PVALS[kk]:.4f}  Holm 보정 p={adj[kk]:.4f}  {'유의' if adj[kk] < 0.05 else '유의하지 않음'}")

# ------------------------------------------------------------------ 장기 일봉: 달러 광의지수의 비-DXY 성분
print("\n" + "=" * 110 + "\n[장기 일봉] 금 일수익률 ~ 2년물 + DXY (+ 달러 광의지수 중 DXY 로 설명 안 되는 성분: 위안·멕시코·캐나다 등), 2006~2026")
tab = None
for f in glob.glob(DAILY_DIR + "*/*/*.csv"):
    t = pd.read_csv(f)
    if t.shape[1] == 9:
        tab = t
        break
if tab is not None:
    tab.columns = ["date", "gold", "dxy", "eur", "jpy", "y2", "y10", "r10", "broad"]
    tab["date"] = pd.to_datetime(tab["date"])
    tab = tab.set_index("date").loc["2006-01-01":]
    tab = tab.dropna(subset=["gold", "dxy", "y2", "broad"])
    ret = pd.DataFrame({
        "gold": np.log(tab["gold"]).diff() * 100, "dxy": np.log(tab["dxy"]).diff() * 100,
        "d2y": tab["y2"].diff() * 100, "broad": np.log(tab["broad"]).diff() * 100}).dropna()
    # DXY 로 설명되는 부분을 빼고 남은 광의지수 성분
    Xb = np.column_stack([np.ones(len(ret)), ret["dxy"].values])
    bb = np.linalg.lstsq(Xb, ret["broad"].values, rcond=None)[0]
    ret["broad_res"] = ret["broad"].values - Xb @ bb
    print(f"  광의지수 일수익률의 DXY 로 설명되는 비율(R²): {1 - (ret['broad_res'] ** 2).sum() / ((ret['broad'] - ret['broad'].mean()) ** 2).sum():.2f}")

    def nw(y, X, lag=5):
        n = len(y)
        Xc = np.column_stack([np.ones(n), X])
        b = np.linalg.lstsq(Xc, y, rcond=None)[0]
        e = y - Xc @ b
        XtXi = np.linalg.inv(Xc.T @ Xc)
        Sm = (Xc * e[:, None]).T @ (Xc * e[:, None])
        for l in range(1, lag + 1):
            G = (Xc[l:] * e[l:, None]).T @ (Xc[:-l] * e[:-l, None])
            Sm += (1 - l / (lag + 1)) * (G + G.T)
        V = XtXi @ Sm @ XtXi
        r2 = 1 - (e @ e) / ((y - y.mean()) ** 2).sum()
        return b, np.sqrt(np.diag(V)), r2
    for lab, a, b_ in (("2006~2026", "2006", "2026"), ("2006~2015", "2006", "2015"), ("2016~2026", "2016", "2026"), ("2020~2026", "2020", "2026")):
        s = ret.loc[a:b_]
        y = s["gold"].values
        b0, se0, r20 = nw(y, s[["d2y", "dxy"]].values)
        b1, se1, r21 = nw(y, s[["d2y", "dxy", "broad_res"]].values)
        print(f"  {lab}: n={len(s):>5} R² {r20:.3f} → {r21:.3f} (Δ={r21 - r20:+.4f}) 광의지수 비-DXY 성분 계수 {b1[3]:+.3f} (HAC t={b1[3] / se1[3]:+.2f})")
    print("  (주의: 광의지수는 뉴욕 정오 기준, 금은 뉴욕 17시 종가라 시각이 다름. 위안 비중은 일부일 뿐이라 '중국 고유'가 아니라 비-DXY 달러 성분)")
else:
    print("  일봉 통합 파일을 찾지 못했다.")
