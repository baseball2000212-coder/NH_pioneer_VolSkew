"""필터형(U3)·합의형(U1)·독립(U0)을 '중국장 변동성' 게이트로 운영하면? (게이트만 바꿔 v3_unified.py 와 비교)
 게이트 V : 20일 변동성≥20 (규칙 2), 중국장 RV≥확장 중앙값 (규칙 3 단독)  ← v3_unified 의 기준
 게이트 CM: 두 규칙 모두 중국장 RV ≥ 직전까지의 확장 중앙값
 게이트 CP: 두 규칙 모두 중국장 RV ≥ 직전까지의 확장 67 백분위
 게이트 C+V: 규칙 2 는 (중국장 RV 중앙값 이상 AND 20일 변동성≥20)
실행: uv run python -I week03/work/v3_cngate.py > week03/work/v3_cngate_out.txt
"""
from pathlib import Path
_src = (Path(__file__).resolve().parent / "v3_unified.py").read_text(encoding="utf-8").split("rows = {}")[0]
exec(compile(_src, "v3_unified_head", "exec"))

cmed = cn >= cn.expanding(120).median().shift(1)
cp67 = cn >= cn.expanding(120).quantile(0.67).shift(1)
m = to_kst_day(gc)
MODES = ["V", "CM", "CP", "C+V"]
rowsM = {k: {} for k in MODES}
for day, x in m.groupby("day", sort=True):
    if day < pd.Timestamp("2020-02-03") or day > pd.Timestamp("2026-10-30"):
        continue
    if x["ct"].nunique() > 1 or x["roll"].sum() > 0:
        continue
    v20 = vol20.reindex([day], method="ffill").values[0]
    if np.isnan(v20):
        continue
    a, b, c = bool(cmed.get(day, False)), bool(cp67.get(day, False)), bool(cn_gate.get(day, False))
    args = {"V": (v20, c), "CM": (99 if a else 0, a), "CP": (99 if b else 0, b), "C+V": (v20 if a else 0, a)}
    for k in MODES:
        rowsM[k][day] = day_pnl(x, *args[k])[0]

PER = {"2020~2023": ("2020-02-03", "2023-12-31"), "2024~2025": ("2024-01-01", "2025-12-31"), "2020~2025": ("2020-02-03", "2025-12-31"), "2026": ("2026-01-01", "2026-10-31")}
for k in MODES:
    r = rowsM[k]
    P = pd.DataFrame({u: pd.Series({d: v[u][0] for d, v in r.items() if u in v}) for u in ("U0G", "U1G", "U3G")}).fillna(0.0)
    N = pd.DataFrame({u: pd.Series({d: v[u][1] for d, v in r.items() if u in v}) for u in ("U0G", "U1G", "U3G")}).fillna(0)
    print("=" * 130 + f"\n게이트 {k}")
    for pl, (a0, b0) in PER.items():
        print(f"[{pl}]")
        for u, lab in (("U0G", "독립"), ("U1G", "합의형"), ("U3G", "필터형")):
            x, n = P[u].loc[a0:b0], N[u].loc[a0:b0]; td = x[n > 0]
            if len(td) < 20:
                print(f"  {lab:<5} 거래일 {len(td)}"); continue
            cum = x.cumsum(); dd = (cum - cum.cummax()).min()
            print(f"  {lab:<5} 거래일 {len(td):>4} 계약/일 {n[n > 0].mean():.2f} 하루 {td.mean():+7.1f}$ t={td.mean() / (td.std() / math.sqrt(len(td))):+5.2f} 승일 {(td > 0).mean():.0%} 누적 {x.sum():+8.0f}$ MDD {dd:+7.0f}$ 최악 {td.min():+6.0f}$")
    on2 = pd.Series({d: 1 if ("U3G" in v and v["U3G"][1] >= 1) else 0 for d, v in r.items()})
    # 규칙 2 가 켜진 날 비율 (게이트 통과)
    g2 = pd.Series({d: (v["U0G"][1] if "U0G" in v else 0) for d, v in r.items()})
    print(f"  (거래일 중 계약 있는 날 비율 2020~2025 {(g2.loc[:'2025'] > 0).mean():.0%}, 2026 {(g2.loc['2026':] > 0).mean():.0%})")
