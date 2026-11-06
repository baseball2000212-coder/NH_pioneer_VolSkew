"""해외 주문·정정·취소와 해외 응답 파서 (3주차 추가).

국내(`orders.py`)와 다른 점
- **주문 API 하나**(`overseas_order`)가 신규·정정·취소를 모두 합니다. `qt_div_cd` 1 신규 · 2 정정 · 3 취소.
- 주문유형 `glb_ord_tp_cd`: 1 시장가 · 2 지정가 · 3 STOP · 4 STOP지정가. 시장가·STOP 은 `ord_pric` 가 "0",
  STOP 이 아니면 `stop_pric` 은 빈 값으로 실어 보냅니다.
- 거부 사유는 `err_cd`·`err_msg` 로 옵니다(`00000` 은 정상). 성공은 `ord_no` 가 있고 `err_cd` 가 거절이 아닐 때입니다.
- 주문내역은 `inq_div` 로 거릅니다: 0 전체 · 1 주문 · 2 체결 · 3 미체결 · 4 미체결(지정청산).

**주문은 자동 재시도하지 않습니다.** 응답이 없으면 `unknown` 이고 주문내역으로 확인한 뒤에야 다시 냅니다.
**주문 접수 ≠ 체결.** 체결 수량·가격은 주문내역(`fills_of`)으로 확인한 것만 씁니다.

칸 이름 출처: 운영진 예시 앱 `backend/overseas_futureoption/overseas_account.py` 의 해외 잔고·주문내역 해석.
주문내역·잔고의 실제 응답은 모의 스모크(3단계)에서 확인하기 전까지 이 칸 이름이 맞는지 보증하지 않습니다.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from . import codes
from .client import Client
from .config import PAPER
from .errors import BusinessError, GatewayError, HttpError, OrderGuardError, TransportError
from .instruments import is_on_tick
from .journal import Journal
from .orders import ORDER_NO_WIDTH, RECONCILE_SLACK_SECONDS, OrderOutcome, ReconcileReport, order_no
from .parsers import ORDER_STATUS, SIDE_CODES, Bar, OrderRecord, Position, Quote, rows_of, to_dec, _bar_ts, _text
from .risk import Decision
from .timeutil import KST

Side = Literal["buy", "sell"]
OvOrderType = Literal["market", "limit", "stop", "stop_limit"]

SIDE_CODE = {"buy": "1", "sell": "2"}
NEW, MODIFY, CANCEL = "1", "2", "3"
ORDER_TYPE_CODE = {"market": "1", "limit": "2", "stop": "3", "stop_limit": "4"}
NO_ORIGINAL = "0"
INQ_ALL, INQ_FILLED, INQ_OPEN = "0", "2", "3"


@dataclass(frozen=True)
class OverseasOrderRequest:
    sym: str
    side: Side
    qty: int
    ord_type: OvOrderType = "market"
    price: Decimal | None = None
    stop_price: Decimal | None = None
    tick: Decimal | None = None
    #: 보유를 줄이는 청산 주문(반대 방향, 수량 ≤ 보유)인가. 참이면 `pre_check`(킬 스위치 등 진입 점검)를 건너뜁니다.
    #: 실행기의 청산과 잔고 기준 정리만 참으로 둡니다. 주문 내용(payload)에는 실리지 않습니다.
    reduce_only: bool = False


#: 응답 없는 주문을 주문내역에서 찾을 때 몇 번, 몇 초 간격으로 볼지(접수 직후에는 내역에 늦게 뜰 수 있음)
RESOLVE_TRIES = 10
RESOLVE_INTERVAL_SECONDS = 1.0


def validate(request: OverseasOrderRequest) -> None:
    if request.side not in SIDE_CODE:
        raise ValueError(f"알 수 없는 방향: {request.side!r}")
    if request.ord_type not in ORDER_TYPE_CODE:
        raise ValueError(f"알 수 없는 주문유형: {request.ord_type!r}")
    if not isinstance(request.qty, int) or request.qty <= 0:
        raise ValueError("수량은 1 이상의 정수여야 합니다.")
    if request.ord_type in ("limit", "stop_limit") and (request.price is None or request.price <= 0):
        raise ValueError("지정가 주문에는 0 보다 큰 가격이 필요합니다.")
    if request.ord_type in ("stop", "stop_limit") and (request.stop_price is None or request.stop_price <= 0):
        raise ValueError("STOP 주문에는 0 보다 큰 스탑가격이 필요합니다.")
    for value in (request.price, request.stop_price):
        if value is not None and request.tick is not None and not is_on_tick(value, request.tick):
            raise ValueError(f"가격 {value} 이(가) 호가 단위 {request.tick} 의 배수가 아닙니다.")


def _fmt(value: Decimal | None, default: str) -> str:
    return default if value is None else format(value, "f")


# ---------------------------------------------------------------- 파서


def parse_overseas_bars(body: Any) -> list[Bar]:
    """해외 차트(분) → 오래된 봉부터. 시각은 `ref_dt` + `exch_tm`(한국시간), 거래량은 `cum_trd_qty`(그 분의 체결량)."""
    bars = [
        Bar(
            ts=_bar_ts(_text(row, "occurs1_ref_dt"), _text(row, "occurs1_exch_tm")),
            open=to_dec(row.get("occurs1_open_pric")), high=to_dec(row.get("occurs1_high_pric")),
            low=to_dec(row.get("occurs1_low_pric")), close=to_dec(row.get("occurs1_close_pric")),
            volume=to_dec(row.get("occurs1_cum_trd_qty")),
        )
        for row in rows_of(body)
    ]
    bars = [bar for bar in bars if bar.ts is not None]
    bars.sort(key=lambda bar: bar.ts)
    return bars


def parse_overseas_quote(body: Any) -> Quote:
    """해외 현재가(첫 줄)."""
    rows = rows_of(body)
    row = rows[0] if rows else {}
    return Quote(
        sym=_text(row, "occurs1_sym"), name=_text(row, "occurs1_sym_nm"),
        last=to_dec(row.get("occurs1_last_pric")), bid=to_dec(row.get("occurs1_bid_pric")), ask=to_dec(row.get("occurs1_ask_pric")),
        open=to_dec(row.get("occurs1_open_pric")), high=to_dec(row.get("occurs1_high_pric")), low=to_dec(row.get("occurs1_low_pric")),
        prev_close=to_dec(row.get("occurs1_pre_close_pric")), multiplier=None, last_trade_date=_text(row, "occurs1_biz_dt"),
    )


def parse_overseas_positions(body: Any) -> list[Position]:
    """해외 잔고내역. 수량이 0 인 줄은 뺍니다. 매수·매도 구분은 `trd_tp`(1 매수 · 2 매도)."""
    found = []
    for row in rows_of(body):
        qty = to_dec(row.get("occurs1_opint_qty")) or Decimal(0)
        if qty == 0:
            continue
        found.append(Position(
            sym=_text(row, "occurs1_sym"), name=_text(row, "occurs1_sym_nm"), side=SIDE_CODES.get(_text(row, "occurs1_trd_tp"), ""),
            qty=qty, closable_qty=to_dec(row.get("occurs1_cls_psb_qty")), avg_price=to_dec(row.get("occurs1_avg_pric")),
            pl_amt=to_dec(row.get("occurs1_pl_amt")),
        ))
    return found


def parse_overseas_orders(body: Any) -> list[OrderRecord]:
    """해외 주문내역. 줄 하나가 주문 하나입니다(정정하면 새 줄 하나만, 취소하면 줄이 사라집니다)."""
    records = []
    for row in rows_of(body):
        code = _text(row, "occurs1_ord_sts")
        records.append(OrderRecord(
            order_no=_text(row, "occurs1_ord_no"), org_order_no=_text(row, "occurs1_org_ord_no"), sym=_text(row, "occurs1_sym"),
            side=SIDE_CODES.get(_text(row, "occurs1_trd_tp"), ""), status_code=code, status=ORDER_STATUS.get(code, "unknown"),
            status_name=_text(row, "occurs1_ord_sts_nm"), qty=to_dec(row.get("occurs1_ord_qty")), exec_qty=to_dec(row.get("occurs1_exec_qty")),
            remaining_qty=to_dec(row.get("occurs1_ord_rem_qty")), cancelled_qty=to_dec(row.get("occurs1_cxl_qty")),
            price=to_dec(row.get("occurs1_ord_pric")), exec_price=to_dec(row.get("occurs1_exec_pric")) if to_dec(row.get("occurs1_exec_pric")) else to_dec(row.get("occurs1_ord_avg_pric")),
            trade_date=_text(row, "occurs1_ord_dt"), recv_time=_text(row, "occurs1_ord_rcv_tm"), exec_time=_text(row, "occurs1_exec_tm"),
            exec_no="", reject_reason=_text(row, "occurs1_rejt_rsn"),
        ))
    return records


def parse_overseas_ack(body: Any) -> tuple[str, str]:
    """주문 응답 → (주문번호, 거부코드). 접수되면 주문번호가 있습니다."""
    body = body if isinstance(body, dict) else {}
    return _text(body, "ord_no"), _text(body, "err_cd")


def _rcv_datetime(text: str) -> datetime | None:
    """열네 자리 `YYYYMMDDHHMMSS` → 한국시간으로 본 시각(시간대 표기가 없어 한국시간으로 가정)."""
    try:
        return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None


# ---------------------------------------------------------------- 게이트웨이


class OverseasGateway:
    """해외 주문·정정·취소와 대사. 모의(paper) 전용입니다."""

    def __init__(
        self,
        client: Client,
        *,
        journal: Journal | None = None,
        pre_check: Callable[[OverseasOrderRequest], Decision] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.journal = journal if journal is not None else client.journal
        self._pre_check = pre_check
        self._clock = clock
        self._sleep = sleep
        self.known_order_nos: set[str] = set()

    # -- 주문
    def place(self, request: OverseasOrderRequest) -> OrderOutcome:
        """신규 주문. 진입 점검(`pre_check`, 킬 스위치 등)은 진입에만 적용합니다 — 청산(`reduce_only`)은 막지 않습니다."""
        self._guard()
        validate(request)
        if self._pre_check is not None and not request.reduce_only:
            decision = self._pre_check(request)
            if not decision.allowed:
                self._log("blocked", request, reason=decision.reason)
                return OrderOutcome("rejected", request, code="RISK", message=decision.reason, not_sent=True)
        return self._submit(self._payload(NEW, request, NO_ORIGINAL), request)

    def modify(self, request: OverseasOrderRequest, org_ord_no: str) -> OrderOutcome:
        self._guard()
        validate(request)
        return self._submit(self._payload(MODIFY, request, order_no(org_ord_no)), request)

    def cancel(self, sym: str, side: Side, qty: int, org_ord_no: str) -> OrderOutcome:
        self._guard()
        request = OverseasOrderRequest(sym, side, qty)
        validate(request)
        return self._submit(self._payload(CANCEL, request, order_no(org_ord_no)), request)

    @staticmethod
    def _payload(kind: str, request: OverseasOrderRequest, org: str) -> dict[str, str]:
        market_like = request.ord_type in ("market", "stop")
        return {
            "qt_div_cd": kind, "sym": request.sym, "trd_div_cd": SIDE_CODE[request.side],
            "glb_ord_tp_cd": ORDER_TYPE_CODE[request.ord_type], "ord_qty": str(request.qty),
            "ord_pric": "0" if market_like else _fmt(request.price, "0"),
            "stop_pric": _fmt(request.stop_price, "") if request.ord_type in ("stop", "stop_limit") else "",
            "org_ord_no": org,
        }

    def _guard(self) -> None:
        if self.client.config.default_environment != PAPER:
            raise OrderGuardError("이 프로젝트는 모의(paper) 전용입니다. 주문을 내지 않았습니다.")

    def _submit(self, payload: dict[str, str], request: OverseasOrderRequest) -> OrderOutcome:
        sent_at = self._clock()
        self._log("sent", request, api_id="overseas_order", qt_div_cd=payload["qt_div_cd"])
        try:
            result = self.client.call("overseas_order", payload)
        except BusinessError as exc:
            outcome = OrderOutcome("rejected", request, code=exc.code, message=exc.detail, sent_at=sent_at, needs_detail=codes.needs_detail(exc.code))
        except GatewayError as exc:
            outcome = OrderOutcome("unknown", request, code=exc.code, message=exc.detail, sent_at=sent_at)
        except HttpError as exc:
            outcome = OrderOutcome("unknown", request, code=f"HTTP{exc.status}", message=exc.message, sent_at=sent_at)
        except TransportError as exc:
            if exc.sent is False:
                outcome = OrderOutcome("rejected", request, code="NOT_SENT", message=exc.message, not_sent=True, sent_at=sent_at)
            else:
                outcome = OrderOutcome("unknown", request, code="NO_RESPONSE", message=exc.message, sent_at=sent_at)
        else:
            number, _ = parse_overseas_ack(result.body)
            if number:
                self.known_order_nos.add(number)
                outcome = OrderOutcome("accepted", request, order_no=number, sent_at=sent_at)
            else:
                outcome = OrderOutcome("unknown", request, code="NO_ORDER_NO", sent_at=sent_at,
                                       message="접수 응답에 주문번호가 없습니다. 주문내역에서 확인하세요.")
        self._log({"accepted": "ack", "rejected": "reject", "unknown": "unknown"}[outcome.status], request,
                  api_id="overseas_order", order_no=outcome.order_no, reject_code=outcome.code, msg=outcome.message)
        return outcome

    # -- 대사
    def history(self, *, inq_div: str = INQ_ALL) -> list[OrderRecord]:
        records: list[OrderRecord] = []
        for body in self.client.paginate("overseas_order-history", {"inq_div": inq_div, "trd_div_cd": "0", "nxt_key": ""}):
            records.extend(parse_overseas_orders(body))
        return records

    def open_orders(self) -> list[OrderRecord]:
        return self.history(inq_div=INQ_OPEN)

    def positions(self) -> list[Position]:
        found: list[Position] = []
        for body in self.client.paginate("overseas_open-interest", {"nxt_key": ""}):
            found.extend(parse_overseas_positions(body))
        return found

    def startup_reconcile(self) -> ReconcileReport:
        report = ReconcileReport(positions=self.positions(), open_orders=self.open_orders())
        for record in report.open_orders:
            self.known_order_nos.add(record.order_no)
        if self.journal is not None:
            self.journal.event("reconcile", "시작 시 대사", positions=len(report.positions), open_orders=len(report.open_orders))
        return report

    def fills_of(self, order_no_text: str) -> OrderRecord | None:
        """주문번호의 주문내역 줄(체결 수량·가격 확인용). 없으면 None."""
        wanted = order_no(order_no_text)
        for record in self.history():
            if order_no(record.order_no) == wanted:
                return record
        return None

    def resolve_unknown(self, outcome: OrderOutcome, *, tries: int = RESOLVE_TRIES,
                        interval: float = RESOLVE_INTERVAL_SECONDS) -> OrderOutcome:
        """응답을 못 받은 주문이 접수됐는지 주문내역으로 확인(정확히 하나일 때만 확정, 모호하면 unknown 유지).

        접수 직후에는 내역에 아직 안 보일 수 있어 `interval` 초 간격으로 `tries` 번(기본 약 10초) 다시 봅니다.
        조회가 실패해도 예외를 내지 않고 다음 번에 다시 봅니다. 그래도 못 찾으면 unknown 그대로 돌려주고,
        판정은 부르는 쪽이 잔고(`positions`)로 합니다.
        """
        request = outcome.request
        if outcome.status != "unknown" or request is None or outcome.sent_at is None:
            return outcome
        floor = datetime.fromtimestamp(outcome.sent_at - RECONCILE_SLACK_SECONDS, KST)
        numbers: list[str] = []
        failures = 0
        for attempt in range(max(1, tries)):
            if attempt:
                self._sleep(interval)
            try:
                records = self.history()
            except Exception as exc:  # noqa: BLE001 — 조회 실패는 '없음'이 아닙니다. 다음 번에 다시 봅니다.
                failures += 1
                if self.journal is not None:
                    self.journal.error("resolve_unknown_query", request.sym, msg=f"{type(exc).__name__}: {exc}", attempt=attempt + 1, action="retry")
                continue
            matches = []
            for record in records:
                when = _rcv_datetime(record.recv_time)
                if (record.sym == request.sym and record.side == request.side and record.qty == Decimal(request.qty)
                        and record.order_no not in self.known_order_nos and when is not None and when >= floor):
                    matches.append(record.order_no)
            numbers = sorted(set(matches))
            if len(numbers) == 1:
                self.known_order_nos.add(numbers[0])
                self._log("ack", request, order_no=numbers[0], note=f"주문내역 대사로 접수 확인({attempt + 1}번째 조회)")
                return OrderOutcome("accepted", request, order_no=numbers[0], code="RECONCILED", sent_at=outcome.sent_at)
            if len(numbers) > 1:
                break
        if numbers:
            note = f"[대사] 후보 {len(numbers)}건 — 모호"
        elif failures >= max(1, tries):
            note = "[대사] 주문내역 조회 실패"
        else:
            note = "[대사] 일치하는 주문 없음"
        outcome.message = (outcome.message + " ").lstrip() + note
        return outcome

    def _log(self, stage: str, request: OverseasOrderRequest, **fields: Any) -> None:
        if self.journal is not None:
            self.journal.order(stage, request.sym, side=request.side, qty=request.qty, ord_type=request.ord_type, price=request.price, **fields)
