"""규칙 2(아시아 돌파 + 공정가치 확인) 검증 — 독립 재구현(team_rules_check3.run) 사용, 팀원 코드 실행 없음.

6E(유로)는 2025-12 부터만 있어서 2020~2025 는 'ZT 만 쓴 확인'까지만 가능하다(규칙서도 같은 한계를 적음).
사전 고정 검정:
 R1 돌파만(확인 없음): 연도별 건당 순 bp, t
 R2 ZT 확인 통과 거래: 연도별
 R3 확인의 정보량: 통과 vs 탈락 거래 평균 차이(Welch t) + 무작위로 같은 비율을 고른 대조(1,000회) 대비 p
 R4 부호를 뒤집은 확인 (규칙서 주장: 손실)
 R5 확인 창 길이 15/30/60분 민감도 (고원 여부)
 R6 2026 에서 ZT+6E 전체 규칙 (재현 확인)
실행: uv run python -I week03/work/v3_rule2.py > week03/work/v3_rule2_out.txt
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

M = HERE.parent / "data" / "team_macro" / "minute"
rng = np.random.default_rng(3)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

gc, zt, e6 = (load_team(M / f) for f in ("GC.csv.gz", "ZT.csv.gz", "6E.csv.gz"))
zl, el = log_series(zt), log_series(e6)
zero = pd.Series(0.0, index=zl.index)


def line(x: pd.Series) -> str:
    if len(x) < 5:
        return f"n={len(x)}"
    t = x.mean() / (x.std() / math.sqrt(len(x)))
    return f"n={len(x):>3} 건당 {x.mean():+6.1f}bp t={t:+5.2f} 승률 {(x > 0).mean():.0%}"


t_all, both = run(gc, zl, zero, confirm=False)
print(f"돌파 전체 {len(t_all)}건 (양방향 동시 돌파로 건너뜀 {both}일), 기간 {t_all.index[0]:%Y-%m-%d}~{t_all.index[-1]:%Y-%m-%d}")
t_zt, _ = run(gc, zl, zero, confirm=True, b2=0.0)
years = [str(y) for y in range(2020, 2027)]

print("\n== R1 돌파만 (확인 없음), 비용 $3.5 포함")
for y in years:
    print(f"  {y}: {line(t_all.loc[y]['bp'])}")
print(f"  2020~2025: {line(t_all.loc['2020':'2025']['bp'])}")
print("== R2 ZT 확인 통과 (6E 없이)")
for y in years:
    print(f"  {y}: {line(t_zt.loc[y]['bp'])}")
print(f"  2020~2025: {line(t_zt.loc['2020':'2025']['bp'])}")

print("\n== R3 확인의 정보량 (통과 vs 탈락), 기간별")
for lab, (a, b) in {"2020~2023": ("2020", "2023"), "2024~2025": ("2024", "2025"), "2026": ("2026", "2026"), "2020~2025": ("2020", "2025")}.items():
    ta = t_all.loc[a:b]
    keep = ta.index.isin(t_zt.index)
    k, d = ta[keep]["bp"], ta[~keep]["bp"]
    if len(k) < 5 or len(d) < 5:
        print(f"  {lab}: 표본 부족 (통과 {len(k)}, 탈락 {len(d)})"); continue
    welch = (k.mean() - d.mean()) / math.sqrt(k.var() / len(k) + d.var() / len(d))
    obs = k.mean()
    null = np.array([ta["bp"].sample(len(k), random_state=int(rng.integers(1e9))).mean() for _ in range(1000)])
    print(f"  {lab}: 통과 {line(k)} | 탈락 {line(d)} | 차이 Welch t={welch:+.2f} | 같은 비율 무작위 대비 p={(null >= obs).mean():.3f}")

print("\n== R4 부호 뒤집은 확인 (규칙서: 손실)")
t_flip, _ = run(gc, zl, zero, confirm=True, b1=-0.675 * -1, b2=0.0)
for lab, (a, b) in {"2020~2025": ("2020", "2025"), "2026": ("2026", "2026")}.items():
    print(f"  {lab}: {line(t_flip.loc[a:b]['bp'])}")

print("\n== R5 확인 창 길이 민감도 (ZT 만, 2020~2025 / 2026)")
for w in (15, 30, 60):
    tw, _ = run(gc, zl, zero, confirm=True, window=w, b2=0.0)
    print(f"  창 {w:>2}분: 2020~2025 {line(tw.loc['2020':'2025']['bp'])} | 2026 {line(tw.loc['2026']['bp'])}")

print("\n== R6 2026 전체 규칙(ZT+6E) 재현")
t_full, _ = run(gc, zl, el, confirm=True)
for lab, (a, b) in {"2026.2~6": ("2026-02-01", "2026-06-30"), "2026.7~10": ("2026-07-01", "2026-10-31"), "2026.10": ("2026-10-01", "2026-10-31")}.items():
    print(f"  {lab}: {line(t_full.loc[a:b]['bp'])}")
print("  (규칙서: 2~6월 +34.0bp 49건, 7~10월 +29.5bp 47건, 10월 −11.1bp 11건)")

# 손절 vs 청산 구성, 2020~2025
x = t_zt.loc["2020":"2025"]
print(f"\n2020~2025 ZT 확인 통과 {len(x)}건: 손절 {int(x['stopped'].sum())}건({x['stopped'].mean():.0%}), 손절 평균 {x[x['stopped']==1]['bp'].mean():+.1f}bp, 23:50 청산 평균 {x[x['stopped']==0]['bp'].mean():+.1f}bp")
