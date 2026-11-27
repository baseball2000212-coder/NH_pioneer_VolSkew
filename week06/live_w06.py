"""6주차: 기존 LiveFeed 에 10단계 호가(KB)와 계좌 주문/체결 실시간 알림(O1_K)을 더한 시세.

- KB: 최신 10단계 호가를 들고 있다가 조각을 보낼 때 `book()` 으로 넘긴다 → 그 순간 호가를 쓸었을 때의 그림자 체결가 기록
  (모의서버는 시장가를 호가와 상관없이 바로 체결하므로, 실제 시장이었으면 얼마에 됐을지를 따로 남긴다).
- O1_K: 주문 접수·체결 알림을 받은 그대로(계좌번호류 칸은 빼고) 기록 → '실시간 체결 알림' 로그. 매매 판단에는 쓰지 않는다
  (판단은 주문내역·잔고 조회로만 — 알림 형식이 처음 쓰는 것이라 기록만 한다).
"""
from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from typing import Any

from nhapi.masking import mask
from nhapi.timeutil import iso_ms
from nhapi.ws import Feed as WsFeed
from nhapi.ws import StreamClient

from live import LiveFeed, _dec

_HIDE = ("acnt", "acct", "acc", "acno", "ac_no", "pwd", "token", "key", "cust", "user", "usr", "ip", "mac")


class LiveFeedW06(LiveFeed):
    def __init__(self, *args: Any, order_notices: bool = True, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._book: dict[str, Any] | None = None
        self._book_ts = None
        self.order_notices = order_notices
        self.notice_count = 0

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
                await self._stream.subscribe(WsFeed("KB", self.cfg.sym))
                if self.order_notices:
                    try:
                        await self._stream.subscribe(WsFeed("O1_K"))
                    except Exception as exc:  # noqa: BLE001 — 알림 구독 실패는 매매를 막지 않는다
                        self.j.error("ws_order_notice", msg=f"{type(exc).__name__}: {exc}", action="skip")
                ready.set()
                await self._stream.run()

            self._loop.run_until_complete(main())

        self._thread = threading.Thread(target=runner, name="ws", daemon=True)
        self._thread.start()
        ready.wait(10)

    def on_message(self, api_id: str, sym: str, body: dict[str, Any]) -> None:
        if api_id in ("KB", "KD"):
            ask, bid = [], []
            for i in range(1, 11):
                ap, aq = _dec(body.get(f"ask_pric_{i}")), _dec(body.get(f"ask_qty_{i}"))
                bp, bq = _dec(body.get(f"bid_pric_{i}")), _dec(body.get(f"bid_qty_{i}"))
                if ap and ap > 0 and aq is not None:
                    ask.append((ap, int(aq)))
                if bp and bp > 0 and bq is not None:
                    bid.append((bp, int(bq)))
            with self._lock:
                self._book, self._book_ts = {"ask": ask, "bid": bid}, self.clock()
                if ask and bid:
                    self._ask, self._bid = ask[0][0], bid[0][0]
            return
        if api_id in ("O1_K", "O1"):
            self.notice_count += 1
            safe = mask({k: v for k, v in body.items() if not any(h in k.lower() for h in _HIDE)})
            self.j.event("order_notice", "실시간 주문/체결 알림", api=api_id, recv=iso_ms(self.clock()), body=safe)
            return
        super().on_message(api_id, sym, body)

    def book(self) -> dict[str, Any] | None:
        """최신 10단계 호가 {'ask': [(가격, 잔량)...], 'bid': [...], 'age_s': 초}. 없으면 None."""
        with self._lock:
            if self._book is None or self._book_ts is None:
                return None
            return {**self._book, "age_s": (self.clock() - self._book_ts).total_seconds()}

    def depth(self, side: str, levels: int = 1) -> int | None:
        b = self.book()
        if not b:
            return None
        lv = b["ask" if side == "buy" else "bid"][:levels]
        return sum(q for _, q in lv)


def book_levels(b: dict[str, Any] | None, side: str) -> list[tuple[Decimal, int]]:
    if not b:
        return []
    return list(b["ask" if side == "buy" else "bid"])
