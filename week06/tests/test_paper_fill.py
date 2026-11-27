from datetime import datetime, timedelta
from decimal import Decimal as D

from nhapi.timeutil import KST
from paper_fill import PaperSimBroker
from runner import Snapshot

T0 = datetime(2026, 10, 28, 15, 20, 0, tzinfo=KST)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)


class Feed:
    def __init__(self, clock, fn):
        self.clock, self.fn = clock, fn

    def snapshot(self):
        return self.fn(self.clock())


def broker(fn, now=T0):
    clock = Clock(now)
    return PaperSimBroker(Feed(clock, fn), tick=D("0.1"), slippage_ticks=D("1"), clock=clock, sleep=clock.sleep), clock


def snap(now, last="1000.0", bid="999.9", ask="1000.1", last_ts=None):
    return Snapshot(ts=now, last=D(last), last_ts=last_ts or now, bid=D(bid) if bid else None, ask=D(ask) if ask else None)


def test_market_buy_pays_ask_plus_slippage_sell_gets_bid_minus_slippage():
    b, _ = broker(lambda now: snap(now))
    buy = b.market_order("buy", 1, "entry")
    assert buy.price == D("1000.2") and buy.status == "filled" and buy.source == "sim" and b.net_position() == 1
    sell = b.market_order("sell", 1, "exit")
    assert sell.price == D("999.8") and b.net_position() == 0


def test_without_book_uses_last_price():
    b, _ = broker(lambda now: snap(now, bid=None, ask=None))
    assert b.market_order("buy", 1, "entry").price == D("1000.1")


def test_no_price_rejects():
    b, _ = broker(lambda now: Snapshot(now, None, None, None, None))
    res = b.market_order("buy", 1, "entry")
    assert res.status == "rejected" and b.net_position() == 0


def test_auction_waits_and_uses_first_tick_after_time_without_slippage():
    at = T0.replace(minute=45)

    def fn(now):
        if now < at:
            return snap(now, last="1001.0")
        return snap(now, last="1003.0", last_ts=now)
    b, clock = broker(fn, now=T0.replace(minute=35, second=30))
    res = b.market_order("sell", 1, "exit", wait_until=at)
    assert clock() >= at and res.price == D("1003.0") and "첫 체결가" in res.message


def test_auction_falls_back_to_last_price_when_no_tick_arrives():
    at = T0.replace(minute=45)
    b, clock = broker(lambda now: snap(now, last="1001.0", last_ts=at - timedelta(minutes=5)), now=at)
    res = b.market_order("sell", 1, "exit", wait_until=at)
    assert res.price == D("1001.0") and "직전 체결가" in res.message
