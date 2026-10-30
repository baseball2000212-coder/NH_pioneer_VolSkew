"""Trader 상태머신 테스트 — 가짜 시계·시세·브로커로 API 없이 전 경로를 돈다."""
import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from nhapi import Journal, KillSwitch, RiskLimits, RiskManager, read_journal
from nhapi.timeutil import KST
from runner import ExecResult, Snapshot, Trader
from strategy_core import StrategyConfig

STRATEGY = Path(__file__).resolve().parents[1] / "strategy.json"
DAY = datetime(2026, 10, 28, tzinfo=KST)


def cfg(**over):
    data = json.loads(STRATEGY.read_text(encoding="utf-8"))
    for key, value in over.items():
        data["params"][key] = value
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
    """시각 → 가격 함수로 시세를 만든다."""

    def __init__(self, clock, price_at, prev_close="1000.0", rest=None, tick_age_s=0.0):
        self.clock, self.price_at, self._prev, self._rest, self.tick_age_s = clock, price_at, prev_close, rest, tick_age_s

    def snapshot(self):
        now = self.clock()
        price = self.price_at(now)
        if price is None:
            return Snapshot(ts=now, last=None, last_ts=None, bid=None, ask=None)
        p = D(str(price))
        return Snapshot(ts=now, last=p, last_ts=now - timedelta(seconds=self.tick_age_s), bid=p - D("0.1"), ask=p + D("0.1"))

    def rest_last(self):
        return None if self._rest is None else D(str(self._rest))

    def prev_close(self):
        return None if self._prev is None else D(str(self._prev))


class Broker:
    source = "sim"

    def __init__(self, clock, feed, script=None, position=0):
        self.clock, self.feed, self.script, self.pos = clock, feed, list(script or []), position
        self.orders = []

    def net_position(self):
        return self.pos

    def cancel_open(self):
        self.orders.append(("cancel_open",))

    def market_order(self, side, qty, purpose, *, wait_until=None):
        self.orders.append((side, qty, purpose, self.clock().strftime("%H:%M:%S")))
        if self.script:
            res = self.script.pop(0)
            if res.status in ("filled", "partial"):
                self.pos += res.filled_qty if side == "buy" else -res.filled_qty
            return res
        price = self.feed.snapshot().last
        self.pos += qty if side == "buy" else -qty
        return ExecResult("filled", qty, price, order_no="T1", source=self.source)


def make(tmp_path, price_at, *, prev_close="1000.0", script=None, position=0, start="15:00:00",
         realized=D(0), kill=None, rest=None, tick_age_s=0.0, **over):
    clock = Clock(start)
    over.setdefault("exit_mode", "auction")
    config = cfg(**over)
    feed = Feed(clock, price_at, prev_close, rest, tick_age_s)
    broker = Broker(clock, feed, script, position)
    journal = Journal(tmp_path / "j.jsonl", "r1", mode="paper-sim", sym=config.sym, clock=clock)
    kill = kill or KillSwitch()
    risk = RiskManager(RiskLimits.from_dict(config.risk), kill)
    trader = Trader(config, feed, broker, journal, risk, clock=clock, sleep=clock.sleep, realized_pnl=realized)
    return trader, broker, journal, clock


def lines(tmp_path, kind=None):
    rows = list(read_journal(tmp_path / "j.jsonl"))
    return [r for r in rows if kind is None or r["kind"] == kind]


def purposes(broker, *names):
    return [o for o in broker.orders if len(o) > 2 and o[2] in names]


def flat(price):
    return lambda now: price


def test_skips_when_move_is_too_large(tmp_path):
    trader, broker, *_ = make(tmp_path, flat(1050.0))          # +5%
    result = trader.run()
    assert result["outcome"] == "skip" and result["reason"] == "skip_threshold"
    assert broker.orders == []   # 주문을 낸 적이 없으니 취소 단계도 건너뛴다
    sig = lines(tmp_path, "signal")[0]
    assert sig["side"] == "none" and sig["reason"] == "skip_threshold" and sig["ret_pct"].startswith("5")


