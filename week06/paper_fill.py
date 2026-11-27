"""[자체 시뮬레이션(종이 체결)] 브로커 — 실제 주문을 내지 않고 시세로 체결을 흉내 낸다.

이 파일의 체결은 모의계좌 체결이 **아니다**. 기록에는 항상 `source="sim"` 으로 남고,
모의계좌 주문(`source="paper-account"`)과 같은 표에 섞지 않는다.

체결 모형 (가정 — 결과 해석 때 한계로 적는다):
    - 연속매매 시장가: 매수는 최우선 매도호가, 매도는 최우선 매수호가에 `slippage_ticks` 틱을 더 불리하게. 호가가 없으면 최근가.
    - 종가 단일가(`wait_until` 지정): 그 시각 이후 처음 받은 체결가(없으면 직전 체결가)에 슬리피지 없이 체결.
    - 한 번에 전량 체결, 부분체결·거부는 없다.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from nhapi.timeutil import now_kst

from runner import ExecResult, Feed

#: 종가 단일가 체결 틱을 기다리는 최대 시간(초)
AUCTION_TICK_WAIT_S = 30.0


class PaperSimBroker:
    source = "sim"

    def __init__(
        self,
        feed: Feed,
        *,
        tick: Decimal,
        slippage_ticks: Decimal,
        clock: Callable[[], datetime] = now_kst,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        self.feed, self.tick, self.slippage_ticks = feed, tick, slippage_ticks
        self.clock, self.sleep = clock, sleep
        self.pos = 0
        self.orders = 0

    def net_position(self) -> int:
        return self.pos

    def cancel_open(self) -> None:
        """시뮬레이터에는 미체결이 없다."""

    def market_order(self, side: str, qty: int, purpose: str, *, wait_until: datetime | None = None) -> ExecResult:
        self.orders += 1
        if wait_until is not None:
            price, note = self._auction_price(wait_until)
        else:
            snap = self.feed.snapshot()
            base = (snap.ask if side == "buy" else snap.bid) or snap.last
            note = "호가 기준" if (snap.ask if side == "buy" else snap.bid) else "최근가 기준(호가 없음)"
            if base is None:
                return ExecResult("rejected", 0, None, code="NO_PRICE", message="시세가 없어 체결할 수 없습니다.", source=self.source)
            slip = self.slippage_ticks * self.tick
            price = base + slip if side == "buy" else base - slip
        self.pos += qty if side == "buy" else -qty
        return ExecResult("filled", qty, price, order_no=f"SIM{self.orders:04d}", message=note, source=self.source)

    def _auction_price(self, at: datetime) -> tuple[Decimal | None, str]:
        while self.clock() < at:
            self.sleep(0.5)
        deadline = self.clock() + timedelta(seconds=AUCTION_TICK_WAIT_S)
        while True:
            snap = self.feed.snapshot()
            if snap.last is not None and snap.last_ts is not None and snap.last_ts >= at:
                return snap.last, "종가 단일가 이후 첫 체결가"
            if self.clock() >= deadline:
                return snap.last, "종가 단일가 체결 틱을 못 받아 직전 체결가 사용"
            self.sleep(0.5)
