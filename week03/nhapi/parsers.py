"""응답 원문 → 도메인 값. 순수 함수만 있습니다(네트워크·파일 접근 없음).

숫자는 문자열로 오므로 `Decimal` 로 바꿉니다. 앞에 0 이 길게 붙은 값(`000000000100000000.000`)도 그대로 읽힙니다.
칸 이름은 `tests/fixtures` 의 응답 샘플로 확인한 것만 씁니다. 샘플이 없는 API(주문가능수량 등)는
원문을 그대로 쓰고, 실제 호출로 칸을 확인한 뒤 여기에 추가합니다.

주의: 서버는 줄(`Occurs1`)이 없는 조회에 `nrec: "0000"` 만 보냅니다. 이것은 오류가 아니라 빈 결과입니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .timeutil import KST

SIDE_CODES = {"1": "buy", "2": "sell"}
#: 주문내역의 처리구분(`ord_sts`). 명세에 뜻이 적힌 값입니다.
ORDER_STATUS = {
    "1": "accepted",
    "3": "confirmed",
    "4": "partial",
    "6": "filled",
    "8": "auto-cancelled",
    "9": "rejected",
}


def to_dec(value: Any) -> Decimal | None:
    """문자열 숫자 → Decimal. 비었거나 숫자가 아니면 None."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def rows_of(body: Any, field: str = "Occurs1") -> list[dict[str, Any]]:
    """본문의 줄 목록. 없으면 빈 목록입니다."""
    if not isinstance(body, dict):
        return []
    rows = body.get(field)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _text(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "").strip()


# ---------------------------------------------------------------- 현재가·호가


@dataclass(frozen=True)
class Quote:
    sym: str
    name: str
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    prev_close: Decimal | None
    multiplier: Decimal | None
    last_trade_date: str


def parse_price(body: dict[str, Any]) -> Quote:
    """국내 현재가."""
    return Quote(
        sym=_text(body, "sym_exch") or _text(body, "sym"),
        name=_text(body, "sym_nm"),
        last=to_dec(body.get("last_pric")),
        bid=to_dec(body.get("bid_pric")),
        ask=to_dec(body.get("ask_pric")),
        open=to_dec(body.get("open_pric")),
        high=to_dec(body.get("high_pric")),
        low=to_dec(body.get("low_pric")),
        prev_close=to_dec(body.get("ref_pric")),
        multiplier=to_dec(body.get("mltplr")),
        last_trade_date=_text(body, "lst_trd_dt"),
    )


@dataclass(frozen=True)
class BookLevel:
    bid_price: Decimal | None
    bid_qty: Decimal | None
    ask_price: Decimal | None
    ask_qty: Decimal | None


@dataclass(frozen=True)
class Book:
    sym: str
    time: str
    last: Decimal | None
    levels: tuple[BookLevel, ...]

    @property
    def best_bid(self) -> Decimal | None:
        return self.levels[0].bid_price if self.levels else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.levels[0].ask_price if self.levels else None


def parse_book(body: dict[str, Any]) -> Book:
    """국내 호가(최대 10단계). 값이 0 인 빈 단계는 뺍니다."""
    levels = []
    for row in rows_of(body):
        level = BookLevel(
            bid_price=to_dec(row.get("occurs1_bid_best_pric")),
            bid_qty=to_dec(row.get("occurs1_bid_qty")),
            ask_price=to_dec(row.get("occurs1_ask_best_pric")),
            ask_qty=to_dec(row.get("occurs1_ask_qty")),
        )
        if (level.bid_price or 0) == 0 and (level.ask_price or 0) == 0:
            continue
        levels.append(level)
    return Book(
        sym=_text(body, "sym"),
        time=_text(body, "qt_tm"),
        last=to_dec(body.get("last_pric")),
        levels=tuple(levels),
    )


# ---------------------------------------------------------------- 분봉


@dataclass(frozen=True)
class Bar:
    ts: datetime | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None