def test_enter_buy_hold_and_exit_in_auction(tmp_path):
    def price(now):                                             # 15:20 이후 1.5pt 오른 채로 끝
        return 1010.0 if now.time() < datetime(2026, 1, 1, 15, 20).time() else 1011.5
    trader, broker, *_ = make(tmp_path, price)
    result = trader.run()
    assert result["outcome"] == "traded" and result["exit_reason"] == "scheduled"
    assert [o[:3] for o in broker.orders if o[0] != "cancel_open"] == [("buy", 1, "entry"), ("sell", 1, "exit")]
    entry_time = purposes(broker, "entry")[0][3]
    exit_time = purposes(broker, "exit")[0][3]
    assert entry_time >= "15:20:00" and exit_time >= "15:35:30"
    trade = lines(tmp_path, "trade")[0]
    assert trade["side"] == "buy" and D(trade["gross_pt"]) == D("0")  # 같은 가격 체결(가짜 브로커) → 0
    assert trade["source"] == "sim"
    assert broker.pos == 0


def test_stop_loss_exits_immediately_and_only_once(tmp_path):
    def price(now):
        t = now.time()
        if t < datetime(2026, 1, 1, 15, 22).time():
            return 1010.0
        return 999.9                                            # 진입가 1010.x 에서 10pt 이상 하락
    trader, broker, *_ = make(tmp_path, price)
    result = trader.run()
    assert result["exit_reason"] == "stop"
    exits = purposes(broker, "stop", "exit")
    assert len(exits) == 1 and exits[0][2] == "stop"
    assert exits[0][3] < "15:23:00"


def test_stop_is_not_used_after_stop_until(tmp_path):
    def price(now):
        return 1010.0 if now.time() < datetime(2026, 1, 1, 15, 34).time() else 995.0
    trader, broker, *_ = make(tmp_path, price, stop_until="15:34:00")
    # 15:34 이전엔 평온, 이후 급락해도 연속매매 손절은 없고 종가 단일가 청산으로 간다
    result = trader.run()
    assert result["exit_reason"] == "scheduled"


def test_kill_switch_during_hold_exits(tmp_path):
    kill = KillSwitch()
    clock_holder = {}

    def price(now):
        if now.time() >= datetime(2026, 1, 1, 15, 27).time():
            kill.trigger("test")
        return 1010.0
    trader, broker, *_ = make(tmp_path, price, kill=kill)
    result = trader.run()
    assert result["exit_reason"] == "kill"
    assert broker.pos == 0 and ("cancel_open",) in broker.orders


def test_entry_rejected_means_no_exit(tmp_path):
    rej = ExecResult("rejected", 0, None, code="5039", message="거부", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[rej])
    result = trader.run()
    assert result["outcome"] == "entry_failed"
    assert len(purposes(broker, "entry")) == 1 and not purposes(broker, "exit", "shutdown")   # 진입 주문 한 번뿐
    assert lines(tmp_path, "error")


def test_entry_unknown_never_resends_and_asks_for_manual_check(tmp_path):
    unk = ExecResult("unknown", 0, None, code="NO_RESPONSE", message="응답 없음", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[unk])
    result = trader.run()
    assert result["outcome"] == "entry_unknown"
    assert len(purposes(broker, "entry")) == 1 and not purposes(broker, "shutdown")
    assert any("수동" in r.get("msg", "") for r in lines(tmp_path, "error"))


