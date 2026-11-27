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
            actual = None
            if isinstance(res, tuple):          # (보고된 결과, 실제로 체결된 수량): 응답은 모름인데 거래소에선 체결된 경우
                res, actual = res
            if res.status in ("filled", "partial"):
                self.pos += res.filled_qty if side == "buy" else -res.filled_qty
            elif actual:
                self.pos += actual if side == "buy" else -actual
            return res
        price = self.feed.snapshot().last or self.feed.rest_last() or D("1000.0")   # 시세가 끊긴 시험에서도 체결가는 있게
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


def test_rechecks_position_at_decision_when_1430_trade_closed_after_15(tmp_path):
    """2026-11-12: 14:30 큰 날 매매가 15:00:00 에 청산 → 15:00 에 읽은 잔고(1)가 판단 때는 0 이면 정상 진입."""
    holder = {}

    def price(now):
        if now.time() >= datetime(2026, 1, 1, 15, 0, 5).time() and holder.get("b") is not None and holder.get("flat") is None:
            holder["b"].pos, holder["flat"] = 0, True     # 15:00:05 에 14:30 매매 청산 완료
        return 1010.0
    trader, broker, *_ = make(tmp_path, price, position=1)
    holder["b"] = broker
    result = trader.run()
    assert result["outcome"] == "traded"
    assert any(r.get("name") == "position_recheck" and r.get("position") == 0 for r in lines(tmp_path, "event"))


def test_still_skips_when_position_remains_at_decision(tmp_path):
    trader, broker, *_ = make(tmp_path, flat(1010.0), position=1)
    result = trader.run()
    assert result["outcome"] == "skip" and result["reason"] == "leftover_position"


def test_skips_when_move_is_too_large(tmp_path):
    trader, broker, *_ = make(tmp_path, flat(1050.0))          # +5%
    result = trader.run()
    assert result["outcome"] == "skip" and result["reason"] == "skip_threshold"
    assert broker.orders == []   # 주문을 낸 적이 없으니 취소 단계도 건너뛴다
    sig = lines(tmp_path, "signal")[0]
    assert sig["side"] == "none" and sig["reason"] == "skip_threshold" and sig["ret_pct"].startswith("5")


def test_enter_buy_hold_and_exit_in_auction(tmp_path):
    entry_at = StrategyConfig.from_dict(json.loads(STRATEGY.read_text(encoding="utf-8"))).entry_time

    def price(now):                                             # 진입 시각(strategy.json, 2026-11-11 부터 15:19) 이후 1.5pt 오른 채로 끝
        return 1010.0 if now.time() < entry_at else 1011.5
    trader, broker, *_ = make(tmp_path, price)
    result = trader.run()
    assert result["outcome"] == "traded" and result["exit_reason"] == "scheduled"
    assert [o[:3] for o in broker.orders if o[0] != "cancel_open"] == [("buy", 1, "entry"), ("sell", 1, "exit")]
    entry_time = purposes(broker, "entry")[0][3]
    exit_time = purposes(broker, "exit")[0][3]
    assert entry_time >= entry_at.strftime("%H:%M:%S") and exit_time >= "15:35:30"
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


UNK = ExecResult("unknown", 0, None, code="NO_RESPONSE", message="응답 없음", source="sim")
OK_ENTRY = ExecResult("filled", 1, D("1010.1"), order_no="E1", source="sim")


def test_entry_unknown_and_not_filled_cancels_and_never_resends(tmp_path):
    # P0: 결과 모름 → 잔고 0 → 미체결 취소 → 다시 내지 않고 끝. 남은 보유 0
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[UNK])
    result = trader.run()
    assert result["outcome"] == "entry_not_filled"
    assert len(purposes(broker, "entry")) == 1 and not purposes(broker, "exit", "shutdown")
    assert ("cancel_open",) in broker.orders and broker.pos == 0
    assert result["final_position"] == 0


def test_entry_unknown_but_actually_filled_keeps_managing_the_position(tmp_path):
    # P0: 응답은 모름인데 실제론 체결 → 잔고로 확인하고 손절 감시·청산을 이어 간다 (예전엔 손절 없는 포지션이 밤을 넘김)
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[(UNK, 1)], exit_mode="continuous")
    result = trader.run()
    assert result["outcome"] == "traded" and result["estimated"] is True
    assert len(purposes(broker, "entry")) == 1 and len(purposes(broker, "exit")) == 1
    assert broker.pos == 0 and result["final_position"] == 0
    assert lines(tmp_path, "trade")[0]["entry_estimated"] is True


