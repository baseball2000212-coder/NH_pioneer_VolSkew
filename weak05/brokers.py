# 출처: 4주차 submission/brokers.py (사용자 제공 초안 nh_w04_engine_20261111.zip 의 broker.py 에서 유래). 5주차: 옵션 전용 부분 제거, Q 를 여기로 이동, 지정가 체결 모의 추가.
"""브로커 두 가지: 엔진은 quote / place / fill / cancel / resolve / positions / open_orders 만 쓴다.

- `NHBroker`  NH선물 해외 **모의계좌**(실제 주문). nhapi 의 Client·OverseasGateway 를 감싼다.   → 체결 출처 mock-account
- `SimBroker` 프로그램 안의 **가상 체결**(자체 시뮬레이션). 시세는 NH 모의 서버에서 받는다.      → 체결 출처 paper-sim
  체결 규칙(보수적): 시장가·건너는 지정가는 맞은편 호가에 전량. 안 건너는 지정가는 이후 호가가 **지정가를 넘어서야**(매수: ask ≤ 지정가) 체결 — 대기열은 모른다고 보고 터치만으론 체결하지 않음.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any


@dataclass
class Q:
    """최우선 호가 한 장."""

    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    ts: datetime | None = None

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def ok(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass
class Ack:
    status: str                     # accepted / rejected / unknown
    order_no: str = ""
    code: str = ""
    message: str = ""
    raw: Any = None


@dataclass
class Fill:
    qty: int
    price: float | None
    remaining: int | None = None
    status: str = ""


@dataclass
class OpenOrder:
    sym: str
    side: str
    qty: int
    order_no: str


class NHBroker:
    """NH선물 해외 모의계좌. `client` 는 nhapi.Client, `gateway` 는 OverseasGateway."""

    def __init__(self, client: Any, gateway: Any, *, clock: Callable[[], datetime] = datetime.now,
                 on_error: Callable[[str, str], None] | None = None) -> None:
        self.client, self.gw, self._clock, self._on_error = client, gateway, clock, on_error

    def _warn(self, where: str, exc: Exception) -> None:
        if self._on_error is not None:
            self._on_error(where, f"{type(exc).__name__}: {str(exc)[:160]}")

    def quote(self, sym: str) -> Q | None:
        try:
            b = self.client.query("overseas_bid-price", {"exch_cd": "FCME", "sym": sym}, retries=1).body
            ts = None
            rcv = str(b.get("qt_rcv_tm") or "")
            if len(rcv) >= 6 and rcv[:6].isdigit():
                ts = self._clock().replace(hour=int(rcv[:2]) % 24, minute=int(rcv[2:4]), second=int(rcv[4:6]), microsecond=0)
            return Q(float(b.get("bid_pric_1") or 0), float(b.get("ask_pric_1") or 0),
                     float(b.get("bid_qty_1") or 0), float(b.get("ask_qty_1") or 0), ts)
        except Exception as exc:  # noqa: BLE001 — 시세 한 번 실패는 다음 주기에 다시 본다
            self._warn(f"quote {sym}", exc)
            return None

    def minute_rows(self, sym: str) -> list[dict[str, str]]:
        """가장 최근 1분봉 한 쪽(오래된 순). 조회 API 라 재시도는 Client.query 가 한다."""
        from nhapi.history import dedup_rows, slim_row
        day = self._clock().strftime("%Y%m%d")
        body = self.client.query("overseas_chart-minute", {
            "exch_cd": "FCME", "sym": sym, "inq_strt_dt": (self._clock() - timedelta(days=1)).strftime("%Y%m%d"), "strt_tm": "000000",
            "inq_ed_dt": day, "ed_tm": "235959", "req_qty": "300", "dcnt": "1", "cidx_yn": "0", "nxt_key": "99999999"}).body
        rows, _ = dedup_rows(slim_row(r) for r in body.get("Occurs1", []))
        return rows

    def place(self, sym: str, side: str, qty: int, price: float | None = None, reduce_only: bool = False) -> Ack:
        from nhapi.overseas import OverseasOrderRequest
        req = OverseasOrderRequest(sym, side, int(qty), "market" if price is None else "limit",
                                   price=None if price is None else Decimal(str(price)), reduce_only=reduce_only)
        out = self.gw.place(req)
        return Ack(out.status, out.order_no, out.code, out.message, raw=out)

    def fill(self, order_no: str) -> Fill | None:
        try:
            r = self.gw.fills_of(order_no)
        except Exception as exc:  # noqa: BLE001
            self._warn("fills_of", exc)
            return None
        if r is None:
            return None
        return Fill(int(r.exec_qty or 0), float(r.exec_price) if r.exec_price else None,
                    None if r.remaining_qty is None else int(r.remaining_qty), r.status)

    def cancel(self, sym: str, side: str, qty: int, order_no: str) -> Ack:
        out = self.gw.cancel(sym, side, int(qty), order_no)
        return Ack(out.status, out.order_no, out.code, out.message, raw=out)

    def resolve(self, ack: Ack) -> Ack:
        if ack.status != "unknown" or ack.raw is None:
            return ack
        out = self.gw.resolve_unknown(ack.raw)
        return Ack(out.status, out.order_no, out.code, out.message, raw=out)

    def positions(self) -> dict[str, int]:
        pos: dict[str, int] = {}
        for p in self.gw.positions():
            pos[p.sym] = pos.get(p.sym, 0) + int(p.qty) * (1 if p.side == "buy" else -1)
        return {s: q for s, q in pos.items() if q != 0}

    def open_orders(self) -> list[OpenOrder]:
        return [OpenOrder(r.sym, r.side, int(r.remaining_qty or r.qty or 0), r.order_no) for r in self.gw.open_orders()]


class SimBroker:
    """가상 체결 브로커. 시세는 `quote_fn(sym)` 에서 받는다(시험: 가짜, 실행: NH 모의 시세)."""

    def __init__(self, quote_fn: Callable[[str], Q | None], minute_fn: Callable[[str], list[dict[str, str]]] | None = None) -> None:
        self.quote_fn, self.minute_fn = quote_fn, minute_fn
        self.pos: dict[str, int] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.reject: dict[str, str] = {}
        self.unknown_next = 0
        self._n = 0

    def quote(self, sym: str) -> Q | None:
        return self.quote_fn(sym)

    def minute_rows(self, sym: str) -> list[dict[str, str]]:
        return self.minute_fn(sym) if self.minute_fn else []

    def _exec(self, o: dict[str, Any], qty: int, px: float) -> None:
        sign = 1 if o["side"] == "buy" else -1
        self.pos[o["sym"]] = self.pos.get(o["sym"], 0) + sign * qty
        if self.pos[o["sym"]] == 0:
            del self.pos[o["sym"]]
        o["avg"] = (o["avg"] * o["exec"] + px * qty) / (o["exec"] + qty)
        o["exec"] += qty
        o["open"] = o["exec"] < o["qty"] and o["price"] is not None
        self.trades.append({"sym": o["sym"], "side": o["side"], "qty": qty, "px": px, "no": o["no"]})

    def _try_fill(self, o: dict[str, Any]) -> None:
        if not o["open"] and o["exec"] == 0 and o["price"] is not None:
            return
        q = self.quote_fn(o["sym"])
        if q is None or not q.ok() or o["exec"] >= o["qty"]:
            return
        cross = q.ask if o["side"] == "buy" else q.bid
        if o["price"] is None:
            self._exec(o, o["qty"] - o["exec"], cross)
        elif (o["side"] == "buy" and q.ask <= o["price"]) or (o["side"] == "sell" and q.bid >= o["price"]):
            self._exec(o, o["qty"] - o["exec"], o["price"] if (o["side"] == "buy" and q.ask < o["price"]) or (o["side"] == "sell" and q.bid > o["price"]) else cross)

    def place(self, sym: str, side: str, qty: int, price: float | None = None, reduce_only: bool = False) -> Ack:
        for prefix, code in self.reject.items():
            if sym.startswith(prefix):
                return Ack("rejected", code=code, message="sim reject")
        self._n += 1
        no = f"S{self._n:09d}"
        o = {"no": no, "sym": sym, "side": side, "qty": qty, "price": price, "exec": 0, "avg": 0.0, "open": True}
        self.orders[no] = o
        if self.unknown_next > 0:
            self.unknown_next -= 1
            return Ack("unknown", code="NO_RESPONSE", raw=no)
        self._try_fill(o)
        if price is None:
            o["open"] = False
        return Ack("accepted", order_no=no)

    def fill(self, order_no: str) -> Fill | None:
        o = self.orders.get(order_no)
        if o is None:
            return None
        if o["open"]:
            self._try_fill(o)
        return Fill(o["exec"], o["avg"] if o["exec"] else None, o["qty"] - o["exec"], "filled" if o["exec"] == o["qty"] else "open")

    def cancel(self, sym: str, side: str, qty: int, order_no: str) -> Ack:
        o = self.orders.get(order_no)
        if o is None or not o["open"]:
            return Ack("rejected", code="NOOPEN", message="취소할 미체결 없음")
        o["open"] = False
        return Ack("accepted", order_no=order_no)

    def resolve(self, ack: Ack) -> Ack:
        if ack.status == "unknown" and isinstance(ack.raw, str) and ack.raw in self.orders:
            return Ack("accepted", order_no=ack.raw, code="RECONCILED")
        return ack

    def positions(self) -> dict[str, int]:
        return dict(self.pos)

    def open_orders(self) -> list[OpenOrder]:
        return [OpenOrder(o["sym"], o["side"], o["qty"] - o["exec"], o["no"]) for o in self.orders.values() if o["open"]]
