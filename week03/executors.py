"""실행기 — 엔진의 Intent 를 체결로 바꿉니다.

SimExecutor    : [백테스트]·[자체 시뮬레이션(종이 체결)]. 기준가에 불리한 방향으로 슬리피지(틱)를 더합니다. 이 체결은 모의계좌 체결이 아닙니다.
OrderExecutor  : [모의계좌 주문]. 해외 모의계좌에 시장가 주문을 내고, **주문내역으로 체결을 확인한 것만** 체결로 돌려줍니다.
                 접수만 되고 체결 확인이 안 되거나 응답을 못 받으면(unknown) **잔고(positions)로 대사**합니다.
                 잔고가 '체결됨'과 맞으면 체결(가격 추정), '안 됨'과 맞으면 미체결 취소 후 None, 어느 쪽도 아니면(모호) 신규 진입을 멈춥니다.
                 청산 주문은 `halted` 여도 막지 않습니다. 거부된 청산은 간격을 늘려 가며(백오프) 다시 내고, 3번째 거부부터 경보를 보냅니다.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nhapi.overseas import OverseasGateway, OverseasOrderRequest
from strategy_core import Intent

#: 청산이 거부·미체결일 때 다음 시도까지 기다릴 초(차례로, 마지막 값에서 멈춤)
EXIT_BACKOFF_SECONDS = (10.0, 20.0, 40.0, 60.0)
#: 이 횟수만큼 청산이 연달아 실패하면 경보를 보내고 잔고로 대조합니다
MAX_EXIT_FAILURES = 3


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


# ---------------------------------------------------------------- 잔고·미체결 도우미 (live.py 의 종료 정리도 씁니다)


def net_position(held, sym: str) -> int | None:
    """잔고 목록 → 그 종목의 순포지션(매수 +). 방향을 알 수 없는 줄이 있으면 None(모호)."""
    net = 0
    for p in held:
        if p.sym != sym:
            continue
        if p.side not in ("buy", "sell"):
            return None
        net += int(p.qty) * (1 if p.side == "buy" else -1)
    return net


def avg_price(held, sym: str) -> float | None:
    for p in held:
        if p.sym == sym and p.avg_price:
            return float(p.avg_price)
    return None


def cancel_open_orders(gateway, sym: str | None, log: Callable[..., Any]) -> int:
    """미체결 주문을 취소합니다(`sym` 이 None 이면 전부). 줄 하나가 실패해도 나머지는 계속합니다.

    방향(`side`)을 알 수 없는 줄은 취소 요청을 만들 수 없어 건너뛰고 기록합니다(원래는 ValueError 로 단계 전체가 끊겼음).
    반환: 취소 요청을 낸 수. 미체결 조회가 실패하면 -1.
    """
    try:
        records = gateway.open_orders()
    except Exception as exc:  # noqa: BLE001
        log("cancel_open", msg=f"미체결 조회 실패: {type(exc).__name__}: {exc}", action="continue")
        return -1
    sent = 0
    for rec in records:
        if sym is not None and rec.sym != sym:
            continue
        if rec.side not in ("buy", "sell"):
            log("cancel_open", msg=f"방향을 알 수 없는 미체결 {rec.order_no} 은(는) 취소하지 못했습니다. 직접 확인하세요.",
                order_no=rec.order_no, action="manual_check")
            continue
        try:
            gateway.cancel(rec.sym, rec.side, int(rec.remaining_qty or rec.qty or 1), rec.order_no)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log("cancel_open", msg=f"{rec.order_no} 취소 실패: {type(exc).__name__}: {exc}", order_no=rec.order_no, action="continue")
    return sent


class OrderExecutor:
    """시장가 주문 → 접수 확인 → 주문내역으로 체결 확인 → (안 되면) 잔고로 대사."""

    def __init__(self, gateway: OverseasGateway, sym: str, *, fill_wait_seconds: float = 20.0, poll_seconds: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 alarm: Callable[..., None] | None = None) -> None:
        self.gw, self.sym = gateway, sym
        self.wait, self.poll, self._sleep, self._clock = fill_wait_seconds, poll_seconds, sleep, clock
        self.halted = False         # 잔고로도 결과를 확정하지 못하면(모호) 새 '진입'을 막습니다. 청산은 막지 않습니다.
        self.last_status = ""
        self.net = 0                # 이 실행이 아는 계좌 순포지션(시작 대사가 깨끗해야 시작하므로 0)
        self.exit_failures = 0      # 연달아 실패한 청산 수
        self._exit_retry_at: float | None = None
        self._alarm = alarm or self._journal_error

    # -- 기록
    def _journal_error(self, category: str, msg: str = "", **fields: Any) -> None:
        journal = getattr(self.gw, "journal", None)
        if journal is not None:
            journal.error(category, self.sym, msg=msg, **fields)

    def _event(self, name: str, text: str, **fields: Any) -> None:
        journal = getattr(self.gw, "journal", None)
        if journal is not None:
            journal.event(name, text, self.sym, **fields)

    # -- 주문
    def execute(self, intent: Intent, qty: int) -> Fill | None:
        entering = intent.kind == "enter"
        if self.halted and entering:
            self.last_status = "halted"
            return None
        if not entering and self._exit_retry_at is not None and self._clock() < self._exit_retry_at:
            self.last_status = "exit-backoff"          # 직전 청산이 실패해 간격을 두는 중(주문을 내지 않음)
            return None
        trade_side = intent.side if entering else -intent.side
        before = self.net
        request = OverseasOrderRequest(self.sym, "buy" if trade_side > 0 else "sell", qty, reduce_only=not entering)
        outcome = self.gw.place(request)
        if outcome.status == "unknown":
            outcome = self.gw.resolve_unknown(outcome)
        self.last_status = outcome.status
        fill = None
        if outcome.status == "rejected":
            if not entering:
                fill = self._exit_failed(intent, before, trade_side * qty, outcome)
            return self._done(fill, entering, before, trade_side * qty)
        if outcome.status == "accepted":
            fill = self._confirm(outcome.order_no, qty)
            if fill is None:
                self.last_status = "accepted-unconfirmed"
        if fill is None:
            fill = self._reconcile(intent, before, trade_side * qty, outcome.order_no)
            if fill is None and not entering and not self.halted:
                fill = self._exit_failed(intent, before, trade_side * qty, outcome)
        return self._done(fill, entering, before, trade_side * qty)

    def _done(self, fill: Fill | None, entering: bool, before: int, delta: int) -> Fill | None:
        if fill is not None:
            self.net = before + delta
            if not entering:
                self.exit_failures, self._exit_retry_at = 0, None
        return fill

    def _confirm(self, order_no: str, qty: int) -> Fill | None:
        """주문내역으로 체결 확인. 조회 예외는 '확인 실패'로 기록하고 다시 봅니다(체결 없음으로 보지 않음)."""
        deadline = self._clock() + self.wait
        while True:
            try:
                record = self.gw.fills_of(order_no)
            except Exception as exc:  # noqa: BLE001
                record = None
                self._journal_error("fill_query", msg=f"{type(exc).__name__}: {exc}", order_no=order_no, action="retry")
            if record is not None and record.exec_qty and record.exec_qty >= Decimal(qty) and record.exec_price:
                return Fill(float(record.exec_price), qty, "mock-account", order_no)
            if self._clock() >= deadline:
                return None
            self._sleep(self.poll)

    def _account(self) -> tuple[int, float | None] | None:
        """잔고 조회(실패하면 두 번 더). (순포지션, 평균가) 또는 None(조회 실패·방향 모름)."""
        for i in range(3):
            try:
                held = self.gw.positions()
            except Exception as exc:  # noqa: BLE001
                self._journal_error("position_query", msg=f"{type(exc).__name__}: {exc}", action="retry" if i < 2 else "give_up")
                self._sleep(0.5)
                continue
            net = net_position(held, self.sym)
            return None if net is None else (net, avg_price(held, self.sym))
        return None

    def _reconcile(self, intent: Intent, before: int, delta: int, order_no: str) -> Fill | None:
        """결과 모름·체결 미확인 → 잔고로 대사합니다(kq150 `_reconcile_entry`/`_reconcile_exit` 와 같은 원칙).

        잔고 = 주문 후 예상 → 체결로 봄(가격은 진입이면 잔고 평균가, 아니면 기준가로 추정)
        잔고 = 주문 전      → 걸린 주문을 취소하고 다시 확인. 그대로면 미체결(None, 멈추지 않음)
        그 밖(조회 실패 포함) → 모호. 신규 진입을 멈추고(halted) 경보.
        """
        expected = before + delta
        self._journal_error("order_unconfirmed", msg=f"주문 결과를 확인하지 못해 잔고로 대사합니다(상태 {self.last_status}). 같은 주문을 다시 내지 않습니다.",
                            order_no=order_no, action="reconcile")
        got = self._account()
        if got is not None and got[0] == before:
            cancel_open_orders(self.gw, self.sym, self._journal_error)   # 접수만 된 주문이 나중에 체결되지 않게
            self._sleep(1.0)
            got = self._account()
        net = None if got is None else got[0]
        if net == expected:
            est = (got[1] if intent.kind == "enter" and got[1] else None) or intent.ref_price
            self.last_status = "reconciled-filled"
            self._event("order_reconciled", "잔고 대사: 체결로 확인(가격 추정)", order_no=order_no, net=net, est_price=est)
            return Fill(float(est), abs(delta), "mock-account", order_no, note="잔고 대사로 체결 확인(가격 추정)")
        if net == before:
            self.last_status = "reconciled-not-filled"
            self._event("order_reconciled", "잔고 대사: 체결되지 않음(미체결 취소)", order_no=order_no, net=net)
            return None
        self.halted = True
        self._alarm("order_state", f"주문 결과를 잔고로도 확정하지 못했습니다(잔고 {net}, 예상 {before} 또는 {expected}). "
                                   "신규 진입을 멈춥니다. 주문내역·잔고를 확인하세요.", order_no=order_no, action="halt")
        return None

    def _exit_failed(self, intent: Intent, before: int, delta: int, outcome) -> Fill | None:
        """청산 실패(거부·미체결) → 백오프. 연달아 MAX_EXIT_FAILURES 번이면 경보를 보내고 잔고로 대조합니다."""
        self.exit_failures += 1
        n = self.exit_failures
        wait = EXIT_BACKOFF_SECONDS[min(n - 1, len(EXIT_BACKOFF_SECONDS) - 1)]
        self._exit_retry_at = self._clock() + wait
        self._journal_error("exit_failed", msg=f"청산 실패 {n}회째({getattr(outcome, 'code', '')} {getattr(outcome, 'message', '')}). {wait:.0f}초 뒤 다시 냅니다.",
                            attempt=n, action="backoff")
        if n == MAX_EXIT_FAILURES:
            self._alarm("exit_rejected", f"청산 주문이 {n}번 연달아 실패했습니다. 잔고로 대조하고, 이후 최대 {EXIT_BACKOFF_SECONDS[-1]:.0f}초 간격으로만 다시 냅니다.",
                        attempt=n, action="reconcile")
        if n >= MAX_EXIT_FAILURES:
            got = self._account()
            if got is not None and got[0] == before + delta:      # 계좌는 이미 정리됨 → 엔진 상태를 맞춤
                self.last_status = "reconciled-filled"
                self._event("order_reconciled", "청산 실패 뒤 잔고 대사: 이미 정리됨(가격 추정)", net=got[0])
                return Fill(float(intent.ref_price), abs(delta), "mock-account", "", note="잔고 대사로 청산 확인(가격 추정)")
        return None


class EchoExecutor:
    """실행기를 감싸 화면에 한 줄씩 보여 줍니다(운영 중 확인·캡처용). 동작은 그대로 위임합니다."""

    def __init__(self, inner, say=print) -> None:
        self._inner, self._say = inner, say

    def execute(self, intent: Intent, qty: int) -> Fill | None:
        fill = self._inner.execute(intent, qty)
        direction = "매수" if (intent.side > 0) == (intent.kind == "enter") else "매도"
        what = "진입" if intent.kind == "enter" else "청산"
        result = f"체결 {fill.price} ({fill.source}{', ' + fill.note if fill.note else ''})" if fill else f"체결 확인 안 됨 ({getattr(self._inner, 'last_status', '')})"
        self._say(f"[{intent.ts:%H:%M}] 규칙 {intent.rule} {what} {direction} 1계약 — {intent.reason} → {result}")
        return fill

    def __getattr__(self, name):
        return getattr(self._inner, name)