def test_entry_unknown_with_unexpected_position_alarms_and_sends_nothing(tmp_path):
    # 잔고가 반대 방향 → 아무것도 내지 않고 경보, 끝날 때 남은 보유를 경보
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[UNK])
    broker.net_position = lambda: -1 if broker.orders else 0     # 시작 땐 0, 매수 주문 뒤엔 엉뚱하게 −1
    alerts = []
    trader.alert = alerts.append
    result = trader.run()
    assert result["outcome"] == "entry_unknown"
    assert not purposes(broker, "exit", "shutdown")
    assert any("position_mismatch" in a for a in alerts) and any("종료 시 보유" in a for a in alerts)


def test_exit_rejected_is_retried_after_exit_retry_time(tmp_path):
    ok_entry = ExecResult("filled", 1, D("1010.1"), order_no="E1", source="sim")
    rej = ExecResult("rejected", 0, None, code="X", message="종가 단일가 거부", source="sim")
    ok_exit = ExecResult("filled", 1, D("1011.0"), order_no="X2", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[ok_entry, rej, ok_exit])
    result = trader.run()
    assert result["outcome"] == "traded"
    exits = purposes(broker, "exit")
    assert len(exits) == 2 and "15:40:00" <= exits[1][3] < "15:45:00"
    assert D(lines(tmp_path, "trade")[0]["gross_pt"]) == D("0.9")


def test_default_exit_is_closing_auction(tmp_path):
    # 기본 청산은 15:45 종가 동시호가 (15:35:30 에 시장가 제출)
    trader, broker, *_ = make(tmp_path, flat(1010.0), exit_mode=cfg().exit_mode)
    assert trader.cfg.exit_mode == "auction"
    trader.run()
    assert purposes(broker, "exit")[0][3] >= "15:35:30"


def test_auction_unknown_with_pending_order_waits_for_1545_and_never_resends(tmp_path):
    # 동시호가 청산 결과 모름 + 걸린 주문 있음 → 다시 내지 않고 15:45 체결을 기다린다 (두 번 팔려 반대 포지션 방지)
    trader, broker, j, clock = make(tmp_path, flat(1010.0), script=[OK_ENTRY, UNK])
    filled_at = DAY.replace(hour=15, minute=45)
    real_pos = broker.net_position
    broker.net_position = lambda: 0 if clock.now >= filled_at else real_pos()
    broker.open_order_count = lambda: 0 if clock.now >= filled_at else 1
    result = trader.run()
    assert len(purposes(broker, "exit")) == 1
    assert result["outcome"] == "traded" and result["final_position"] == 0
    assert "auction_wait" in (tmp_path / "j.jsonl").read_text(encoding="utf-8")


def test_kill_during_auction_wait_never_sends_a_second_sell(tmp_path):
    # 리뷰 #2: 동시호가 대기 중 킬 스위치 → 예전엔 매도 6건. 이제 1건만 내고 15:45 체결을 기다린다
    kill = KillSwitch()
    acc = ExecResult("accepted", 0, None, order_no="X1", message="접수됨, 체결 미확인", source="sim")

    def price(now):
        if now.time() >= datetime(2026, 1, 1, 15, 35, 10).time():
            kill.trigger("test")
        return 1010.0
    trader, broker, j, clock = make(tmp_path, price, script=[OK_ENTRY, acc], kill=kill)
    filled_at = DAY.replace(hour=15, minute=45)
    real_pos = broker.net_position
    broker.net_position = lambda: 0 if clock.now >= filled_at else real_pos()
    broker.open_order_count = lambda: 0 if clock.now >= filled_at else 1
    result = trader.run()
    sells = [o for o in broker.orders if len(o) > 2 and o[0] == "sell"]
    assert len(sells) == 1, sells
    assert result["final_position"] == 0


def test_finish_does_not_resell_while_auction_order_pending(tmp_path):
    # 리뷰 #3: 조회 예외 등으로 run 이 중간에 끝나도, 동시호가 주문이 걸려 있으면 종료 정리에서 새 매도를 내지 않는다
    trader, broker, j, clock = make(tmp_path, flat(1010.0))
    trader.side, trader.qty_held, trader.entry_price, trader._orders_sent = "buy", 1, D("1010"), True
    broker.pos = 1
    clock.now = DAY.replace(hour=15, minute=36)
    filled_at = DAY.replace(hour=15, minute=45)
    broker.net_position = lambda: 0 if clock.now >= filled_at else 1
    broker.open_order_count = lambda: 0 if clock.now >= filled_at else 1
    trader._finish()
    assert not [o for o in broker.orders if len(o) > 2]
    assert ("cancel_open",) not in broker.orders
    assert trader.result["final_position"] == 0


