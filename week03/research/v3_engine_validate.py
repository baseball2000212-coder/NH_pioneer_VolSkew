"""제출 엔진(submission/strategy_core.py·backtest.py)이 연구 코드(v3_unified/v3_cngate 의 필터형 U3, 중국장 67 백분위 게이트)와 같은 결과를 내는지 대조.

차이가 날 수밖에 없는 곳(의도한 것):
 - 서머타임 구간 규칙 3: 연구 코드는 '규칙 2 가 나중에(21:30~22:00) 진입할 날'을 미리 알고 규칙 3 을 건너뛰는 룩어헤드가 있었다. 엔진은 규칙 3 진입 시점에 규칙 2 포지션이 없으면 진입한다.
 - 규칙 2 23:50 청산: 연구는 23:49 봉 종가, 엔진은 23:50 봉 시가.
엔진 쪽 위험 한도·비상 손절·슬리피지는 이 대조에서는 끕니다.
실행: uv run python -I week03/work/v3_engine_validate.py > week03/work/v3_engine_validate_out.txt
"""
import contextlib
import copy
import io
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SUB = HERE.parent / "submission"
sys.path.insert(0, str(SUB)); sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

buf = io.StringIO()
ns = {"__file__": str(HERE / "v3_cngate.py"), "__name__": "v3_cngate_exec"}
with contextlib.redirect_stdout(buf):
    exec(compile((HERE / "v3_cngate.py").read_text(encoding="utf-8"), "v3_cngate.py", "exec"), ns)
research = pd.Series({d: v["U3G"][0] for d, v in ns["rowsM"]["CP"].items() if "U3G" in v})
research_n = pd.Series({d: v["U3G"][1] for d, v in ns["rowsM"]["CP"].items() if "U3G" in v})
cn_log = ns["cn"]
gc = ns["gc"]; zt_df = ns["zt"]

from backtest import run_backtest, summarize   # noqa: E402
from feeds import ZtSeries                      # noqa: E402
from gate_store import GateStore                # noqa: E402
from strategy_core import Bar                   # noqa: E402

cfg = json.loads((SUB / "strategy.json").read_text(encoding="utf-8"))
cfg["risk"]["daily_loss_limit"] = 1e9
cfg["risk"]["rule3_emergency_stop_usd"] = 0
cfg["costs"]["slippage_ticks"] = 0

m = ns["to_kst_day"](gc)
roll_days = set()
for day, x in m.groupby("day"):
    if x["ct"].nunique() > 1 or x["roll"].sum() > 0:
        roll_days.add(day.date())
store = GateStore()
for d in roll_days:                       # 월물 교체일: 거래는 안 하지만 연구 코드는 이력에 넣는다(롤 점프를 0 으로 둔 RV)
    if pd.Timestamp(d) in cn_log.index:
        store.add(d, float(np.exp(cn_log.loc[pd.Timestamp(d)])))
idx = gc.index
bars = [Bar(t.to_pydatetime(), o, h, l, c) for t, o, h, l, c, ct, roll in zip(idx, gc["o"], gc["h"], gc["l"], gc["c"], gc["ct"], gc["roll"])]
from sessions import kst_day              # noqa: E402
bars = [b for b in bars if kst_day(b.ts) not in roll_days]
zt = ZtSeries([Bar(t.to_pydatetime(), o, h, l, c) for t, o, h, l, c in zip(zt_df.index, zt_df["o"], zt_df["h"], zt_df["l"], zt_df["c"])])
print(f"봉 {len(bars):,}개, 월물 교체일 {len(roll_days)}일 제외")
trades, daily = run_backtest(bars, zt, cfg, store, start=datetime(2020, 2, 3).date(), end=datetime(2026, 10, 30).date())

eng = pd.Series({pd.Timestamp(d["day"]): d["pnl"] for d in daily})
eng_n = pd.Series({pd.Timestamp(d["day"]): d["n_trades"] for d in daily})
print(f"엔진 거래일 {int((eng_n > 0).sum())}, 연구 거래일 {int((research_n > 0).sum())}")

# 중국장 RV 대조
rv_e = pd.Series({pd.Timestamp(d["day"]): d["rv"] for d in daily if d["rv"] > 0})
rv_r = np.exp(cn_log).reindex(rv_e.index)
ok = (np.abs(rv_e - rv_r) / rv_r < 1e-6).mean()
print(f"중국장 RV 일치율(상대오차<1e-6): {ok:.1%} ({len(rv_e)}일)")

# 일별 손익 대조
allidx = sorted(set(eng.index) | set(research.index))
e = eng.reindex(allidx).fillna(0.0); r = research.reindex(allidx).fillna(0.0)
same = (np.abs(e - r) < 0.5)
print(f"일별 손익 일치(±$0.5): {same.mean():.1%} ({len(allidx)}일), 불일치 {int((~same).sum())}일")
dif = (e - r)[~same]
win_dst = lambda ts: ts.month in (3, 4, 5, 6, 7, 8, 9, 10)
print("불일치 일의 월 분포:", dict(pd.Series([t.month for t in dif.index]).value_counts().sort_index()))
for lab, a, b in (("2020~2025", "2020-02-03", "2025-12-31"), ("2026", "2026-01-01", "2026-10-31")):
    ee, rr = e.loc[a:b], r.loc[a:b]
    en, rn = eng_n.loc[a:b], research_n.reindex(allidx).fillna(0).loc[a:b]
    td_e, td_r = ee[en > 0], rr[rn > 0]
    print(f"[{lab}] 엔진: 거래일 {len(td_e)} 하루 {td_e.mean():+.1f}$ t={td_e.mean() / (td_e.std() / math.sqrt(len(td_e))):+.2f} 합 {ee.sum():+.0f}$ | 연구: 거래일 {len(td_r)} 하루 {td_r.mean():+.1f}$ t={td_r.mean() / (td_r.std() / math.sqrt(len(td_r))):+.2f} 합 {rr.sum():+.0f}$")
print("엔진 요약:", json.dumps(summarize(trades, daily), ensure_ascii=False))

# 불일치 분해
en_all, rn_all = eng_n.reindex(allidx).fillna(0) > 0, research_n.reindex(allidx).fillna(0) > 0
diff_trade = en_all != rn_all
print(f"거래 여부가 다른 날 {int(diff_trade.sum())}일 (엔진만 {int((en_all & ~rn_all).sum())}, 연구만 {int((~en_all & rn_all).sum())}), 손익 합 차이 {float((e - r)[diff_trade].sum()):+.0f}$")
small = (e - r).abs()[~same & ~diff_trade]
print(f"둘 다 거래했지만 손익이 다른 날 {len(small)}일: 평균 차이 ${small.mean():.2f}, 최대 ${small.max():.2f}, 합 {float((e - r)[~same & ~diff_trade].sum()):+.0f}$")
months = pd.Series([t.month for t in (e - r)[diff_trade].index]).value_counts().sort_index()
print("거래 여부 다른 날의 월 분포:", {int(k): int(v) for k, v in months.items()})
