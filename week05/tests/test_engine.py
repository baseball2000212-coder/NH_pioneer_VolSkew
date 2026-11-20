from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from brokers import Q, SimBroker
from engine import HOLD, IDLE, Engine, entry_allowed, stop_distance, stop_hit
from helpers import CFG, make_bars
from signals import FEATURES, SignalModel, T2Chart


def test_stop_distance_and_hit():
    d = stop_distance(5.0, 7500.0, 3, 3.0)
    assert abs(d - 3.0 * 5e-4 * 7500 * 3 ** 0.5) < 1e-9
    assert stop_hit(1, 7500, 10, bid=7489.75, ask=7490) and not stop_hit(1, 7500, 10, bid=7491, ask=7491.25)
    assert stop_hit(-1, 7500, 10, bid=7509, ask=7510.25) and not stop_hit(-1, 7500, 10, bid=7500, ask=7500.25)


def base(**kw):
    t = datetime(2026, 11, 17, 22, 0)
    args = dict(now=t, entry_time=t + timedelta(minutes=5), h=3, bar_min=5, flatten=datetime(2026, 11, 17, 23, 40),
                window=("21:30", "23:40"), trades_today=0, max_trades=12, realized_usd=0.0, daily_limit_usd=200.0,
                one_stop_usd=50.0, halted=False)
    args.update(kw)
    return entry_allowed(**args)


def test_entry_limits():
    assert base() == (True, "")
    assert base(halted=True)[1] == "halted"
    assert base(entry_time=datetime(2026, 11, 17, 21, 0))[1] == "outside_window"
    assert base(entry_time=datetime(2026, 11, 17, 23, 30))[1] == "no_time_before_flatten"
    assert base(trades_today=12)[1] == "max_trades"
    assert base(realized_usd=-160.0, one_stop_usd=50.0)[1] == "daily_loss_limit"     # 남은 한도 40 < 1회 손절 50
    assert base(realized_usd=-100.0, one_stop_usd=50.0)[0] is True


# ---- 엔진 통합 (가짜 시세 + SimBroker)

class Log:
    def __init__(self):
        self.rows = []

    def __getattr__(self, name):
        def add(*a, **k):
            self.rows.append((name, a, k))
        return add

    def kinds(self, kind):
        return [r for r in self.rows if r[0] == kind]


class Kill:
    def __init__(self):
        self.on = False
        self.reason = "test"

    def triggered(self):
        return self.on


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def model_always(side):
    m = SignalModel(tuple(FEATURES), np.zeros(6), np.ones(6), np.array([-1, 0, 1]), np.zeros((3, 6)),
                    np.array([1.0 if side == -1 else 0.0, 0.0, 1.0 if side == 1 else 0.0]), T2Chart(np.zeros(6), np.eye(6), 1e9), {"h": 3})
    return m


def build(side=1, quote=Q(7500.0, 7500.25), start=datetime(2026, 11, 17, 22, 0, 30)):
    clock = Clock(start)
    state = {"q": quote}
    bars = make_bars(700, start="2026-11-17 12:00", step=1)
    rows = [{"ref_dt": t.strftime("%Y%m%d"), "exch_tm": t.strftime("%H%M%S"), "open_pric": str(r.open), "high_pric": str(r.high),
             "low_pric": str(r.low), "close_pric": str(r.close), "cum_trd_qty": str(r.volume), "glb_biz_dt": "", "cum_trd_amt": "", "_t": t}
            for t, r in bars.iterrows()]
    broker = SimBroker(lambda s: state["q"], lambda s: [{k: v for k, v in x.items() if k != "_t"} for x in rows if x["_t"] < pd.Timestamp(clock())])
    log, kill = Log(), Kill()
    eng = Engine(broker, CFG, model_always(side), sym="MESZ26", source="paper-sim", clock=clock, sleep=clock.sleep, log=log, kill=kill,
                 h=3, gate=False)
    return eng, broker, log, kill, clock, state


