"""submission/data 만들기: (1) 게이트 이력 시드(GC 1분봉에서 중국장 RV 계산) (2) NH 분봉 수집 파일을 CSV.gz 로.
실행: uv run python -I week03/work/make_submission_data.py
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUB = ROOT / "week03" / "submission"
sys.path.insert(0, str(SUB))
from feeds import load_nh_json, save_csv   # noqa: E402

GC = ROOT / "week03" / "data" / "team_macro" / "minute" / "GC.csv.gz"
raw = pd.read_csv(GC, usecols=[0, 2, 6, 8], header=0, names=["utc", "ct", "c", "roll"])
raw.index = pd.to_datetime(raw["utc"], utc=True)
raw = raw[~raw.index.duplicated()].sort_index()
r1 = np.log(raw["c"]).diff()
r1[(raw["roll"] == 1) | (raw["ct"] != raw["ct"].shift())] = 0.0
r1 = r1.fillna(0.0)
k = raw.index.tz_convert("Asia/Seoul")
f = pd.DataFrame({"r2": r1.values ** 2, "day": k.normalize().tz_localize(None), "hm": k.hour * 60 + k.minute}, index=raw.index)
cn = f[(f.hm >= 600) & (f.hm < 955)].groupby("day")["r2"].sum()
cn = cn[(cn > 0) & (cn.index.dayofweek < 5)]
out = SUB / "data" / "gate_history_seed.csv"
with out.open("w", encoding="utf-8", newline="\n") as h:
    h.write("day,cn_rv\n")
    for d, v in cn.items():
        h.write(f"{d.date().isoformat()},{v!r}\n")
print("시드", len(cn), "일", cn.index[0].date(), "~", cn.index[-1].date())
for sym in ("MGCZ26", "ZTZ26"):
    bars = load_nh_json(ROOT / "week03" / "data" / "nh_china" / f"minute_{sym}.json")
    save_csv(SUB / "data" / f"{sym}_1m.csv.gz", bars)
    print(sym, len(bars), bars[0].ts, bars[-1].ts)