def _bar_ts(date_text: str, time_text: str) -> datetime | None:
    stamp = f"{date_text}{time_text.zfill(6)}"
    try:
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None


def parse_bars(body: dict[str, Any]) -> list[Bar]:
    """국내 차트(분) → 오래된 봉부터 정렬한 목록. 시각은 `ref_dt` + `exch_tm`."""
    bars = [
        Bar(
            ts=_bar_ts(_text(row, "occurs1_ref_dt"), _text(row, "occurs1_exch_tm")),
            open=to_dec(row.get("occurs1_open_pric")),
            high=to_dec(row.get("occurs1_high_pric")),
            low=to_dec(row.get("occurs1_low_pric")),
            close=to_dec(row.get("occurs1_close_pric")),
            volume=to_dec(row.get("occurs1_cum_trd_qty")),
        )
        for row in rows_of(body)
    ]
    bars.sort(key=lambda bar: bar.ts or datetime.min.replace(tzinfo=KST))
    return bars


# ---------------------------------------------------------------- 품목별세부정보


@dataclass(frozen=True)
class ProductRow:
    """품목별세부정보 한 줄. 시간 구간은 `08:45~15:45` 글자 그대로입니다(해석은 `hours`)."""

    name: str
    is_night: bool
    tick: Decimal | None
    opening_auction: str
    closing_auction: str
    trading: str
    last_day_trading: str
    nearest_expiry: str
    settlement: str
    multiplier: Decimal | None
    tick_value: Decimal | None
    initial_margin_rate: str
    maintenance_margin_rate: str
    contract_initial_margin: Decimal | None


NIGHT_MARK = "(야간)"


def parse_product_detail(body: dict[str, Any]) -> list[ProductRow]:
    """국내 품목별세부정보. 주간·야간이 다른 줄로 오고 야간 줄은 이름이 `(야간)` 으로 시작합니다."""
    products = []
    for row in rows_of(body):
        name = _text(row, "occurs1_dtl_cd_ko_nm")
        products.append(
            ProductRow(
                name=name,
                is_night=name.startswith(NIGHT_MARK),
                tick=to_dec(row.get("occurs1_cd_val_inf_1")),
                opening_auction=_text(row, "occurs1_cd_val_inf_2"),
                closing_auction=_text(row, "occurs1_cd_val_inf_3"),
                trading=_text(row, "occurs1_cd_val_inf_4"),
                last_day_trading=_text(row, "occurs1_cd_val_inf_5"),
                nearest_expiry=_text(row, "occurs1_exp_dt"),
                settlement=_text(row, "occurs1_sett_nm"),
                multiplier=to_dec(row.get("occurs1_mltplr")),
                tick_value=to_dec(row.get("occurs1_tick_val")),
                initial_margin_rate=_text(row, "occurs1_init_mrgn"),
                maintenance_margin_rate=_text(row, "occurs1_mtn_mrgn"),
                contract_initial_margin=to_dec(row.get("occurs1_ctr_init_mrgn")),
            )
        )
    return products


# ---------------------------------------------------------------- 예수금·잔고


@dataclass(frozen=True)
class Deposit:
    total_assets: Decimal | None
    cash: Decimal | None
    orderable_total: Decimal | None
    orderable_cash: Decimal | None
    fee: Decimal | None
    initial_margin: Decimal | None
    maintenance_margin: Decimal | None


def parse_deposit(body: dict[str, Any]) -> Deposit:
    """국내 예수금조회."""
    return Deposit(
        total_assets=to_dec(body.get("dps_aset_tot_amt")),
        cash=to_dec(body.get("dps_csh_amt")),
        orderable_total=to_dec(body.get("ord_psb_tot_amt")),
        orderable_cash=to_dec(body.get("ord_psb_csh_amt")),
        fee=to_dec(body.get("fee")),
        initial_margin=to_dec(body.get("init_mrgn_tot_amt")),
        maintenance_margin=to_dec(body.get("mtn_mrgn_tam_amt")),
    )