def test_auction_unknown_with_no_pending_order_resends_before_1545(tmp_path):
    # 결과 모름인데 걸린 주문이 없음 = 주문이 안 들어간 것 → 15:45 전에 다시 낸다
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[OK_ENTRY, UNK])
    broker.open_order_count = lambda: 0
    result = trader.run()
    exits = purposes(broker, "exit")
    assert result["outcome"] == "traded" and len(exits) == 2 and exits[1][3] < "15:45:00"


def test_no_orders_after_1545_in_auction_mode(tmp_path):
    rej = ExecResult("rejected", 0, None, code="X", message="거부", source="sim")
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[OK_ENTRY] + [rej] * 6)
    alerts = []
    trader.alert = alerts.append
    result = trader.run()
    assert result["outcome"] == "exit_failed"
    assert all(o[3] < "15:45:00" for o in purposes(broker, "exit", "shutdown"))
    assert any("종료 시 보유 1" in a for a in alerts)


def test_exit_unknown_but_actually_filled_is_not_resent(tmp_path):
    # P0: 청산 결과 모름 → 잔고 0 → 청산된 것. 다시 내면 반대 포지션이 생기므로 내지 않는다
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[OK_ENTRY, (UNK, 1)], exit_mode="continuous")
    result = trader.run()
    assert result["outcome"] == "traded" and result["estimated"] is True
    assert len(purposes(broker, "exit")) == 1 and broker.pos == 0


def test_exit_unknown_and_still_held_cancels_then_resends_before_cutoff(tmp_path):
    # P0: 청산 결과 모름 → 아직 보유 → 미체결 취소 → 남은 수량만 15:34:55 전에 다시 → 보유 0
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[OK_ENTRY, UNK], exit_mode="continuous")
    result = trader.run()
    exits = purposes(broker, "exit")
    assert result["outcome"] == "traded" and len(exits) == 2
    assert all(o[3] < "15:34:56" for o in exits)
    assert ("cancel_open",) in broker.orders and broker.pos == 0 and result["final_position"] == 0


def test_exit_unknown_every_time_stops_at_cutoff_and_alarms_leftover(tmp_path):
    # 끝까지 결과 모름 + 계속 보유 → 15:34:55 뒤로는 안 내고, 남은 보유를 경보
    trader, broker, *_ = make(tmp_path, flat(1010.0), script=[OK_ENTRY] + [UNK] * 8, exit_mode="continuous")
    alerts = []
    trader.alert = alerts.append
    result = trader.run()
    assert result["outcome"] == "exit_failed"
    assert all(o[3] < "15:34:56" for o in purposes(broker, "exit", "shutdown"))
    assert broker.pos == 1 and result["final_position"] == 1
    assert any("종료 시 보유 1" in a for a in alerts)


def test_stale_feed_during_hold_falls_back_to_rest_for_stop(tmp_path):
    # P0: WS 가 끊겨도 REST 현재가로 손절을 본다
    def price(now):
        return 1010.0 if now.time() < datetime(2026, 1, 1, 15, 22).time() else None
    trader, broker, *_ = make(tmp_path, price, rest=999.0, exit_mode="continuous")
    result = trader.run()
    assert result["exit_reason"] == "stop"
    assert any(r["category"] == "stale_feed" for r in lines(tmp_path, "error"))


def test_stale_feed_and_no_rest_exits_immediately(tmp_path):
    # P0: WS 도 REST 도 안 되면 5초 넘게 눈먼 채로 들고 있지 않고 바로 청산
    def price(now):
        return 1010.0 if now.time() < datetime(2026, 1, 1, 15, 22).time() else None
    trader, broker, *_ = make(tmp_path, price, rest=None, exit_mode="continuous")
    alerts = []
    trader.alert = alerts.append
    result = trader.run()
    assert result["outcome"] == "traded" and result["exit_reason"] == "stale"
    stop_order = purposes(broker, "stale")[0]
    assert "15:22:00" <= stop_order[3] < "15:22:07"
    assert broker.pos == 0 and any("stale_feed" in a for a in alerts)


def test_decision_uses_5s_freshness(tmp_path):
    # 판단가 허용 60초 → 5초. 10초 묵은 체결가는 쓰지 않고 REST 로 간다
    trader, broker, *_ = make(tmp_path, flat(1010.0), tick_age_s=10, rest=1005.0)
    trader.run()
    assert lines(tmp_path, "signal")[0]["price_source"] == "rest"


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
