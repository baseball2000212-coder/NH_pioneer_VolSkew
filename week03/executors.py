"""실행기 — 엔진의 Intent 를 체결로 바꿉니다.

SimExecutor    : [백테스트]·[자체 시뮬레이션(종이 체결)]. 기준가에 불리한 방향으로 슬리피지(틱)를 더합니다. 이 체결은 모의계좌 체결이 아닙니다.
OrderExecutor  : [모의계좌 주문]. 해외 모의계좌에 시장가 주문을 내고, **주문내역으로 체결을 확인한 것만** 체결로 돌려줍니다.
                 접수만 되고 체결 확인이 안 되면 `Fill` 이 아니라 None 을 돌려줍니다(체결로 쓰지 않음).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from nhapi.overseas import OverseasGateway, OverseasOrderRequest
from strategy_core import Intent


@dataclass(frozen=True)
class Fill:
    price: float
    qty: int
    source: str            # "backtest-model" | "paper-sim" | "mock-account"
    order_no: str = ""
    note: str = ""


class SimExecutor:
    def __init__(self, source: str, tick: float, slippage_ticks: float = 0.0) -> None:
        self.source, self.slip = source, tick * slippage_ticks

    def execute(self, intent: Intent, qty: int) -> Fill | None:
        # 진입은 매수면 비싸게, 매도면 싸게. 청산은 반대 방향 주문이므로 포지션 방향의 반대로 불리하게.
        adverse = intent.side if intent.kind == "enter" else -intent.side
        return Fill(intent.ref_price + adverse * self.slip, qty, self.source)


class OrderExecutor:
    """시장가 주문 → 접수 확인 → 주문내역으로 체결 확인."""

    def __init__(self, gateway: OverseasGateway, sym: str, *, fill_wait_seconds: float = 20.0, poll_seconds: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.gw, self.sym = gateway, sym
        self.wait, self.poll, self._sleep, self._clock = fill_wait_seconds, poll_seconds, sleep, clock
        self.halted = False         # 응답·체결 확인이 불확실하면 새 주문을 막습니다
        self.last_status = ""

    def execute(self, intent: Intent, qty: int) -> Fill | None:
        if self.halted:
            self.last_status = "halted"
            return None
        trade_side = intent.side if intent.kind == "enter" else -intent.side
        outcome = self.gw.place(OverseasOrderRequest(self.sym, "buy" if trade_side > 0 else "sell", qty))
        if outcome.status == "unknown":
            outcome = self.gw.resolve_unknown(outcome)
        self.last_status = outcome.status
        if outcome.status == "rejected":
            return None
        if outcome.status == "unknown":
            self.halted = True
            return None
        deadline = self._clock() + self.wait
        while True:
            record = self.gw.fills_of(outcome.order_no)
            if record is not None and record.exec_qty and record.exec_qty >= Decimal(qty) and record.exec_price:
                return Fill(float(record.exec_price), qty, "mock-account", outcome.order_no)
            if self._clock() >= deadline:
                self.halted = True
                self.last_status = "accepted-unconfirmed"
                return None
            self._sleep(self.poll)


class EchoExecutor:
    """실행기를 감싸 화면에 한 줄씩 보여 줍니다(운영 중 확인·캡처용). 동작은 그대로 위임합니다."""

    def __init__(self, inner, say=print) -> None:
        self._inner, self._say = inner, say

    def execute(self, intent: Intent, qty: int) -> Fill | None:
        fill = self._inner.execute(intent, qty)
        direction = "매수" if (intent.side > 0) == (intent.kind == "enter") else "매도"
        what = "진입" if intent.kind == "enter" else "청산"
        result = f"체결 {fill.price} ({fill.source})" if fill else f"체결 확인 안 됨 ({getattr(self._inner, 'last_status', '')})"
        self._say(f"[{intent.ts:%H:%M}] 규칙 {intent.rule} {what} {direction} 1계약 — {intent.reason} → {result}")
        return fill

    def __getattr__(self, name):
        return getattr(self._inner, name)