@dataclass(frozen=True)
class Position:
    sym: str
    name: str
    side: str  # buy / sell
    qty: Decimal
    closable_qty: Decimal | None
    avg_price: Decimal | None
    pl_amt: Decimal | None


def parse_positions(body: dict[str, Any]) -> list[Position]:
    """국내 잔고내역. 주간·야간 줄이 한 응답에 섞여 올 수 있습니다. 수량이 0 인 줄은 뺍니다."""
    found = []
    for row in rows_of(body):
        qty = to_dec(row.get("occurs1_opint_qty")) or Decimal(0)
        if qty == 0:
            continue
        found.append(
            Position(
                sym=_text(row, "occurs1_sym"),
                name=_text(row, "occurs1_ko_sym_nm"),
                side=SIDE_CODES.get(_text(row, "occurs1_trd_div"), ""),
                qty=qty,
                closable_qty=to_dec(row.get("occurs1_cls_psb_qty")),
                avg_price=to_dec(row.get("occurs1_avg_pric")),
                pl_amt=to_dec(row.get("occurs1_pl_amt")),
            )
        )
    return found


# ---------------------------------------------------------------- 주문


@dataclass(frozen=True)
class OrderRecord:
    """주문내역 한 줄. 체결이 여러 번이면 줄도 여러 개일 수 있어 `exec_no` 로 가립니다."""

    order_no: str
    org_order_no: str
    sym: str
    side: str
    status_code: str
    status: str
    status_name: str
    qty: Decimal | None
    exec_qty: Decimal | None
    remaining_qty: Decimal | None
    cancelled_qty: Decimal | None
    price: Decimal | None
    exec_price: Decimal | None
    trade_date: str
    recv_time: str
    exec_time: str
    exec_no: str
    reject_reason: str


def parse_orders(body: dict[str, Any]) -> list[OrderRecord]:
    """국내 주문내역."""
    records = []
    for row in rows_of(body):
        code = _text(row, "occurs1_ord_sts")
        records.append(
            OrderRecord(
                order_no=_text(row, "occurs1_ord_no"),
                org_order_no=_text(row, "occurs1_org_ord_no"),
                sym=_text(row, "occurs1_sym"),
                side=SIDE_CODES.get(_text(row, "occurs1_trd_div"), ""),
                status_code=code,
                status=ORDER_STATUS.get(code, "unknown"),
                status_name=_text(row, "occurs1_ord_sts_nm"),
                qty=to_dec(row.get("occurs1_ord_qty")),
                exec_qty=to_dec(row.get("occurs1_exec_qty")),
                remaining_qty=to_dec(row.get("occurs1_rem_ord_qty")),
                cancelled_qty=to_dec(row.get("occurs1_cxl_qty")),
                price=to_dec(row.get("occurs1_ord_pric")),
                exec_price=to_dec(row.get("occurs1_exec_pric")),
                trade_date=_text(row, "occurs1_trd_dt"),
                recv_time=_text(row, "occurs1_exch_rcv_tm"),
                exec_time=_text(row, "occurs1_exch_exec_tm"),
                exec_no=_text(row, "occurs1_exch_exec_no"),
                reject_reason=_text(row, "occurs1_ord_rejt_rsn"),
            )
        )
    return records


@dataclass(frozen=True)
class OrderAck:
    order_no: str
    msg_cd: str
    msg: str


def parse_order_ack(body: dict[str, Any]) -> OrderAck:
    """국내 주문·정정·취소 응답. 성공이면 `ord_no` 가 있고 `msg_cd`·`msg_cntn` 은 빈 문자열입니다."""
    return OrderAck(
        order_no=_text(body, "ord_no"),
        msg_cd=_text(body, "msg_cd"),
        msg=_text(body, "msg_cntn"),
    )