def test_startup_refuses_existing_position():
    eng, broker, log, *_ = build()
    broker.pos["MESZ26"] = 1
    assert eng.startup() is False and log.kinds("error")


def open_position(eng, clock, state):
    eng.startup()
    eng.tick(clock())
    assert eng.state == "PENDING"
    state["q"] = Q(7499.5, 7500.0)                  # ask 가 지정가(7500.0)에 닿아 체결
    clock.sleep(1)
    eng.tick(clock())
    assert eng.state == HOLD


def test_entry_fill_then_time_exit_and_flat():
    eng, broker, log, kill, clock, state = build(side=1)
    eng.startup()
    eng.tick(clock())
    assert eng.state == "PENDING" and log.kinds("order")[0][2]["price"] == 7500.0   # 같은 쪽 최우선 호가에 지정가
    state["q"] = Q(7499.5, 7500.0)
    clock.sleep(1)
    eng.tick(clock())
    assert eng.state == HOLD and eng.pos.entry_px == 7500.0
    clock.t = eng.pos.exit_by + timedelta(seconds=1)
    eng.tick(clock())
    assert eng.state == IDLE and broker.positions() == {}
    t = log.kinds("trade")[0][2]
    assert t["reason"] == "time" and eng.stats.trades == 1


def test_entry_timeout_cancels_without_position():
    eng, broker, log, kill, clock, state = build(side=1)
    eng.startup()
    eng.tick(clock())
    clock.sleep(CFG["risk"]["entry_timeout_sec"] + 1)
    eng.tick(clock())
    assert eng.state == IDLE and broker.positions() == {} and not broker.open_orders()
    assert log.kinds("order")[-1][0] == "order" and log.kinds("order")[-1][1][0] == "entry_cancel"


def test_stop_exit():
    eng, broker, log, kill, clock, state = build(side=1)
    open_position(eng, clock, state)
    state["q"] = Q(eng.pos.entry_px - eng.pos.stop_dist - 1, eng.pos.entry_px - eng.pos.stop_dist)
    eng.tick(clock())
    assert log.kinds("trade")[-1][2]["reason"] == "stop" and broker.positions() == {}


def test_kill_switch_flattens_and_ends():
    eng, broker, log, kill, clock, state = build(side=1)
    open_position(eng, clock, state)
    kill.on = True
    eng.resume = True                             # run() 이 startup 을 다시 부르므로 보유 이어받기로
    summary = eng.run()
    assert summary["flat"] and broker.positions() == {} and "kill" in summary["reason"]


def test_recon_mismatch_halts_and_flattens():
    eng, broker, log, kill, clock, state = build()
    eng.startup()
    broker.pos["MESZ26"] = 1                      # 엔진이 모르는 보유가 생김
    clock.sleep(31)
    eng.reconcile(clock())
    assert eng.halted and broker.positions() == {} and log.kinds("error")[0][1][0] == "recon_mismatch"


def test_unknown_ack_halts_entries():
    eng, broker, log, kill, clock, state = build()
    eng.startup()
    broker.unknown_next = 1
    broker.resolve = lambda ack: ack              # 판정 실패
    eng.tick(clock())
    assert eng.halted and eng.state == IDLE


def test_stale_signal_is_skipped():
    eng, broker, log, kill, clock, state = build(side=1, start=datetime(2026, 11, 17, 22, 3, 0))   # 21:55 봉은 22:00 에 끝남 → 3분 늦음
    eng.startup()
    eng.tick(clock())
    assert eng.state == IDLE and eng.stats.skipped.get("stale_signal") == 1 and not log.kinds("order")


def test_bars_recorded_while_holding_without_new_entry():
    eng, broker, log, kill, clock, state = build(side=1)
    open_position(eng, clock, state)
    n = len(eng.bars_out)
    clock.sleep(300)                                  # 다음 5분봉이 완성됨
    eng.tick(clock())
    assert len(eng.bars_out) == n + 1 and eng.stats.signals == 1
