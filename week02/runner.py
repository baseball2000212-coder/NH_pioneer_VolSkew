"""하루 한 번의 장 막판 매매를 진행하는 상태머신 (Trader).

시장·주문·시계를 직접 모르고 `Feed`(시세)·`Broker`(주문)·`clock`·`sleep` 만 받는다 →
같은 흐름을 [자체 시뮬레이션]·[모의계좌 주문]·테스트에서 그대로 쓴다.

흐름 (모든 시각·값은 strategy.json):
    시작 대기 → 전일 종가 확인 → 15:19:50 판단(최근가·등락률) → 15:20:00 시장가 진입
    → 보유 중 손절 감시(15:34:55 까지, 손절폭 stop_pt) → 15:35:30 종가 단일가 청산 주문
    → 거부되면 15:44:00 재시도 → 종료 시 미체결 취소·잔고 확인·기록 마감

안전 원칙 (CLAUDE.md §4-2):
    - `unknown`(응답 없음) 주문은 다시 내지 않는다. 사람이 주문내역·잔고로 확인한다.
    - 거부(rejected)만 한정 횟수 다시 낸다.
    - 킬 스위치(STOP 파일/Ctrl+C) → 보유가 있으면 청산 → 미체결 취소 → 기록 마감.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Any, Callable, Protocol

from nhapi import Journal, RiskManager, shutdown_sequence
from nhapi.timeutil import now_kst

import strategy_core as core
from strategy_core import StrategyConfig

MAX_EXIT_ATTEMPTS = 3


@dataclass(frozen=True)
class Snapshot:
    """시세 한 컷. `last_ts` 는 마지막 체결을 받은 시각."""

    ts: datetime
    last: Decimal | None
    last_ts: datetime | None
    bid: Decimal | None
    ask: Decimal | None


@dataclass
class ExecResult:
    """주문 한 번의 결과. 체결로 확인된 수량·가격만 `filled_qty`·`price` 에 넣는다."""

    status: str  # filled / partial / accepted(접수만, 체결 미확인) / rejected / unknown
    filled_qty: int
    price: Decimal | None
    order_no: str = ""
    code: str = ""
    message: str = ""
    source: str = ""  # sim / paper-account


class Feed(Protocol):
    def snapshot(self) -> Snapshot: ...
    def rest_last(self) -> Decimal | None: ...
    def prev_close(self) -> Decimal | None: ...


class Broker(Protocol):
    source: str

    def net_position(self) -> int: ...
    def market_order(self, side: str, qty: int, purpose: str, *, wait_until: datetime | None = None) -> ExecResult: ...
    def cancel_open(self) -> None: ...


class Trader:
    def __init__(
        self,
        cfg: StrategyConfig,
        feed: Feed,
        broker: Broker,
        journal: Journal,
        risk: RiskManager,
        *,
        clock: Callable[[], datetime] = now_kst,
        sleep: Callable[[float], None] = _time.sleep,
        realized_pnl: Decimal = Decimal(0),
    ) -> None:
        self.cfg, self.feed, self.broker, self.j, self.risk = cfg, feed, broker, journal, risk
        self.clock, self.sleep = clock, sleep
        self.realized_pnl = realized_pnl
        self.side: str | None = None
        self.qty_held = 0
        self.entry_price: Decimal | None = None
        self._orders_sent = False
        self._unresolved = False
        self._exited = False
        self.result: dict[str, Any] = {"outcome": "not_run"}

    # ------------------------------------------------------------------ 시간 도우미

    def _at(self, t: time) -> datetime:
        return self.clock().replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)

    def _wait_until(self, t: time) -> bool:
        """그 시각까지 기다린다. 킬 스위치가 켜지면 False."""
        target = self._at(t)
        while self.clock() < target:
            if self.risk.kill.triggered():
                return False
            self.sleep(self.cfg.poll_interval_s)
        return not self.risk.kill.triggered()

    def _done(self, outcome: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        self.result = {"outcome": outcome, "reason": reason, **extra}
        return self.result

    # ------------------------------------------------------------------ 실행

    def run(self) -> dict[str, Any]:
        try:
            self._run()
        except KeyboardInterrupt:
            self.risk.kill.trigger("Ctrl+C")
            self.j.event("interrupted", "Ctrl+C 로 중단")
            self.result = {"outcome": "aborted", "reason": "kill"} if self.result["outcome"] == "not_run" else self.result
        except Exception as exc:  # noqa: BLE001 — 어떤 오류든 정리 단계는 반드시 돈다.
            self.j.error("runner", msg=f"{type(exc).__name__}: {exc}", action="shutdown")
            self.result = {"outcome": "error", "reason": f"{type(exc).__name__}"}
        finally:
            self._finish()
        return self.result

    def _run(self) -> dict[str, Any]:
        c = self.cfg
        self.j.run(
            "start", strategy=c.name, version=c.version, sym=c.sym, qty=c.qty, threshold_pct=c.threshold_pct,
            stop_pt=c.stop_pt, exit_mode=c.exit_mode, source=self.broker.source,
        )
        if not self._wait_until(c.start_not_before):
            return self._done("aborted", "kill")
        if self.clock() > self._at(c.end_time):
            return self._done("skip", "after_session")

        position = self.broker.net_position()
        prev_close = self.feed.prev_close()
        self.j.event("pre_check", "시작 점검", position=position, prev_close=prev_close)

        if not self._wait_until(c.decision_time):
            return self._done("aborted", "kill")
        if self.clock() > self._at(c.entry_time) + _seconds(c.entry_grace_s):
            self.j.signal("none", "late_start", now=self.clock())
            return self._done("skip", "late_start")

        snap = self.feed.snapshot()
        price, price_source = self._decision_price(snap)
        values: dict[str, Any] = {
            "prev_close": prev_close, "price": price, "price_source": price_source,
            "threshold_pct": c.threshold_pct, "bid": snap.bid, "ask": snap.ask, "last_tick_ts": snap.last_ts,
        }
        if prev_close is None:
            self.j.signal("none", "no_prev_close", **values)
            return self._done("skip", "no_prev_close")
        if price is None:
            self.j.signal("none", "no_price", **values)
            return self._done("skip", "no_price")
        ret = core.day_return_pct(prev_close, price)
        values["ret_pct"] = ret
        decision = core.decide(ret, c.threshold_pct)
        if decision.side is None:
            self.j.signal("none", decision.reason, **values)
            return self._done("skip", decision.reason, ret_pct=str(ret))
        side = decision.side
        if position != 0:
            self.j.signal("none", "leftover_position", position=position, **values)
            self.j.error("position", msg=f"시작 시 보유 {position} 이(가) 있어 진입하지 않습니다. 잔고를 확인하세요.", action="skip")
            return self._done("skip", "leftover_position")
        verdict = self.risk.can_enter(side, c.qty, position=position, realized_pnl=self.realized_pnl, one_stop_loss=c.one_stop_loss_won)
        if not verdict.allowed:
            self.j.signal("none", "risk_blocked", detail=verdict.reason, **values)
            return self._done("skip", "risk_blocked", detail=verdict.reason)
        self.j.signal(side, "enter", **values)

        if not self._wait_until(c.entry_time):
            return self._done("aborted", "kill")
        return self._trade(side, values)

    def _decision_price(self, snap: Snapshot) -> tuple[Decimal | None, str]:
        """최근 체결가(WS)가 신선하면 그것, 아니면 REST 현재가."""
        fresh = snap.last is not None and snap.last_ts is not None and (snap.ts - snap.last_ts).total_seconds() <= self.cfg.price_max_age_s
        if fresh:
            return snap.last, "ws"
        rest = self.feed.rest_last()
        if rest is not None:
            return rest, "rest"
        return None, "none"

    # ------------------------------------------------------------------ 진입·보유·청산

    def _trade(self, side: str, values: dict[str, Any]) -> dict[str, Any]:
        c = self.cfg
        ref = self.feed.snapshot()
        res = self.broker.market_order(side, c.qty, "entry")
        self._orders_sent = True
        self._log_exec("entry", side, c.qty, res, ref_price=ref.last, bid=ref.bid, ask=ref.ask)
        if res.status in ("unknown", "accepted") and res.filled_qty == 0:
            self._unresolved = True
            self.j.error("order_unknown", msg="진입 주문 결과를 확인하지 못했습니다. 수동 확인 필요: 주문내역·잔고를 조회하세요. 다시 내지 않습니다.", action="stop")
            return self._done("entry_unknown", res.code)
        if res.filled_qty <= 0 or res.price is None:
            self.j.error("order_rejected", msg=f"진입 주문 실패: {res.code} {res.message}", action="no_position")
            return self._done("entry_failed", res.code)

        self.side, self.qty_held, self.entry_price = side, res.filled_qty, res.price
        stop = core.stop_price(side, res.price, c.stop_pt)
        self.j.event("position_open", "진입 완료", side=side, qty=res.filled_qty, entry_price=res.price, stop_price=stop, source=res.source)

        reason = self._hold(side, stop)
        exit_res = self._exit(reason)
        return self._finalize_trade(reason, exit_res, values)

    def _hold(self, side: str, stop: Decimal) -> str:
        c = self.cfg
        stop_until = self._at(c.stop_until)
        exit_at = self._at(c.exit_submit_time)
        while True:
            now = self.clock()
            if self.risk.kill.triggered():
                return "kill"
            snap = self.feed.snapshot()
            if now < stop_until and snap.last is not None and core.stop_hit(side, snap.last, stop):
                self.j.event("stop_hit", "손절선 도달", price=snap.last, stop_price=stop)
                return "stop"
            if now >= exit_at:
                return "exit"
            self.sleep(c.poll_interval_s)

    def _exit(self, purpose: str) -> ExecResult | None:
        c = self.cfg
        side = core.opposite(self.side or "")
        remaining, fills = self.qty_held, []
        attempts = 0
        continuous = c.exit_mode == "continuous"
        while remaining > 0 and attempts < MAX_EXIT_ATTEMPTS and self.clock() <= self._at(c.end_time):
            if continuous and self.clock() >= self._at(c.stop_until):
                # 15:35 이후 주문은 모의 서버에서 '미접수'로 남고 취소도 안 된다(2026-10-28 프로브). 내지 않는다.
                self.j.error("order_window", msg="연속매매 마감(15:35) 직전이라 청산 주문을 내지 않습니다. 다음 영업일 09:00 이후 수동 청산이 필요합니다.", action="manual_check")
                self._unresolved = True
                break
            attempts += 1
            wait_until = self._at(c.auction_fill_time) if (purpose == "exit" and c.exit_mode == "auction") else None
            res = self.broker.market_order(side, remaining, purpose, wait_until=wait_until)
            self._orders_sent = True
            self._log_exec(purpose, side, remaining, res)
            if res.filled_qty > 0 and res.price is not None:
                fills.append((res.filled_qty, res.price))
                remaining -= res.filled_qty
            if remaining <= 0:
                break
            if res.status in ("unknown", "accepted"):
                self._unresolved = True
                self.j.error("order_unknown", msg="청산 주문 결과를 확인하지 못했습니다. 수동 확인 필요: 주문내역·잔고를 조회하세요. 다시 내지 않습니다.", action="stop")
                break
            self.j.error("order_rejected", msg=f"청산 주문 실패({attempts}/{MAX_EXIT_ATTEMPTS}): {res.code} {res.message}", action="retry")
            if purpose == "exit" and not continuous and self.clock() < self._at(c.exit_retry_time):
                self._wait_until(c.exit_retry_time)
            else:
                self.sleep(1.0)
        self._exited = remaining <= 0
        if not fills:
            return None
        qty = sum(q for q, _ in fills)
        avg = sum(q * p for q, p in fills) / qty
        return ExecResult("filled" if remaining <= 0 else "partial", qty, avg, source=self.broker.source)

    def _finalize_trade(self, purpose: str, exit_res: ExecResult | None, values: dict[str, Any]) -> dict[str, Any]:
        c = self.cfg
        reason = "scheduled" if purpose == "exit" else purpose
        if exit_res is None or not self._exited:
            return self._done("exit_failed", reason, side=self.side)
        gross = core.pnl_pt(self.side or "", self.entry_price or Decimal(0), exit_res.price or Decimal(0))
        net = gross - c.cost_pt
        self.j.trade(
            side=self.side, qty=self.qty_held, entry_price=self.entry_price, exit_price=exit_res.price,
            exit_reason=reason, gross_pt=gross, assumed_cost_pt=c.cost_pt, net_pt=net,
            net_won=core.pnl_won(net, self.qty_held, c.multiplier), ret_pct=values.get("ret_pct"), source=exit_res.source,
            note="net 은 설정의 가정 비용(cost_pt_roundtrip)을 뺀 값. 실제 수수료는 예수금 조회로 따로 확인",
        )
        return self._done("traded", "", exit_reason=reason, gross_pt=str(gross), net_pt=str(net))

    # ------------------------------------------------------------------ 기록·종료

    def _log_exec(self, purpose: str, side: str, qty: int, res: ExecResult, **extra: Any) -> None:
        self.j.order(
            "result", side=side, qty=qty, purpose=purpose, status=res.status, order_no=res.order_no,
            reject_code=res.code, msg=res.message, **extra,
        )
        if res.filled_qty > 0:
            self.j.fill(res.source or self.broker.source, side=side, qty=res.filled_qty, price=res.price, purpose=purpose, order_no=res.order_no)

    def _finish(self) -> None:
        def cancel_all() -> None:
            if self._orders_sent:
                self.broker.cancel_open()

        def flatten() -> None:
            if self._unresolved:
                self.j.error("shutdown", msg="결과를 모르는 주문이 있어 자동 청산을 하지 않습니다. 잔고를 직접 확인하세요.", action="manual_check")
                return
            pos = self.broker.net_position()
            if pos != 0 and self.entry_price is not None and not self._exited:   # 우리가 연 포지션만 정리한다
                self.side, self.qty_held = ("buy" if pos > 0 else "sell"), abs(pos)
                self._exit("shutdown")
            final = self.broker.net_position()
            self.j.event("final_position", "종료 시 보유", position=final)
            if final != 0:
                self.j.error("position", msg=f"종료 시 보유 {final} 이(가) 남았습니다. 수동 정리가 필요합니다.", action="manual_check")

        shutdown_sequence(cancel_all=cancel_all, flatten=flatten, close_journal=lambda: self.j.run("end", **self.result), journal=self.j)


def _seconds(value: float):
    from datetime import timedelta
    return timedelta(seconds=value)
