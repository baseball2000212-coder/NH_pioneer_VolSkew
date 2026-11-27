"""6주차 SplitTrader 테스트 — 가짜 시계·시세·브로커로 분할 진입·분할 손절·조각 감시를 API 없이 돈다."""
import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from nhapi import Journal, KillSwitch, RiskLimits, RiskManager, read_journal
from nhapi.timeutil import KST
from runner import ExecResult, Snapshot
from runner_w06 import SplitPlan, SplitTrader
import split_exec as sx
from strategy_core import StrategyConfig

STRATEGY = Path(__file__).resolve().parents[1] / "strategy.json"
DAY = datetime(2026, 11, 25, tzinfo=KST)
GUARD = sx.GuardConfig.from_dict({"max_age_s": 5, "book_max_age_s": 2, "depth_levels": 2, "half_life_s": 2.78, "gap_s": 5,
                                  "spread_first_ticks": 6, "spread_first_wait_s": 1, "spread_next_ticks": 4, "spread_next_wait_s": 5,
                                  "slow_slip_ticks": 3, "slow_gap_s": 10, "halt_slip_ticks": 5, "window_s": 30, "max_rejects": 2,
                                  "exit_gap_s": 1})


def cfg(qty=20):
    data = json.loads(STRATEGY.read_text(encoding="utf-8"))
    data["strategy"]["order_qty"] = qty
    data["risk"]["max_position"] = qty
    data["risk"]["daily_loss_limit"] = "100000000"
    data["params"]["exit_mode"] = "auction"
    return StrategyConfig.from_dict(data)


class Clock:
    def __init__(self, hms="15:00:00"):
        h, m, s = map(int, hms.split(":"))
        self.now = DAY.replace(hour=h, minute=m, second=s)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class Feed:
    def __init__(self, clock, price_at, spread="0.2", age_at=None):
        self.clock, self.price_at, self.spread, self.age_at = clock, price_at, D(spread), age_at or (lambda now: 0.0)

    def snapshot(self):
        now = self.clock()
        p = D(str(self.price_at(now)))
        half = self.spread / 2
        return Snapshot(ts=now, last=p, last_ts=now - timedelta(seconds=self.age_at(now)), bid=p - half, ask=p + half)

    def rest_last(self):
        return None

    def prev_close(self):
        return D("1000.0")


class Broker:
    source = "sim"

    def __init__(self, clock, feed, script=None):
        self.clock, self.feed, self.script, self.pos, self.orders = clock, feed, list(script or []), 0, []

    def net_position(self):
        return self.pos

    def cancel_open(self):
        self.orders.append(("cancel_open",))

    def market_order(self, side, qty, purpose, *, wait_until=None):
        self.orders.append((side, qty, purpose, self.clock().strftime("%H:%M:%S")))
        self.clock.sleep(0.3)                                   # 주문·체결 확인에 걸리는 시간
        if self.script:
            res = self.script.pop(0)
            if res is not None:
                if res.filled_qty:
                    self.pos += res.filled_qty if side == "buy" else -res.filled_qty
                return res
        price = self.feed.snapshot().last
        self.pos += qty if side == "buy" else -qty
        return ExecResult("filled", qty, price, order_no="T", source=self.source)


def planner(qty=20, weights=(8, 6, 6), offsets=(0, 1, 3), stop_pt="10", exit_w=(1, 1), exit_off=(0, 1)):
    def f(values, side):
        return SplitPlan(qty, sx.scale_schedule(qty, weights, offsets) if qty else [],
                         [sx.Child(o, w) for o, w in zip(exit_off, exit_w)], D(stop_pt), {"cap": qty})
    return f


def book_fn(clock, feed, l1=6, l2=9):
    def f():
        snap = feed.snapshot()
        t = D("0.1")
        return {"ask": [(snap.ask, l1), (snap.ask + t, l2), (snap.ask + 2 * t, 20)],
                "bid": [(snap.bid, l1), (snap.bid - t, l2), (snap.bid - 2 * t, 20)], "age_s": 0.1}
    return f


def make(tmp_path, price_at, plan=None, script=None, book=True, **feed_kw):
    clock = Clock()
    config = cfg()
    feed = Feed(clock, price_at, **feed_kw)
    broker = Broker(clock, feed, script)
    journal = Journal(tmp_path / "j.jsonl", "w06", mode="paper-sim", sym=config.sym, clock=clock)
    risk = RiskManager(RiskLimits.from_dict(config.risk), KillSwitch())
    t = SplitTrader(config, feed, broker, journal, risk, clock=clock, sleep=clock.sleep, planner=plan or planner(), guard=GUARD,
                    book=book_fn(clock, feed) if book is True else (book or None))
    return t, broker, journal


def events(tmp_path, name):
    return [r for r in read_journal(tmp_path / "j.jsonl") if r.get("name") == name]


def entries(broker):
    return [o for o in broker.orders if len(o) > 2 and o[2].startswith("entry")]


def test_scale_schedule_sums_and_front_loads():
    s = sx.scale_schedule(20, (8, 6, 6), (0, 1, 3))
    assert [c.qty for c in s] == [8, 6, 6]
    s = sx.scale_schedule(7, (1, 1, 1), (0, 1, 2))
    assert [c.qty for c in s] == [3, 2, 2] and sum(c.qty for c in s) == 7
    assert [c.qty for c in sx.scale_schedule(2, (1, 1, 1), (0, 1, 2))] == [1, 1]