def test_exit_rejected_is_retried_after_exit_retry_time(tmp_path):
    ok_entry = ExecResult("filled", 1, D("1010.1"), order_no="E1", source="sim")
    rej = ExecResult("rejected", 0, None, code="X", message="종가 단일가 거부", source="sim")
    ok_exit = ExecResult("filled", 1, D("1011.0"), order_no="X2", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[ok_entry, rej, ok_exit])
    result = trader.run()
    assert result["outcome"] == "traded"
    exits = purposes(broker, "exit")
    assert len(exits) == 2 and exits[1][3] >= "15:44:00"
    assert D(lines(tmp_path, "trade")[0]["gross_pt"]) == D("0.9")


def test_exit_unknown_is_not_resent(tmp_path):
    ok_entry = ExecResult("filled", 1, D("1010.1"), order_no="E1", source="sim")
    unk = ExecResult("unknown", 0, None, code="NO_RESPONSE", message="응답 없음", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[ok_entry, unk])
    result = trader.run()
    assert result["outcome"] == "exit_failed"
    assert len(purposes(broker, "exit")) == 1


def test_daily_loss_limit_blocks_entry(tmp_path):
    trader, broker, *_ = make(tmp_path, flat(1010.0), realized=D("-250000"))   # 남은 5만 < 1회 손절 10만
    result = trader.run()
    assert result["outcome"] == "skip" and result["reason"] == "risk_blocked"
    assert broker.orders == []


def test_leftover_position_blocks_entry(tmp_path):
    trader, broker, *_ = make(tmp_path, flat(1010.0), position=1)
    result = trader.run()
    assert result["reason"] == "leftover_position" and broker.orders == []


def test_no_price_or_no_prev_close_skips(tmp_path):
    trader, broker, *_ = make(tmp_path, lambda now: None)
    assert trader.run()["reason"] == "no_price"
    trader, broker, *_ = make(tmp_path / "b", flat(1010.0), prev_close=None)
    assert trader.run()["reason"] == "no_prev_close"


def test_stale_tick_falls_back_to_rest_price(tmp_path):
    trader, broker, *_ = make(tmp_path, flat(1010.0), tick_age_s=300, rest=1005.0)
    trader.run()
    sig = lines(tmp_path, "signal")[0]
    assert sig["price_source"] == "rest" and sig["price"].startswith("1005")


def test_late_start_does_not_enter(tmp_path):
    trader, broker, *_ = make(tmp_path, flat(1010.0), start="15:25:00")
    result = trader.run()
    assert result["reason"] == "late_start" and broker.orders == []


def test_continuous_mode_exits_at_1534_30_and_retries_quickly(tmp_path):
    rej = ExecResult("rejected", 0, None, code="X", message="일시 거부", source="sim")
    ok_entry = ExecResult("filled", 1, D("1010.1"), order_no="E1", source="sim")
    ok_exit = ExecResult("filled", 1, D("1011.0"), order_no="X2", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[ok_entry, rej, ok_exit], exit_mode="continuous")
    result = trader.run()
    exits = purposes(broker, "exit")
    assert result["outcome"] == "traded" and len(exits) == 2
    assert exits[0][3] >= "15:34:30" and exits[1][3] < "15:34:55"        # 15:44 까지 기다리지 않는다


def test_continuous_mode_never_sends_orders_after_1534_55(tmp_path):
    rej = ExecResult("rejected", 0, None, code="X", message="거부", source="sim")
    ok_entry = ExecResult("filled", 1, D("1010.1"), order_no="E1", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[ok_entry, rej, rej, rej], exit_mode="continuous")
    result = trader.run()
    assert result["outcome"] == "exit_failed"
    assert all(o[3] < "15:34:56" for o in purposes(broker, "exit", "shutdown"))


def test_continuous_mode_refuses_to_send_after_cutoff(tmp_path):
    trader, broker, j, clock = make(tmp_path, flat(1010.0), exit_mode="continuous")
    trader.side, trader.qty_held = "buy", 1
    clock.now = clock.now.replace(hour=15, minute=34, second=56)
    trader._exit("kill")
    assert purposes(broker, "kill") == [] and any(r["category"] == "order_window" for r in lines(tmp_path, "error"))
