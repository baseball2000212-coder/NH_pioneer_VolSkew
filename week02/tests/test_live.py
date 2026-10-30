"""live.py: 주문내역 체결 확인·WS 틱 반영을 가짜 게이트웨이로 검증 (네트워크 없음)."""
import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from nhapi import Journal, read_journal
from nhapi.orders import OrderOutcome
from nhapi.parsers import OrderRecord, Position
from nhapi.timeutil import KST
from live import LiveFeed, NhOrderBroker, _executions
from strategy_core import StrategyConfig

CFG = StrategyConfig.from_dict(json.loads((Path(__file__).resolve().parents[1] / "strategy.json").read_text(encoding="utf-8")))
T0 = datetime(2026, 10, 28, 15, 20, 0, tzinfo=KST)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)


def rec(order_no="123", exec_qty="0", exec_price="0", exec_no="0", reject="", qty="1", remaining="0", sym="A066C000", side="buy"):
    return OrderRecord(
        order_no=order_no, org_order_no=order_no, sym=sym, side=side, status_code="", status="", status_name="접수",
        qty=D(qty), exec_qty=D(exec_qty), remaining_qty=D(remaining), cancelled_qty=D(0), price=D(0),
        exec_price=D(exec_price), trade_date="20261028", recv_time="152000000", exec_time="", exec_no=exec_no, reject_reason=reject,
    )


class Gateway:
    def __init__(self, outcome, histories, positions=(), opens=()):
        self.outcome, self.histories, self._pos, self._opens = outcome, list(histories), list(positions), list(opens)
        self.placed, self.cancelled, self.resolved = [], [], 0

    def place(self, request):
        self.placed.append(request)
        return self.outcome

    def resolve_unknown(self, outcome):
        self.resolved += 1
        return outcome

    def history(self):
        return self.histories.pop(0) if len(self.histories) > 1 else self.histories[0]

    def positions(self):
        return self._pos

    def open_orders(self):
        return self._opens

    def cancel(self, sym, side, qty, org_ord_no, **kw):
        self.cancelled.append((sym, side, qty, org_ord_no))
        return OrderOutcome("accepted", order_no="C1")


def broker(tmp_path, gateway):
    clock = Clock()
    j = Journal(tmp_path / "j.jsonl", "r", mode="paper-order", sym=CFG.sym, clock=clock)
    return NhOrderBroker(gateway, CFG, j, clock=clock, sleep=clock.sleep), clock


def test_executions_dedupes_same_exec_no_and_averages():
    qty, price = _executions([rec(exec_qty="1", exec_price="1000.0", exec_no="0000000557"),
                              rec(exec_qty="1", exec_price="1000.0", exec_no="0000000557")])
    assert qty == 1 and price == D("1000.0")
    qty, price = _executions([rec(exec_qty="1", exec_price="1000.0", exec_no="01"), rec(exec_qty="3", exec_price="1001.0", exec_no="02")])
    assert qty == 4 and price == D("1000.75")
    assert _executions([rec()]) == (0, None)


def test_filled_after_polling_history(tmp_path):
    gw = Gateway(OrderOutcome("accepted", order_no="123"), [[rec()], [rec(exec_qty="1", exec_price="1010.2", exec_no="9")]])
    b, _ = broker(tmp_path, gw)
    res = b.market_order("buy", 1, "entry")
    assert res.status == "filled" and res.price == D("1010.2") and res.source == "paper-account" and res.order_no == "123"
    assert gw.placed[0].ord_type == "market" and gw.placed[0].exec_cond == "FAS"


def test_accepted_but_never_filled_is_not_reported_as_filled(tmp_path):
    gw = Gateway(OrderOutcome("accepted", order_no="123"), [[rec()]])
    b, clock = broker(tmp_path, gw)
    res = b.market_order("buy", 1, "entry")
    assert res.status == "accepted" and res.filled_qty == 0           # 접수 ≠ 체결
    assert (clock() - T0).total_seconds() >= CFG.fill_poll_max_s


def test_rejected_by_server_returns_rejected_without_polling(tmp_path):
    gw = Gateway(OrderOutcome("rejected", code="5039", message="시장가 FOK 불가"), [[rec()]])
    b, _ = broker(tmp_path, gw)
    res = b.market_order("buy", 1, "entry")
    assert res.status == "rejected" and res.code == "5039" and gw.placed


def test_unknown_is_resolved_via_history_or_stays_unknown(tmp_path):
    gw = Gateway(OrderOutcome("unknown", code="NO_RESPONSE", message="응답 없음", sent_at=0), [[rec()]])
    b, _ = broker(tmp_path, gw)
    res = b.market_order("sell", 1, "exit")
    assert res.status == "unknown" and gw.resolved == 3 and len(gw.placed) == 1   # 다시 내지 않는다


def test_history_reject_reason_is_reported(tmp_path):
    gw = Gateway(OrderOutcome("accepted", order_no="123"), [[rec(reject="종가 단일가 시장가 불가")]])
    b, _ = broker(tmp_path, gw)
    res = b.market_order("sell", 1, "exit")
    assert res.status == "rejected" and "단일가" in res.message


def test_net_position_and_cancel_open(tmp_path):
    pos = [Position(sym="A066C000", name="", side="sell", qty=D(2), closable_qty=D(2), avg_price=D(1), pl_amt=D(0)),
           Position(sym="OTHER", name="", side="buy", qty=D(5), closable_qty=D(5), avg_price=D(1), pl_amt=D(0))]
    gw = Gateway(None, [[]], positions=pos, opens=[rec(order_no="77", remaining="1"), rec(order_no="78", sym="OTHER", remaining="1")])
    b, _ = broker(tmp_path, gw)
    assert b.net_position() == -2
    b.cancel_open()
    assert gw.cancelled == [("A066C000", "buy", 1, "77")]


def test_live_feed_updates_from_ka_tick_only(tmp_path):
    clock = Clock()
    j = Journal(tmp_path / "j.jsonl", "r", mode="paper-order", sym=CFG.sym, clock=clock)
    feed = LiveFeed(None, CFG, j, tick_path=tmp_path / "ticks.jsonl", clock=clock)
    feed.on_message("KA", CFG.sym, {"tick_div": "1", "exec_qty": "3", "last_pric": "1010.5", "bid_best_pric": "1010.4",
                                    "ask_best_pric": "1010.5", "exch_tm": "152001"})
    feed.on_message("KA", CFG.sym, {"tick_div": "2", "exec_qty": "0", "last_pric": "9999.0"})     # 체결 아님
    feed.on_message("KB", CFG.sym, {"last_pric": "8888.0"})                                       # 호가 메시지는 무시
    snap = feed.snapshot()
    assert (snap.last, snap.bid, snap.ask) == (D("1010.5"), D("1010.4"), D("1010.5")) and snap.last_ts == T0
    assert feed.tick_count == 1
    feed.stop()
    lines = (tmp_path / "ticks.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["price"] == "1010.5"
