# 출처: 사용자 제공 초안 nh_w04_engine_20261111.zip 의 nh_w04/broker.py (2026-11-11). 4주차에 수정: nhapi 사용, 체결 지연(latency) 가정.
"""브로커 인터페이스와 구현 두 가지.

- `NHBroker`  NH선물 해외 모의계좌(실제 주문). `nhapi` 의 Client·OverseasGateway 를 감싼다.
- `SimBroker` 프로그램 안에서 체결을 흉내 낸다. 시험, 기록 재현, 드라이런(NH 시세 + 가상 체결)에 쓴다.

엔진은 아래 메서드만 쓴다: quote / place / fill / cancel / resolve / positions / open_orders.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from strategy_core import Q, parse_opt_sym


@dataclass
class Ack:
    status: str                     # accepted / rejected / unknown
    order_no: str = ""
    code: str = ""
    message: str = ""
    raw: Any = None


@dataclass
class Fill:
    qty: int                        # 체결 수량(절댓값)
    price: float | None
    remaining: int | None = None
    status: str = ""


@dataclass
class OpenOrder:
    sym: str
    side: str
    qty: int
    order_no: str


# ---------------------------------------------------------------- NH 모의계좌


class NHBroker:
    """NH선물 해외 모의계좌. `client` 는 nhapi.Client, `gateway` 는 OverseasGateway."""

    def __init__(self, client: Any, gateway: Any, *, clock: Callable[[], datetime] = datetime.now,
                 on_error: Callable[[str, str], None] | None = None) -> None:
        self.client, self.gw, self._clock, self._on_error = client, gateway, clock, on_error

    @staticmethod
    def _exch(sym: str) -> str:
        return "OCME" if sym.startswith("O_") else "FCME"

    def _warn(self, where: str, exc: Exception) -> None:
        if self._on_error is not None:
            self._on_error(where, f"{type(exc).__name__}: {str(exc)[:160]}")

    def quote(self, sym: str) -> Q | None:
        try:
            b = self.client.query("overseas_bid-price", {"exch_cd": self._exch(sym), "sym": sym}, retries=1).body
            ts = None
            rcv = str(b.get("qt_rcv_tm") or "")
            if len(rcv) >= 6 and rcv[:6].isdigit():
                now = self._clock()
                ts = now.replace(hour=int(rcv[:2]) % 24, minute=int(rcv[2:4]), second=int(rcv[4:6]), microsecond=0)
            return Q(float(b.get("bid_pric_1") or 0), float(b.get("ask_pric_1") or 0),
                     float(b.get("bid_qty_1") or 0), float(b.get("ask_qty_1") or 0), ts)
        except Exception as exc:  # noqa: BLE001 — 시세 한 번 실패는 다음 주기에 다시 본다
            self._warn(f"quote {sym}", exc)
            return None

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


# ---------------------------------------------------------------- 가상 체결


class SimBroker:
    """가상 체결 브로커. 시세는 `quote_fn(sym)` 에서 받는다(시험: dict, 재현: 기록, 드라이런: NH 시세).

    체결 규칙: 시장가·건너는 지정가는 맞은편 호가에 전량 체결. 옵션은 보유보다 많이 팔면 7093 거부(NH 모의와 같게).
    시험용 고장 주입: `reject`(종목 접두어 → 거부코드), `unknown_next`(다음 n번 응답 없음), `unknown_executes`,
    `limit_fills`(False 면 지정가가 안 체결됨), `max_fill`(종목 → 주문당 최대 체결 수량).
    """

    def __init__(self, quote_fn: Callable[[str], Q | None], fill_quote_fn: Callable[[str], Q | None] | None = None) -> None:
        self.quote_fn = quote_fn
        #: 체결 가격을 정하는 호가. 주지 않으면 주문 낸 순간의 호가. 백테스트는 `latency` 초 뒤의 호가를 준다(체결 지연 가정).
        self.fill_quote_fn = fill_quote_fn or quote_fn
        self.pos: dict[str, int] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.reject: dict[str, str] = {}
        self.unknown_next = 0
        self.unknown_executes = True
        self.limit_fills = True
        self.max_fill: dict[str, int] = {}
        self._n = 0

    def quote(self, sym: str) -> Q | None:
        return self.quote_fn(sym)

    def _exec(self, o: dict[str, Any], qty: int, px: float) -> None:
        sign = 1 if o["side"] == "buy" else -1
        self.pos[o["sym"]] = self.pos.get(o["sym"], 0) + sign * qty
        if self.pos[o["sym"]] == 0:
            del self.pos[o["sym"]]
        o["avg"] = (o["avg"] * o["exec"] + px * qty) / (o["exec"] + qty)
        o["exec"] += qty
        self.trades.append({"sym": o["sym"], "side": o["side"], "qty": qty, "px": px, "no": o["no"]})

    def place(self, sym: str, side: str, qty: int, price: float | None = None, reduce_only: bool = False) -> Ack:
        for prefix, code in self.reject.items():
            if sym.startswith(prefix):
                return Ack("rejected", code=code, message="sim reject")
        if parse_opt_sym(sym) and side == "sell" and qty > self.pos.get(sym, 0):
            return Ack("rejected", code="7093", message="옵션신규매도 거래가 불가능합니다.")
        self._n += 1
        no = f"S{self._n:09d}"
        o = {"no": no, "sym": sym, "side": side, "qty": qty, "price": price, "exec": 0, "avg": 0.0, "open": True}
        unknown = self.unknown_next > 0
        if unknown:
            self.unknown_next -= 1
            if not self.unknown_executes:
                return Ack("unknown", code="NO_RESPONSE", raw=None)
        self.orders[no] = o
        q = self.fill_quote_fn(sym)
        if q is not None and q.ok():
            cross = q.ask if side == "buy" else q.bid
            marketable = price is None or (price >= q.ask if side == "buy" else price <= q.bid)
            if marketable and (price is None or self.limit_fills):
                n = min(qty, self.max_fill.get(sym, qty))
                self._exec(o, n, cross)
                o["open"] = o["exec"] < qty and price is not None
        if price is None:
            o["open"] = False
        return Ack("unknown", code="NO_RESPONSE", raw=no) if unknown else Ack("accepted", order_no=no)

    def fill(self, order_no: str) -> Fill | None:
        o = self.orders.get(order_no)
        if o is None:
            return None
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