def test_walk_book_and_slip():
    lv = [(D("1100.0"), 5), (D("1100.1"), 7), (D("1100.2"), 20)]
    avg, got = sx.walk_book(lv, 20)
    assert got == 20 and avg == (D("1100.0") * 5 + D("1100.1") * 7 + D("1100.2") * 8) / 20
    assert sx.slip_ticks("buy", D("1100.3"), D("1100.0"), D("1100.2"), D("0.1")) == D("2")
    assert sx.slip_ticks("sell", D("1099.9"), D("1100.0"), D("1100.2"), D("0.1")) == D("2")


def test_depth_children_then_single_auction_exit(tmp_path):
    t, broker, _ = make(tmp_path, lambda now: 1010.0)
    out = t.run()
    assert out["outcome"] == "traded" and out["qty"] == 20
    e = entries(broker)
    assert [o[1] for o in e] == [15, 5]                      # 1·2호가 6+9=15, 5초 뒤 남은 5
    assert [o[3] for o in e] == ["15:19:00", "15:19:05"]
    exits = [o for o in broker.orders if len(o) > 2 and o[2] == "exit"]
    assert len(exits) == 1 and exits[0][1] == 20
    assert broker.pos == 0


def test_no_book_uses_ladder(tmp_path):
    t, broker, _ = make(tmp_path, lambda now: 1010.0, book=False)
    out = t.run()
    assert out["qty"] == 20 and [o[1] for o in entries(broker)] == [8, 6, 6]


def test_stop_exits_in_depth_children(tmp_path):
    price = lambda now: 1010.0 if now.time() < datetime(2026, 1, 1, 15, 25).time() else 995.0
    t, broker, _ = make(tmp_path, price)
    out = t.run()
    assert out["outcome"] == "traded" and out["exit_reason"] == "stop"
    stops = [o for o in broker.orders if len(o) > 2 and o[2].startswith("stop_")]
    assert len(stops) >= 2 and stops[0][1] == 15 and sum(o[1] for o in stops) == 20
    assert broker.pos == 0


def test_slippage_slows_then_halts(tmp_path):
    slow = ExecResult("filled", 15, D("1010.4"), order_no="S", source="sim")    # 중간가 대비 +4틱 → 늦춤
    bad = ExecResult("filled", 4, D("1010.6"), order_no="X", source="sim")     # 1호가 6 − 덜 돌아온 1.2 = 4계약, +6틱 → 멈춤
    t, broker, _ = make(tmp_path, lambda now: 1010.0, script=[slow, bad])
    out = t.run()
    e = entries(broker)
    assert e[0][1] == 15 and e[1][3] == "15:19:10"             # 10초 간격
    assert e[1][1] == 4 and len(e) == 2 and out["qty"] == 19
    assert any(r.get("name") == "child_slow" for r in read_journal(tmp_path / "j.jsonl"))
    assert any(r.get("category") == "child_slippage" for r in read_journal(tmp_path / "j.jsonl"))
    assert broker.pos == 0


def test_rejects_stop_entry(tmp_path):
    rej = ExecResult("rejected", 0, None, code="E1", source="sim")
    t, broker, _ = make(tmp_path, lambda now: 1010.0, script=[rej, rej])
    out = t.run()
    assert out["outcome"] == "entry_not_filled" and out["reason"] == "rejects"
    assert len(entries(broker)) == 2 and broker.pos == 0


def test_stale_feed_blocks_children(tmp_path):
    age = lambda now: 0.0 if now.time() < datetime(2026, 1, 1, 15, 19, 1).time() else 9.0
    t, broker, _ = make(tmp_path, lambda now: 1010.0, age_at=age)
    out = t.run()
    assert out["qty"] == 15 and [o[1] for o in entries(broker)] == [15]
    assert events(tmp_path, "child_halt")[0]["text"].startswith("시세가 끊겨")


def test_first_child_wide_spread_waits_then_sends(tmp_path):
    t, broker, _ = make(tmp_path, lambda now: 1010.0, spread="0.7")     # 7틱 ≥ 6
    out = t.run()
    e = entries(broker)
    assert e[0][3] == "15:19:01"                                         # 1초 보류 뒤 그냥 보냄
    assert len(e) == 1 and out["qty"] == 15                              # 다음 조각은 4틱 이상이라 5초 보류 뒤 접음
    assert events(tmp_path, "child_halt")[0]["text"].startswith("스프레드")


def test_size_zero_skips(tmp_path):
    t, broker, _ = make(tmp_path, lambda now: 1010.0, plan=planner(qty=0))
    out = t.run()
    assert out["outcome"] == "skip" and out["reason"] == "w06_size_zero" and broker.orders == []


def test_partial_child_counts_by_position(tmp_path):
    part = ExecResult("partial", 5, D("1010.0"), order_no="P", source="sim")
    t, broker, _ = make(tmp_path, lambda now: 1010.0, script=[part])
    out = t.run()
    assert out["qty"] == 20 and broker.pos == 0
    assert [o[1] for o in entries(broker)][0] == 15


def test_record_pnl_uses_1545_close_for_scheduled_exit():
    import w06_record as wr

    class T:
        side, entry_price, qty_entered = "sell", D("1085.33"), 20
        result = {"exit_reason": "scheduled", "exit_avg": "1081.60"}

    ppc, px = wr.pnl_per_contract(T(), D("1082.50"))
    assert px == D("1082.50")
    assert ppc == D("2.83") - D("0.000098") * (D("1085.33") + D("1082.50"))
    T.result = {"exit_reason": "stop", "exit_avg": "1095.40"}
    ppc, px = wr.pnl_per_contract(T(), D("1082.50"))
    assert px == D("1095.40") and ppc < D("-10")
