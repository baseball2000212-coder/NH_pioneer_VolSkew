"""연결 데우기(2026-11-27) 테스트 — 진입 직전·조각 사이·보유 중에 연결이 5초 넘게 쉬지 않고, 데우기가 주문을 늦추지 않는지."""
import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from nhapi import Journal, KillSwitch, RiskLimits, RiskManager, read_journal
from nhapi.timeutil import KST
from runner import ExecResult, Snapshot, Trader, WARM_QUIET_S
from runner_w06 import SplitPlan, SplitTrader
import split_exec as sx
from strategy_core import StrategyConfig

STRATEGY = Path(__file__).resolve().parents[1] / "strategy.json"
DAY = datetime(2026, 11, 27, tzinfo=KST)
GUARD = sx.GuardConfig.from_dict({"max_age_s": 5, "book_max_age_s": 2, "depth_levels": 2, "half_life_s": 2.78, "gap_s": 5,
                                  "spread_first_ticks": 6, "spread_first_wait_s": 1, "spread_next_ticks": 4, "spread_next_wait_s": 5,
                                  "slow_slip_ticks": 3, "slow_gap_s": 10, "halt_slip_ticks": 5, "window_s": 30, "max_rejects": 2,
                                  "exit_gap_s": 1})
KEEPALIVE_S = 5.0   # httpx 기본: 5초 넘게 안 쓴 연결을 닫는다


def at(hms):
    h, m, s = map(int, hms.split(":"))
    return DAY.replace(hour=h, minute=m, second=s)


def cfg(qty):
    data = json.loads(STRATEGY.read_text(encoding="utf-8"))
    data["strategy"]["order_qty"] = qty
    data["risk"]["max_position"] = qty
    data["risk"]["daily_loss_limit"] = "100000000"
    data["params"]["exit_mode"] = "auction"
    return StrategyConfig.from_dict(data)


class Clock:
    def __init__(self):
        self.now = at("15:00:00")

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class Feed:
    """REST 를 부를 때마다 기록한다(데우기·REST 대체 시세). 데우기는 0.06초 걸린다."""

    def __init__(self, clock, price=1010.0, fail=False, cost_s=0.06):
        self.clock, self.price, self.fail, self.cost_s = clock, D(str(price)), fail, cost_s
        self.warm_calls = []

    def snapshot(self):
        now = self.clock()
        return Snapshot(ts=now, last=self.price, last_ts=now, bid=self.price - D("0.1"), ask=self.price + D("0.1"))

    def warm_connection(self):
        self.warm_calls.append(self.clock())
        self.clock.sleep(self.cost_s)
        if self.fail:
            raise RuntimeError("warm failed")

    def rest_last(self):
        return None

    def prev_close(self):
        return D("1000.0")


class Broker:
    source = "sim"

    def __init__(self, clock, feed):
        self.clock, self.feed, self.pos, self.orders = clock, feed, 0, []

    def net_position(self):
        return self.pos

    def cancel_open(self):
        pass

    def open_order_count(self):
        return 0

    def market_order(self, side, qty, purpose, *, wait_until=None):
        self.orders.append((self.clock(), side, qty, purpose))
        self.clock.sleep(0.3)
        self.pos += qty if side == "buy" else -qty
        return ExecResult("filled", qty, self.feed.price, order_no="T", source=self.source)


def book_fn(feed, l1=6, l2=9):
    def f():
        p, t = feed.price, D("0.1")
        return {"ask": [(p + t, l1), (p + 2 * t, l2)], "bid": [(p - t, l1), (p - 2 * t, l2)], "age_s": 0.1}
    return f


def planner(values, side):
    return SplitPlan(20, sx.scale_schedule(20, (8, 6, 6), (0, 1, 3)), [sx.Child(0, 1)], D("10"), {"cap": 20})


def make(tmp_path, split=True, **feed_kw):
    clock = Clock()
    config = cfg(20 if split else 1)
    feed = Feed(clock, **feed_kw)
    broker = Broker(clock, feed)
    journal = Journal(tmp_path / "j.jsonl", "warm", mode="paper-sim", sym=config.sym, clock=clock)
    risk = RiskManager(RiskLimits.from_dict(config.risk), KillSwitch())
    if split:
        t = SplitTrader(config, feed, broker, journal, risk, clock=clock, sleep=clock.sleep, planner=planner, guard=GUARD,
                        book=book_fn(feed))
    else:
        t = Trader(config, feed, broker, journal, risk, clock=clock, sleep=clock.sleep)
    return t, feed, broker


