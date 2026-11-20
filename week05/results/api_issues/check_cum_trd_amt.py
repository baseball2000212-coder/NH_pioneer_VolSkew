"""제보 2 과거 집계: results/data/minute_*.csv.gz 에서 cum_trd_amt 9자리 잘림 횟수 (API 호출 없음).

판정: 직전 값 >= 9억 이고 다음 값이 1.0억~1.2억(9자리) → 잘림. 08:00(여름 07:00) 세션 초기화는 직전 값이 9억 미만이라 제외된다.
검증: 잘림 앞뒤 봉의 증가분 비율 d(cum_trd_amt) / (cum_trd_qty * close) 가 1/10 로 바뀌는지.
"""
import glob
from pathlib import Path
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
for f in sorted(glob.glob(str(DATA / "minute_*.csv.gz"))):
    m = pd.read_csv(f).sort_values(["ref_dt", "exch_tm"]).reset_index(drop=True)
    a = m.cum_trd_amt.astype("int64"); p = a.shift().fillna(0).astype("int64")
    trunc = (a < p) & (p >= 9e8) & (a >= 1e8) & (a < 1.2e8)
    r = (a - p) / (m.cum_trd_qty * m.close_pric)
    ok = tot = 0
    for i in m.index[trunc]:
        if 0 < i < len(m) - 1 and m.cum_trd_qty[i + 1] > 0 and m.cum_trd_qty[i - 1] > 0:
            tot += 1; ok += 0.08 < r[i + 1] / r[i - 1] < 0.125
    late = m.ref_dt >= 20261102
    print(f"{Path(f).name}: 잘림 {int(trunc.sum())}회 (11/02~ {int((trunc & late).sum())}회), 다음 봉 비율 1/10: {ok}/{tot}")
