import json
from datetime import datetime, timedelta

import pandas as pd

from bars import minute_from_rows
from brokers import Q, SimBroker
from engine import Engine
from helpers import CFG, make_bars
from nhapi.journal import Journal
from replay_check import compare
from signals import SignalModel
from summarize_journal import read_run, summarize
from test_engine import Clock, Kill, model_always


def minute_rows(n=700, start="2026-11-17 12:00"):
    bars = make_bars(n, start=start, step=1)
    return [{"ref_dt": t.strftime("%Y%m%d"), "exch_tm": t.strftime("%H%M%S"), "open_pric": str(r.open), "high_pric": str(r.high),
             "low_pric": str(r.low), "close_pric": str(r.close), "cum_trd_qty": str(r.volume), "glb_biz_dt": "", "cum_trd_amt": ""}
            for t, r in bars.iterrows()]


def test_engine_with_real_journal_then_summarize_and_replay(tmp_path):
    rows = minute_rows()
    clock = Clock(datetime(2026, 11, 17, 22, 0, 30))
    state = {"q": Q(7500.0, 7500.25)}
    full = minute_from_rows(rows)
    broker = SimBroker(lambda s: state["q"], lambda s: [r for r in rows if pd.Timestamp(r["ref_dt"] + r["exch_tm"]) < pd.Timestamp(clock())])
    journal = Journal(tmp_path / "t.jsonl", "t-1", env="paper", mode="paper-sim", sym="MESZ26")
    bars_out = []
    eng = Engine(broker, CFG, model_always(1), sym="MESZ26", source="paper-sim", clock=clock, sleep=clock.sleep, log=journal, kill=Kill(),
                 h=3, gate=False, bars_out=bars_out)
    eng.startup()
    eng.tick(clock())
    state["q"] = Q(7499.5, 7500.0)
    clock.sleep(1)
    eng.tick(clock())
    clock.t = eng.pos.exit_by + timedelta(seconds=1)
    eng.tick(clock())
    eng.finish("test")
    recs = read_run(tmp_path / "t.jsonl", "t-1")
    s = summarize(recs)
    assert s["trades"] == 1 and s["fills_by_source"] == {"paper-sim": 2} and s["bars_evaluated"] == 1
    assert all({"run_id", "ts", "env", "mode", "sym"} <= set(r) for r in recs)

    # 같은 1분봉이면 리플레이가 라이브 기록과 100% 일치 (모형은 항상 롱이라 특징만 비교)
    live = pd.DataFrame(bars_out)
    res = compare(live, full, model_always(1), CFG, gate=False)
    assert res["match_ratio"] == 1.0 and res["max_abs_diff"] < 1e-4   # 기록은 소수 4~6자리로 반올림
    # 마지막 분봉이 바뀌면(늦게 확정된 봉) 불일치로 잡힌다
    changed = full.copy()
    changed.iloc[-300:, changed.columns.get_loc("close")] += 5
    assert compare(live, changed, model_always(1), CFG, gate=False)["match_ratio"] < 1.0
