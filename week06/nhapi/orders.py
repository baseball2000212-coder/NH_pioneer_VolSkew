"""국내 주문·정정·취소와 주문내역 대사.

주문은 **되돌릴 수 없습니다.** 그래서 조회와 다르게 다룹니다.

- **자동 재시도하지 않습니다.** 응답이 오지 않으면 `unknown` 이고, 사람이(또는 `resolve_unknown`이)
  주문내역을 확인한 뒤에야 다시 낼지 정합니다.
- **HTTP 200 은 성공이 아닙니다.** 성공은 `ord_no` 가 있고 `msg_cd` 가 빈 문자열일 때입니다.
- **주문 접수 ≠ 체결.** `accepted` 는 접수일 뿐입니다. 체결은 주문내역(`ord_sts`·`exec_qty`)이나
  실시간 주문/체결 통보로 확인한 것만 체결로 씁니다.
- 주간·야간은 **API 자체가 다릅니다**(`order` / `order-night`). `OrderRequest.session` 으로 고릅니다.
- 이 프로젝트는 모의(paper) 전용입니다. 환경이 paper 가 아니면 주문을 막습니다.

시장가·최유리는 `ord_pric` 가 `"0"` 입니다. 정정·취소의 `org_ord_no` 는 앞을 0 으로 채운 10자리입니다.
해외 주문(`overseas_order`)은 3주차에 추가합니다.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from . import codes
from .client import Client
from .config import PAPER
from .errors import BusinessError, GatewayError, HttpError, OrderGuardError, TransportError
from .instruments import is_on_tick
from .journal import Journal
from .parsers import OrderRecord, Position, parse_order_ack, parse_orders, parse_positions
from .risk import Decision
from .timeutil import KST, yyyymmdd

Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market", "best"]
ExecCond = Literal["FAS", "FOK", "FAK"]
Session = Literal["day", "night"]
Status = Literal["accepted", "rejected", "unknown"]

SIDE_CODE = {"buy": "1", "sell": "2"}
ORDER_TYPE_CODE = {"limit": "1", "market": "2", "best": "4"}
EXEC_COND_CODE = {"FAS": "1", "FOK": "2", "FAK": "3"}
ORDER_NO_WIDTH = 10
#: 대사 때 주문 접수 시각을 견주는 여유(초). 시계 오차를 흡수합니다.
RECONCILE_SLACK_SECONDS = 5


def order_no(text: Any) -> str:
    """원주문번호를 열 자리로 맞춥니다. 이미 열 자리 이상이면 자르지 않고 그대로 둡니다."""
    return str(text or "").strip().rjust(ORDER_NO_WIDTH, "0")


@dataclass(frozen=True)
class OrderRequest:
    sym: str
    side: Side
    qty: int
    ord_type: OrderType = "market"
    price: Decimal | None = None
    exec_cond: ExecCond = "FAS"
    session: Session = "day"
    #: 호가 단위. 주면 지정가 가격이 틱의 배수인지 검사합니다.
    tick: Decimal | None = None


@dataclass
class OrderOutcome:
    """주문 한 건의 결과. `accepted` 는 접수일 뿐 체결이 아닙니다."""

    status: Status
    request: OrderRequest | None = None
    order_no: str = ""
    code: str = ""
    message: str = ""
    #: 요청이 서버에 나가지 못했음이 확실한가(연결 실패 등).
    not_sent: bool = False
    sent_at: float | None = None
    needs_detail: bool = False


@dataclass
class ReconcileReport:
    """시작 시 대사 결과: 보유·미체결."""

    positions: list[Position] = field(default_factory=list)
    open_orders: list[OrderRecord] = field(default_factory=list)


def _api(base: str, session: Session) -> str:
    return f"{base}-night" if session == "night" else base


def _price_text(request: OrderRequest) -> str:
    if request.ord_type in ("market", "best"):
        return "0"
    assert request.price is not None
    return format(request.price, "f")


def validate(request: OrderRequest) -> None:
    """보내기 전 검사. 잘못되면 ValueError."""
    if request.side not in SIDE_CODE:
        raise ValueError(f"알 수 없는 방향: {request.side!r}")
    if request.ord_type not in ORDER_TYPE_CODE:
        raise ValueError(f"알 수 없는 주문유형: {request.ord_type!r}")
    if request.exec_cond not in EXEC_COND_CODE:
        raise ValueError(f"알 수 없는 체결조건: {request.exec_cond!r}")
    if not isinstance(request.qty, int) or request.qty <= 0:
        raise ValueError("수량은 1 이상의 정수여야 합니다.")
    if request.ord_type == "limit":
        if request.price is None or request.price <= 0:
            raise ValueError("지정가 주문에는 0 보다 큰 가격이 필요합니다.")
        if request.tick is not None and not is_on_tick(request.price, request.tick):
            raise ValueError(f"가격 {request.price} 이(가) 호가 단위 {request.tick} 의 배수가 아닙니다.")


class OrderGateway:
    """주문·정정·취소와 대사."""

    def __init__(
        self,
        client: Client,
        *,
        journal: Journal | None = None,
        pre_check: Callable[[OrderRequest], Decision] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.journal = journal if journal is not None else client.journal
        self._pre_check = pre_check
        self._clock = clock
        #: 접수 응답을 받은 적 있는 주문번호. 대사에서 "새로 생긴 주문"을 가릴 때 씁니다.
        self.known_order_nos: set[str] = set()

    # ------------------------------------------------------------------ 주문

    def place(self, request: OrderRequest) -> OrderOutcome:
        """신규 주문. 재시도하지 않습니다."""
        self._guard()
        validate(request)
        if self._pre_check is not None:
            decision = self._pre_check(request)
            if not decision.allowed:
                self._log("blocked", request, reason=decision.reason)
                return OrderOutcome("rejected", request, code="RISK", message=decision.reason, not_sent=True)
        payload = {
            "sym": request.sym,
            "trd_div": SIDE_CODE[request.side],
            "ord_tp_cd": ORDER_TYPE_CODE[request.ord_type],
            "ord_qty": str(request.qty),
            "ord_pric": _price_text(request),
            "exec_cnd_cd": EXEC_COND_CODE[request.exec_cond],
        }
        return self._submit(_api("domestic_order", request.session), payload, request)

    def modify(self, request: OrderRequest, org_ord_no: str) -> OrderOutcome:
        """정정 주문. `request.qty` 는 정정수량입니다."""
        self._guard()
        validate(request)
        payload = {
            "sym": request.sym,
            "trd_div": SIDE_CODE[request.side],
            "ord_tp_cd": ORDER_TYPE_CODE[request.ord_type],
            "ord_qty": str(request.qty),
            "ord_pric": _price_text(request),
            "exec_cnd_cd": EXEC_COND_CODE[request.exec_cond],
            "org_ord_no": order_no(org_ord_no),
        }
        return self._submit(_api("domestic_modify", request.session), payload, request)

    def cancel(self, sym: str, side: Side, qty: int, org_ord_no: str, *, session: Session = "day") -> OrderOutcome:
        """취소 주문. 요청 칸은 종목·매매구분·취소수량·원주문번호 넷뿐입니다."""
        self._guard()
        request = OrderRequest(sym=sym, side=side, qty=qty, session=session)
        validate(request)
        payload = {
            "sym": sym,
            "trd_div": SIDE_CODE[side],
            "ord_qty": str(qty),
            "org_ord_no": order_no(org_ord_no),
        }
        return self._submit(_api("domestic_cancel", session), payload, request)

    def _guard(self) -> None:
        if self.client.config.default_environment != PAPER:
            raise OrderGuardError("이 프로젝트는 모의(paper) 전용입니다. 주문을 내지 않았습니다.")

    def _submit(self, api_id: str, payload: dict[str, str], request: OrderRequest) -> OrderOutcome:
        sent_at = self._clock()
        self._log("sent", request, api_id=api_id)
        try:
            result = self.client.call(api_id, payload)
        except BusinessError as exc:
            outcome = OrderOutcome(
                "rejected", request, code=exc.code, message=exc.detail, sent_at=sent_at,
                needs_detail=codes.needs_detail(exc.code),
            )
        except GatewayError as exc:
            # 게이트웨이 거절은 요청이 처리됐는지 모를 수 있습니다. 거절로 단정하지 않습니다.
            outcome = OrderOutcome("unknown", request, code=exc.code, message=exc.detail, sent_at=sent_at)
        except HttpError as exc:
            outcome = OrderOutcome("unknown", request, code=f"HTTP{exc.status}", message=exc.message, sent_at=sent_at)
        except TransportError as exc:
            if exc.sent is False:
                outcome = OrderOutcome("rejected", request, code="NOT_SENT", message=exc.message, not_sent=True, sent_at=sent_at)
            else:
                outcome = OrderOutcome("unknown", request, code="NO_RESPONSE", message=exc.message, sent_at=sent_at)
        else:
            ack = parse_order_ack(result.body)
            if ack.order_no:
                self.known_order_nos.add(ack.order_no)
                outcome = OrderOutcome("accepted", request, order_no=ack.order_no, sent_at=sent_at)
            else:
                # 거부 사유도 주문번호도 없습니다. 나갔을 수 있으므로 확인이 필요합니다.
                outcome = OrderOutcome(
                    "unknown", request, code="NO_ORDER_NO",
                    message="접수 응답에 주문번호가 없습니다. 주문내역에서 확인하세요.", sent_at=sent_at,
                )
        self._log(
            {"accepted": "ack", "rejected": "reject", "unknown": "unknown"}[outcome.status],
            request, api_id=api_id, order_no=outcome.order_no, reject_code=outcome.code, msg=outcome.message,
        )
        return outcome

    # ------------------------------------------------------------------ 대사

    def history(self, *, exec_div: str = "0", session_code: str = "0") -> list[OrderRecord]:
        """주문내역(연속조회로 모두). `exec_div` 0 전체·1 체결·2 미체결·3 미접수, `session_code` 0 전체·1 주간·2 야간."""
        payload = {
            "exec_div": exec_div, "trd_div": "0", "trd_tm_div_cd": session_code,
            "ord_div_cd": "0", "nxt_key": "",
        }
        records: list[OrderRecord] = []
        for body in self.client.paginate("domestic_order-history", payload):
            records.extend(parse_orders(body))
        return records

    def open_orders(self) -> list[OrderRecord]:
        """미체결 주문."""
        return self.history(exec_div="2")

    def positions(self) -> list[Position]:
        """보유 잔고."""
        found: list[Position] = []
        for body in self.client.paginate("domestic_open-interest", {"nxt_key": ""}):
            found.extend(parse_positions(body))
        return found

    def startup_reconcile(self) -> ReconcileReport:
        """실행 시작 시 보유·미체결을 확인합니다(기록에도 남깁니다)."""
        report = ReconcileReport(positions=self.positions(), open_orders=self.open_orders())
        for record in report.open_orders:
            self.known_order_nos.add(record.order_no)
        if self.journal is not None:
            self.journal.event(
                "reconcile", "시작 시 대사",
                positions=len(report.positions), open_orders=len(report.open_orders),
            )
        return report

    def resolve_unknown(self, outcome: OrderOutcome) -> OrderOutcome:
        """응답을 못 받은 주문이 접수됐는지 주문내역으로 확인합니다.

        오늘 주문 중 (종목·방향·수량이 같고) 이 주문을 보낸 시각 이후에 접수된, 아직 모르는 주문번호를 찾습니다.
        정확히 하나면 접수로 확정하고, 없으면 `unknown` 을 유지합니다(아직 반영 전일 수 있음).
        둘 이상이면 모호하므로 `unknown` 을 유지합니다. 확정되기 전에는 같은 주문을 다시 내지 않습니다.
        """
        request = outcome.request
        if outcome.status != "unknown" or request is None or outcome.sent_at is None:
            return outcome
        sent_kst = datetime.fromtimestamp(outcome.sent_at, KST)
        today = yyyymmdd(sent_kst)
        floor = datetime.fromtimestamp(outcome.sent_at - RECONCILE_SLACK_SECONDS, KST).strftime("%H%M%S")
        matches = [
            record for record in self.history()
            if record.trade_date == today
            and record.sym == request.sym
            and record.side == request.side
            and record.qty == Decimal(request.qty)
            and record.order_no == record.org_order_no  # 신규 주문 줄
            and record.order_no not in self.known_order_nos
            and record.recv_time[:6] >= floor
        ]
        numbers = sorted({record.order_no for record in matches})
        if len(numbers) == 1:
            self.known_order_nos.add(numbers[0])
            resolved = OrderOutcome("accepted", request, order_no=numbers[0], code="RECONCILED", sent_at=outcome.sent_at)
            self._log("ack", request, order_no=numbers[0], note="주문내역 대사로 접수 확인")
            return resolved
        outcome.message = (outcome.message + " ").lstrip() + (
            "[대사] 일치하는 주문 없음" if not numbers else f"[대사] 후보 {len(numbers)}건 — 모호"
        )
        return outcome

    # ------------------------------------------------------------------ 기록

    def _log(self, stage: str, request: OrderRequest, **fields: Any) -> None:
        if self.journal is None:
            return
        self.journal.order(
            stage, request.sym, side=request.side, qty=request.qty, ord_type=request.ord_type,
            price=request.price, session=request.session, **fields,
        )