def http_times(feed, broker):
    """주문·데우기 모두 같은 연결을 쓴다 — 요청이 끝난 시각 목록."""
    ends = [c + timedelta(seconds=feed.cost_s) for c in feed.warm_calls] + [o[0] + timedelta(seconds=0.3) for o in broker.orders]
    return sorted(ends)


def assert_warm_before(order_at, feed, broker):
    before = [c for c in feed.warm_calls if c < order_at]
    assert before, "진입 전 데우기가 없다"
    last = max(before)
    gap = (order_at - last).total_seconds()
    assert WARM_QUIET_S <= gap < KEEPALIVE_S - 1, gap            # 1초 안쪽엔 안 데우고, 연결은 아직 살아 있다
    prev = [e for e in http_times(feed, broker) if e <= order_at]
    assert (order_at - prev[-1]).total_seconds() < KEEPALIVE_S


def max_idle(feed, broker, start, end):
    ends = [e for e in http_times(feed, broker) if start <= e <= end]
    pts = [start] + ends + [end]
    return max((b - a).total_seconds() for a, b in zip(pts, pts[1:]))


def test_split_entry_on_time_with_warm_connection(tmp_path):
    t, feed, broker = make(tmp_path)
    out = t.run()
    assert out["outcome"] == "traded" and out["qty"] == 20
    entries = [o for o in broker.orders if o[3].startswith("entry")]
    assert [o[2] for o in entries] == [15, 5]
    assert entries[0][0] == at("15:19:00")                       # 데우기가 첫 주문을 늦추지 않는다
    assert entries[1][0] == at("15:19:00") + timedelta(seconds=5)
    assert_warm_before(entries[0][0], feed, broker)
    assert_warm_before(entries[1][0], feed, broker)               # 조각 사이 5초도 데운다
    assert min(feed.warm_calls) >= at("15:18:45")                 # 판단 10초 전부터만 데운다


def test_hold_keeps_connection_alive_until_exit(tmp_path):
    t, feed, broker = make(tmp_path)
    t.run()
    exit_order = [o for o in broker.orders if o[3] == "exit"][0]
    assert exit_order[0] == at("15:35:30")
    assert max_idle(feed, broker, at("15:19:05"), exit_order[0]) < KEEPALIVE_S
    assert not [c for c in feed.warm_calls if exit_order[0] - timedelta(seconds=WARM_QUIET_S) < c < exit_order[0]]
    assert not [c for c in feed.warm_calls if c > exit_order[0]]  # 청산 뒤엔 안 데운다
    gaps = [(b - a).total_seconds() for a, b in zip(feed.warm_calls, feed.warm_calls[1:])]
    assert min(gaps) >= 2.5 - 1e-9                                # 너무 자주 부르지 않는다
    assert len(feed.warm_calls) < 450                             # 약 17분에 몇백 번 수준


def test_single_contract_trader_also_warms(tmp_path):
    t, feed, broker = make(tmp_path, split=False)
    out = t.run()
    assert out["outcome"] == "traded"
    entry = [o for o in broker.orders if o[3] == "entry"][0]
    assert entry[0] == at("15:19:00")
    assert_warm_before(entry[0], feed, broker)


def test_warm_failure_does_not_stop_trading(tmp_path):
    t, feed, broker = make(tmp_path, fail=True)
    out = t.run()
    assert out["outcome"] == "traded" and broker.pos == 0
    assert [o for o in broker.orders if o[3].startswith("entry")][0][0] == at("15:19:00")
    errs = [r for r in read_journal(tmp_path / "j.jsonl") if r.get("kind") == "error" and r.get("category") == "warm_connection"]
    assert errs and len(errs) == len(feed.warm_calls)


def test_feed_without_warm_is_noop(tmp_path):
    t, feed, broker = make(tmp_path)
    feed.warm_connection = None          # 데우기 없는 피드(예전 가짜 피드) → 그냥 지나감
    out = t.run()
    assert out["outcome"] == "traded"
    assert [o for o in broker.orders if o[3].startswith("entry")][0][0] == at("15:19:00")
