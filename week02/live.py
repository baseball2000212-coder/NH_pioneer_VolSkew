"""실제 NH선물 모의 서버에 붙는 부분: 시세(WS 체결 + REST 보조)와 모의계좌 주문 브로커.

- `LiveFeed`: 별도 스레드의 asyncio 루프에서 `KA`(체결, 야간 짝 `KC` 자동) 를 구독하고 최근가·최우선호가를 갱신한다.
  전일 종가는 분봉 조회(chart-minute)의 전 거래일 15:45 봉 종가를 쓴다(팀 백테스트 정의와 같다).
  현재가 조회(price)의 `ref_pric` 는 대조용으로만 기록한다.
- `NhOrderBroker`: nhapi `OrderGateway` 로 시장가 주문을 내고, **주문내역(order-history)** 에서 체결을 확인한다.
  접수는 체결이 아니다. 응답이 없으면 `unknown` 으로 두고 다시 내지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time as _time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from nhapi import Client, Journal
from nhapi.orders import OrderGateway, OrderRequest
from nhapi.parsers import parse_bars, parse_price
from nhapi.timeutil import iso_ms, now_kst, yyyymmdd
from nhapi.ws import Feed as WsFeed
from nhapi.ws import StreamClient, is_trade_tick

from runner import ExecResult, Snapshot
from strategy_core import StrategyConfig

AUCTION_GRACE_S = 120.0


def _dec(value: Any) -> Decimal | None:
    try:
        text = str(value).strip()
        return Decimal(text) if text else None
    except Exception:  # noqa: BLE001
        return None


class LiveFeed:
    def __init__(self, client: Client, cfg: StrategyConfig, journal: Journal, *, tick_path: Path | None = None,
                 clock: Callable[[], datetime] = now_kst) -> None:
        self.client, self.cfg, self.j, self.clock = client, cfg, journal, clock
        self._lock = threading.Lock()
        self._last: Decimal | None = None
        self._last_ts: datetime | None = None
        self._bid: Decimal | None = None
        self._ask: Decimal | None = None
        self._ticks = 0
        self._tick_file = tick_path.open("a", encoding="utf-8", newline="\n") if tick_path else None
        self._stream: StreamClient | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ WS

    def start(self) -> None:
        ready = threading.Event()

        def runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stream = StreamClient(
                self.client.config, self.client.tokens, "domestic",
                on_message=self.on_message, on_state=self._on_state, journal=self.j,
            )

            async def main() -> None:
                await self._stream.subscribe(WsFeed("KA", self.cfg.sym))
                ready.set()
                await self._stream.run()

            self._loop.run_until_complete(main())

        self._thread = threading.Thread(target=runner, name="ws", daemon=True)
        self._thread.start()
        ready.wait(10)

    def stop(self) -> None:
        if self._stream is not None and self._loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._stream.stop(), self._loop)
            try:
                fut.result(10)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(5)
        if self._tick_file:
            self._tick_file.close()

    def _on_state(self, state: str, detail: str) -> None:
        self.j.event("ws_state", detail, state=state)

    def on_message(self, api_id: str, sym: str, body: dict[str, Any]) -> None:
        if api_id not in ("KA", "KC") or not is_trade_tick(api_id, body):
            return
        price = _dec(body.get("last_pric"))
        if price is None:
            return
        now = self.clock()
        with self._lock:
            self._last, self._last_ts = price, now
            bid, ask = _dec(body.get("bid_best_pric")), _dec(body.get("ask_best_pric"))
            if bid is not None and bid > 0:
                self._bid = bid
            if ask is not None and ask > 0:
                self._ask = ask
            self._ticks += 1
        if self._tick_file:
            self._tick_file.write(json.dumps({
                "ts": iso_ms(now), "api": api_id, "exch_tm": body.get("exch_tm"), "price": body.get("last_pric"),
                "qty": body.get("exec_qty"), "exec_div": body.get("exec_div"), "bid": body.get("bid_best_pric"),
                "ask": body.get("ask_best_pric"), "theo_pric": body.get("theo_pric"), "dev": body.get("dev"),
            }, ensure_ascii=False) + "\n")
            self._tick_file.flush()

    @property
    def tick_count(self) -> int:
        return self._ticks

    # ------------------------------------------------------------------ Feed 규약

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(ts=self.clock(), last=self._last, last_ts=self._last_ts, bid=self._bid, ask=self._ask)

    def rest_last(self) -> Decimal | None:
        try:
            quote = parse_price(self.client.query("domestic_price", {"sym": self.cfg.sym}).body)
        except Exception as exc:  # noqa: BLE001 — 보조 경로라 실패는 기록만 하고 None.
            self.j.error("rest_price", msg=f"{type(exc).__name__}: {exc}", action="none")
            return None
        return quote.last

    def _minute_bars(self, start: datetime, end: datetime) -> list:
        payload = {
            "sym": self.cfg.sym, "inq_strt_dt": yyyymmdd(start), "inq_ed_dt": yyyymmdd(end), "strt_tm": "000000",
            "ed_tm": "235959", "dcnt": "1", "nght_tp": "1", "cidx_yn": "0", "fut_cidx_exp_mod": "0",
            "nxt_key": "99999999", "req_qty": "300",
        }
        return parse_bars(self.client.query("domestic_chart-minute", payload).body)

    def prev_close(self) -> Decimal | None:
        """전 거래일 15:45 봉 종가(종가 단일가). 현재가 조회의 `ref_pric` 와 견줘 기록한다."""
        today = self.clock()
        try:
            bars = [b for b in self._minute_bars(today - timedelta(days=10), today) if b.ts and b.ts.date() < today.date()]
        except Exception as exc:  # noqa: BLE001
            self.j.error("prev_close", msg=f"{type(exc).__name__}: {exc}", action="skip")
            return None
        if not bars:
            self.j.error("prev_close", msg="전 거래일 분봉이 없습니다.", action="skip")
            return None
        last_day = max(b.ts.date() for b in bars)
        day_bars = [b for b in bars if b.ts.date() == last_day]
        chart = next((b for b in day_bars if b.ts.strftime("%H%M") == "1545"), day_bars[-1])
        api_ref = None
        try:
            api_ref = parse_price(self.client.query("domestic_price", {"sym": self.cfg.sym}).body).prev_close
        except Exception:  # noqa: BLE001
            pass
        self.j.event(
            "prev_close_check", "전일 종가 출처 대조", chart_date=last_day.isoformat(), chart_bar=chart.ts.strftime("%H:%M"),
            chart_close=chart.close, price_api_ref=api_ref,
            differ=(api_ref is not None and chart.close is not None and api_ref != chart.close),
        )
        return chart.close

    def ref_bars(self) -> dict[str, Any]:
        """사후 분석용: 오늘 15:19·15:20·15:34·15:45 봉(백테스트 정의와 같은 가격)."""
        today = self.clock()
        out: dict[str, Any] = {}
        try:
            for b in self._minute_bars(today, today):
                key = b.ts.strftime("%H%M")
                if key in ("1519", "1520", "1534", "1545"):
                    out[key] = {"open": b.open, "high": b.high, "low": b.low, "close": b.close}
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
        self.j.event("ref_bars", "오늘 기준 봉(분봉 조회)", **{"bars": out})
        return out


class NhOrderBroker:
    """모의계좌 시장가 주문. 체결은 주문내역으로 확인한 것만 체결로 쓴다."""

    source = "paper-account"

    def __init__(self, gateway: OrderGateway, cfg: StrategyConfig, journal: Journal, *,
                 clock: Callable[[], datetime] = now_kst, sleep: Callable[[float], None] = _time.sleep) -> None:
        self.gw, self.cfg, self.j, self.clock, self.sleep = gateway, cfg, journal, clock, sleep

    def net_position(self) -> int:
        total = Decimal(0)
        for p in self.gw.positions():
            if p.sym == self.cfg.sym:
                total += p.qty if p.side == "buy" else -p.qty
        return int(total)

    def cancel_open(self) -> None:
        for record in self.gw.open_orders():
            if record.sym != self.cfg.sym:
                continue
            qty = int(record.remaining_qty or record.qty or 0)
            if qty <= 0:
                continue
            out = self.gw.cancel(record.sym, record.side, qty, record.order_no)
            self.j.event("cancel_open", "미체결 취소", order_no=record.order_no, status=out.status, code=out.code, msg=out.message)

    def market_order(self, side: str, qty: int, purpose: str, *, wait_until: datetime | None = None) -> ExecResult:
        request = OrderRequest(
            sym=self.cfg.sym, side=side, qty=qty, ord_type="market", exec_cond="FAS", session="day", tick=self.cfg.tick,
        )
        out = self.gw.place(request)
        for _ in range(3):
            if out.status != "unknown":
                break
            self.sleep(1.0)
            out = self.gw.resolve_unknown(out)
        if out.status == "rejected":
            return ExecResult("rejected", 0, None, code=out.code, message=out.message, source=self.source)
        if out.status == "unknown":
            return ExecResult("unknown", 0, None, code=out.code, message=out.message, source=self.source)

        max_wait = self.cfg.fill_poll_max_s
        if wait_until is not None:
            max_wait = max((wait_until - self.clock()).total_seconds(), 0) + AUCTION_GRACE_S
        qty_done, price, rows = self._confirm(out.order_no, qty, max_wait)
        self.j.event("order_history_rows", "주문내역 확인", order_no=out.order_no, rows=rows)
        if qty_done >= qty:
            return ExecResult("filled", qty_done, price, order_no=out.order_no, source=self.source)
        if qty_done > 0:
            return ExecResult("partial", qty_done, price, order_no=out.order_no, source=self.source)
        rejected = next((r for r in rows if r.get("reject_reason")), None)
        if rejected:
            return ExecResult("rejected", 0, None, order_no=out.order_no, code="HISTORY_REJECT", message=str(rejected["reject_reason"]), source=self.source)
        return ExecResult("accepted", 0, None, order_no=out.order_no, message="접수됨, 체결 미확인", source=self.source)

    def _confirm(self, order_no: str, qty: int, max_wait: float) -> tuple[int, Decimal | None, list[dict[str, Any]]]:
        """주문내역을 간격을 두고 조회해 이 주문의 체결 수량·평균가를 구한다."""
        started = self.clock()
        while True:
            records = [r for r in self.gw.history() if r.order_no == order_no or r.org_order_no == order_no]
            done, price = _executions(records)
            rows = [
                {"status": r.status_name, "qty": r.qty, "exec_qty": r.exec_qty, "exec_price": r.exec_price, "exec_no": r.exec_no,
                 "reject_reason": r.reject_reason, "recv_time": r.recv_time, "exec_time": r.exec_time}
                for r in records
            ]
            if done >= qty or (self.clock() - started).total_seconds() >= max_wait or any(r["reject_reason"] for r in rows):
                return done, price, rows
            self.sleep(self.cfg.fill_poll_interval_s)


def _executions(records: list) -> tuple[int, Decimal | None]:
    """주문내역 줄에서 체결 수량과 가중평균가. 체결번호가 다른 줄은 합치고, 같으면 한 번만 센다."""
    seen: dict[str, tuple[Decimal, Decimal]] = {}
    for i, r in enumerate(records):
        if r.exec_qty and r.exec_qty > 0 and r.exec_price and r.exec_price > 0:
            seen[r.exec_no.strip("0") and r.exec_no or f"row{i}"] = (r.exec_qty, r.exec_price)
    if not seen:
        return 0, None
    total = sum(q for q, _ in seen.values())
    return int(total), sum(q * p for q, p in seen.values()) / total
