"""하루 한 번의 장 막판 매매를 진행하는 상태머신 (Trader).

시장·주문·시계를 직접 모르고 `Feed`(시세)·`Broker`(주문)·`clock`·`sleep` 만 받는다 →
같은 흐름을 [자체 시뮬레이션]·[모의계좌 주문]·테스트에서 그대로 쓴다.

흐름 (모든 시각·값은 strategy.json):
    시작 대기 → 전일 종가 확인 → decision_time 판단(최근가·등락률) → entry_time 시장가 진입 (2026-11-11 부터 15:18:55 / 15:19:00)
    → 보유 중 손절 감시(15:34:55 까지, 손절폭 stop_pt) → 15:35:30 종가 단일가 청산 주문
    → 거부되면 15:44:00 재시도 → 종료 시 미체결 취소·잔고 확인·기록 마감

안전 원칙 (CLAUDE.md §4-2):
    - `unknown`(응답 없음) 주문은 그대로 다시 내지 않는다. 먼저 **잔고로 대사**한다 (quant-team P0, 2026-11-05):
        진입 결과 모름 → 잔고에 보유가 생겼으면 체결로 보고 손절 감시·청산을 이어 간다. 없으면 미체결 취소 후 끝.
        청산 결과 모름 → 잔고가 0 이면 청산된 것. 아직 보유면 미체결 취소 → 남은 수량만 15:34:55 전까지 다시 낸다.
        잔고가 예상과 다른 방향·수량이면 아무것도 내지 않고 경보(사람이 확인).
    - 거부(rejected)만 한정 횟수 다시 낸다.
    - 보유 중 시세가 price_max_age_s(5초) 넘게 끊기면 REST 현재가로 손절을 감시하고, 그것도 안 되면 바로 청산한다.
    - 끝날 때는 결과 모름이 있었어도 **항상 잔고를 확인**하고, 0 이 아니면 경보를 남긴다.
    - 킬 스위치(STOP 파일/Ctrl+C) → 보유가 있으면 청산 → 미체결 취소 → 기록 마감.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Any, Callable, Protocol

from nhapi import Journal, RiskManager, shutdown_sequence
from nhapi.timeutil import now_kst

import strategy_core as core
from strategy_core import StrategyConfig

MAX_EXIT_ATTEMPTS = 3
RECONCILE_TRIES = 4          # 잔고 대사: 1초 간격으로 몇 번 볼지 (체결이 잔고에 늦게 뜰 수 있다)
REST_RETRY_S = 1.0           # 시세가 끊겼을 때 REST 현재가를 다시 물어보는 간격
# 연결 데우기(2026-11-27 사용자 "무조건 해야"): httpx 는 5초 넘게 안 쓴 연결을 닫아, 그 뒤 첫 주문이 연결을 새로 맺느라
# 늦었다(11/9~26 모의 주문 39건: 직전 통신 뒤 5초 넘게 쉰 주문 왕복 0.21~0.44초, 5초 안 0.10초). 판단·진입 직전과 보유 중
# 가벼운 현재가 조회로 연결을 살려 둔다. 주문 시각 1초 안쪽에선 데우지 않아 조회가 주문을 늦추지 않게 한다.
WARM_EVERY_S = 2.5           # 데우기 간격 (5초보다 충분히 짧게)
WARM_QUIET_S = 1.0           # 주문 시각까지 이보다 적게 남았으면 데우지 않는다
WARM_LEAD_S = 10.0           # 판단·진입 시각 몇 초 전부터 데울지


@dataclass(frozen=True)
class Snapshot:
    """시세 한 컷. `last_ts` 는 마지막 체결을 받은 시각."""

    ts: datetime
    last: Decimal | None
    last_ts: datetime | None
    bid: Decimal | None
    ask: Decimal | None


@dataclass
class ExecResult:
    """주문 한 번의 결과. 체결로 확인된 수량·가격만 `filled_qty`·`price` 에 넣는다."""

    status: str  # filled / partial / accepted(접수만, 체결 미확인) / rejected / unknown
    filled_qty: int
    price: Decimal | None
    order_no: str = ""
    code: str = ""
    message: str = ""
    source: str = ""  # sim / paper-account


class Feed(Protocol):
    def snapshot(self) -> Snapshot: ...
    def rest_last(self) -> Decimal | None: ...
    def prev_close(self) -> Decimal | None: ...


class Broker(Protocol):
    source: str

    def net_position(self) -> int: ...
    def market_order(self, side: str, qty: int, purpose: str, *, wait_until: datetime | None = None) -> ExecResult: ...
    def cancel_open(self) -> None: ...


class Trader:
    def __init__(
        self,
        cfg: StrategyConfig,
        feed: Feed,
        broker: Broker,
        journal: Journal,
        risk: RiskManager,
        *,
        clock: Callable[[], datetime] = now_kst,
        sleep: Callable[[float], None] = _time.sleep,
        realized_pnl: Decimal = Decimal(0),
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg, self.feed, self.broker, self.j, self.risk = cfg, feed, broker, journal, risk
        self.clock, self.sleep = clock, sleep
        self.realized_pnl = realized_pnl
        self.alert = alert or (lambda text: None)   # 텔레그램 등. 실패해도 매매를 멈추지 않게 부르는 쪽이 감싼다
        self.side: str | None = None
        self.qty_held = 0
        self.entry_price: Decimal | None = None
        self.entry_estimated = False      # 결과 모름 → 잔고로 체결 확인, 가격은 그때 시세로 추정
        self.exit_estimated = False
        self._orders_sent = False
        self._unresolved = False
        self._exited = False
        self._stale_since: datetime | None = None
        self._last_rest_try: datetime | None = None
        self._rest_price: Decimal | None = None
        self._last_warm: datetime | None = None
        self.result: dict[str, Any] = {"outcome": "not_run"}

    def _alarm(self, category: str, msg: str, action: str) -> None:
        self.j.error(category, msg=msg, action=action)
        try:
            self.alert(f"[kq150] {category}: {msg}")
        except Exception:  # noqa: BLE001 — 알림 실패는 매매를 멈추지 않는다
            pass

    def _open_orders(self) -> int | None:
        """이 종목 미체결 주문 수. 브로커가 모르면 None (그땐 다시 내지 않는 쪽으로 판단)."""
        fn = getattr(self.broker, "open_order_count", None)
        if fn is None:
            return None
        try:
            return int(fn())
        except Exception as exc:  # noqa: BLE001
            self.j.error("open_orders_query", msg=f"{type(exc).__name__}: {exc}", action="assume_unknown")
            return None

    def _position(self) -> int | None:
        """잔고 조회. 실패하면 두 번 더 해 보고 None."""
        for i in range(3):
            try:
                return int(self.broker.net_position())
            except Exception as exc:  # noqa: BLE001
                self.j.error("position_query", msg=f"{type(exc).__name__}: {exc}", action="retry" if i < 2 else "give_up")
                self.sleep(0.5)
        return None

    # ------------------------------------------------------------------ 시간 도우미

    def _at(self, t: time) -> datetime:
        return self.clock().replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)

    def _wait_until(self, t: time, *, warm: bool = False) -> bool:
        """그 시각까지 기다린다. 킬 스위치가 켜지면 False. warm 이면 그 시각 WARM_LEAD_S 초 전부터 연결을 데운다."""
        target = self._at(t)
        while self.clock() < target:
            if self.risk.kill.triggered():
                return False
            if warm and (target - self.clock()).total_seconds() <= WARM_LEAD_S:
                self._keep_warm(due=target)
            # 마지막 한 번은 남은 시간만큼만 잔다(데우기 0.06초로 박자가 밀려도 주문이 그 시각에 바로 나가게)
            self.sleep(min(self.cfg.poll_interval_s, max(0.001, (target - self.clock()).total_seconds())))
        return not self.risk.kill.triggered()

    def _keep_warm(self, due: datetime | None = None) -> None:
        """주문 경로의 HTTP 연결이 닫히지 않게 가벼운 조회를 한 번 한다(위 WARM_* 설명).
        시세 피드에 warm_connection 이 없으면(시험용 가짜 피드) 아무것도 안 한다. 실패해도 매매는 그대로."""
        fn = getattr(self.feed, "warm_connection", None)
        if fn is None:
            return
        now = self.clock()
        if due is not None and (due - now).total_seconds() < WARM_QUIET_S:
            return
        last = max((x for x in (self._last_warm, self._last_rest_try) if x is not None), default=None)
        if last is not None and (now - last).total_seconds() < WARM_EVERY_S:
            return
        self._last_warm = now
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — 데우기는 보조 경로
            self.j.error("warm_connection", msg=f"{type(exc).__name__}: {exc}", action="none")

    def _sleep_until(self, t: time) -> None:
        """킬 스위치와 상관없이 그 시각까지 기다린다 (동시호가 체결 대기용)."""
        target = self._at(t)
        while self.clock() < target:
            self.sleep(min(1.0, max(0.01, (target - self.clock()).total_seconds())))

    def _auction_order_pending(self) -> bool:
        """동시호가 청산 주문이 15:45 체결을 기다리는 중일 수 있는지 (걸린 주문이 있거나 확인 불가)."""
        c = self.cfg
        if c.exit_mode != "auction" or not self._orders_sent or not (self._at(c.stop_until) <= self.clock() < self._at(c.auction_fill_time)):
            return False
        n = self._open_orders()
        return n is None or n > 0

    def _done(self, outcome: str, reason: str = "", **extra: Any) -> dict[str, Any]:
        self.result = {"outcome": outcome, "reason": reason, **extra}
        return self.result

    # ------------------------------------------------------------------ 실행

    def run(self) -> dict[str, Any]:
        try:
            self._run()
        except KeyboardInterrupt:
            self.risk.kill.trigger("Ctrl+C")
            self.j.event("interrupted", "Ctrl+C 로 중단")
            self.result = {"outcome": "aborted", "reason": "kill"} if self.result["outcome"] == "not_run" else self.result
        except Exception as exc:  # noqa: BLE001 — 어떤 오류든 정리 단계는 반드시 돈다.
            self.j.error("runner", msg=f"{type(exc).__name__}: {exc}", action="shutdown")
            self.result = {"outcome": "error", "reason": f"{type(exc).__name__}"}
        finally:
            self._finish()
        return self.result

    def _run(self) -> dict[str, Any]:
        c = self.cfg
        self.j.run(
            "start", strategy=c.name, version=c.version, sym=c.sym, qty=c.qty, threshold_pct=c.threshold_pct,
            stop_pt=c.stop_pt, exit_mode=c.exit_mode, source=self.broker.source,
        )
        if not self._wait_until(c.start_not_before):
            return self._done("aborted", "kill")
        if self.clock() > self._at(c.end_time):
            return self._done("skip", "after_session")

        position = self.broker.net_position()
        prev_close = self.feed.prev_close()
        self.j.event("pre_check", "시작 점검", position=position, prev_close=prev_close)

        if not self._wait_until(c.decision_time, warm=True):
            return self._done("aborted", "kill")
        if self.clock() > self._at(c.entry_time) + _seconds(c.entry_grace_s):
            self.j.signal("none", "late_start", now=self.clock())
            return self._done("skip", "late_start")

        snap = self.feed.snapshot()
        price, price_source = self._decision_price(snap)
        values: dict[str, Any] = {
            "prev_close": prev_close, "price": price, "price_source": price_source,
            "threshold_pct": c.threshold_pct, "bid": snap.bid, "ask": snap.ask, "last_tick_ts": snap.last_ts,
        }
        if prev_close is None:
            self.j.signal("none", "no_prev_close", **values)
            return self._done("skip", "no_prev_close")
        if price is None:
            self.j.signal("none", "no_price", **values)
            return self._done("skip", "no_price")
        ret = core.day_return_pct(prev_close, price)
        values["ret_pct"] = ret
        decision = core.decide(ret, c.threshold_pct)
        if decision.side is None:
            self.j.signal("none", decision.reason, **values)
            return self._done("skip", decision.reason, ret_pct=str(ret))
        side = decision.side
        if position != 0:
            # 2026-11-12: 14:30 큰 날 매매(research/desks/kq150/regime1430/live_1430.py)가 15:00:00 에 청산한다 → 15:00 에 읽은 잔고가
            # 그 청산 전 값일 수 있어 판단 직전에 한 번 더 읽는다. 그래도 남아 있으면 진입하지 않는다(종전과 같음).
            position = self.broker.net_position()
            self.j.event("position_recheck", "판단 직전 잔고 다시 확인", position=position)
        if position != 0:
            self.j.signal("none", "leftover_position", position=position, **values)
            self.j.error("position", msg=f"시작 시 보유 {position} 이(가) 있어 진입하지 않습니다. 잔고를 확인하세요.", action="skip")
            return self._done("skip", "leftover_position")
        verdict = self.risk.can_enter(side, c.qty, position=position, realized_pnl=self.realized_pnl, one_stop_loss=c.one_stop_loss_won)
        if not verdict.allowed:
            self.j.signal("none", "risk_blocked", detail=verdict.reason, **values)
            return self._done("skip", "risk_blocked", detail=verdict.reason)
        self.j.signal(side, "enter", **values)

        if not self._wait_until(c.entry_time, warm=True):
            return self._done("aborted", "kill")
        return self._trade(side, values)

    def _decision_price(self, snap: Snapshot) -> tuple[Decimal | None, str]:
        """최근 체결가(WS)가 신선하면 그것, 아니면 REST 현재가."""
        fresh = snap.last is not None and snap.last_ts is not None and (snap.ts - snap.last_ts).total_seconds() <= self.cfg.price_max_age_s
        if fresh:
            return snap.last, "ws"
        rest = self.feed.rest_last()
        if rest is not None:
            return rest, "rest"
        return None, "none"

    # ------------------------------------------------------------------ 진입·보유·청산

    def _trade(self, side: str, values: dict[str, Any]) -> dict[str, Any]:
        c = self.cfg
        ref = self.feed.snapshot()
        res = self.broker.market_order(side, c.qty, "entry")
        self._orders_sent = True
        self._log_exec("entry", side, c.qty, res, ref_price=ref.last, bid=ref.bid, ask=ref.ask)
        if res.status in ("unknown", "accepted") and res.filled_qty == 0:
            held = self._reconcile_entry(side, c.qty)
            if held is None:
                return self._done("entry_unknown", res.code)
            if held == 0:
                return self._done("entry_not_filled", res.code)
            est = self.feed.snapshot().last or ref.last
            res = ExecResult("filled", held, est, order_no=res.order_no, code=res.code, message="잔고로 확인(가격 추정)", source=res.source)
            self.entry_estimated = True
            self.j.event("entry_reconciled", "결과 모름 → 잔고에 보유 확인, 체결로 보고 계속", qty=held, est_price=est)
        if res.filled_qty <= 0 or res.price is None:
            self.j.error("order_rejected", msg=f"진입 주문 실패: {res.code} {res.message}", action="no_position")
            return self._done("entry_failed", res.code)

        self.side, self.qty_held, self.entry_price = side, res.filled_qty, res.price
        stop = core.stop_price(side, res.price, c.stop_pt)
        self.j.event("position_open", "진입 완료", side=side, qty=res.filled_qty, entry_price=res.price, stop_price=stop, source=res.source)

        reason = self._hold(side, stop)
        exit_res = self._exit(reason)
        return self._finalize_trade(reason, exit_res, values)

    def _reconcile_entry(self, side: str, qty: int) -> int | None:
        """진입 결과 모름 → 잔고로 확인. 보유 수량(우리 방향, 0 이상)을 돌려준다. 판단 불가면 None(경보)."""
        self.j.error("order_unknown", msg="진입 주문 결과 모름 → 잔고로 확인합니다. 같은 주문을 다시 내지 않습니다.", action="reconcile")
        sign = 1 if side == "buy" else -1
        pos = None
        for _ in range(RECONCILE_TRIES):
            self.sleep(1.0)
            pos = self._position()
            if pos:
                break
        if pos == 0:
            self.broker.cancel_open()          # 접수만 되고 안 걸린 주문이 나중에 체결되지 않게
            self.sleep(1.0)
            pos = self._position()
        if pos is not None and pos * sign >= 0 and abs(pos) <= qty:
            return abs(pos)
        self._unresolved = True
        self._alarm("position_mismatch", f"진입 대사 실패: 잔고 {pos} (예상 0 또는 {sign * qty}). 사람이 주문내역·잔고를 확인하세요.", "manual_check")
        return None

    def _hold(self, side: str, stop: Decimal) -> str:
        c = self.cfg
        stop_until = self._at(c.stop_until)
        exit_at = self._at(c.exit_submit_time)
        while True:
            now = self.clock()
            if self.risk.kill.triggered():
                return "kill"
            price = self._monitor_price(now)
            if price == "stale":
                self._alarm("stale_feed", f"보유 중 시세가 {c.price_max_age_s:.0f}초 넘게 끊기고 REST 도 실패 → 바로 청산합니다.", "exit")
                return "stale"
            if now < stop_until and price is not None and core.stop_hit(side, price, stop):
                self.j.event("stop_hit", "손절선 도달", price=price, stop_price=stop)
                return "stop"
            if now >= exit_at:
                return "exit"
            self._keep_warm(due=exit_at)          # 손절 주문도 바로 나가게 (정해진 청산 1초 전부터는 쉼)
            self.sleep(min(c.poll_interval_s, max(0.001, (exit_at - self.clock()).total_seconds())))

    def _monitor_price(self, now: datetime) -> Decimal | str | None:
        """손절 감시용 가격. WS 체결이 신선하면 그것, 끊겼으면 REST(1초에 한 번), 둘 다 안 되면 'stale'."""
        snap = self.feed.snapshot()
        age = (now - snap.last_ts).total_seconds() if snap.last_ts is not None else float("inf")
        if snap.last is not None and age <= self.cfg.price_max_age_s:
            self._stale_since = None
            return snap.last
        if self._stale_since is None:
            self._stale_since = now
            self.j.error("stale_feed", msg=f"WS 체결이 {age:.1f}초째 없습니다. REST 현재가로 감시합니다.", action="rest_fallback")
        if self._last_rest_try is None or (now - self._last_rest_try).total_seconds() >= REST_RETRY_S:
            self._last_rest_try = now
            self._rest_price = self.feed.rest_last()
            if self._rest_price is None:
                return "stale"
        return self._rest_price

    def _exit(self, purpose: str) -> ExecResult | None:
        c = self.cfg
        side = core.opposite(self.side or "")
        remaining, fills = self.qty_held, []
        attempts = 0
        continuous = c.exit_mode == "continuous"
        while remaining > 0 and attempts < MAX_EXIT_ATTEMPTS and self.clock() <= self._at(c.end_time):
            if not continuous and self.clock() >= self._at(c.auction_fill_time):
                # 주간장 종가가 끝난 뒤의 주문은 내지 않는다 (야간장으로 넘어가 엉뚱한 시각에 체결될 수 있다)
                self._alarm("order_window", "15:45 종가 뒤라 청산 주문을 내지 않습니다. 야간장(18:00) 또는 다음 영업일에 수동 청산이 필요합니다.", "manual_check")
                self._unresolved = True
                break
            if continuous and self.clock() >= self._at(c.stop_until):
                # 15:35 이후 주문은 모의 서버에서 '미접수'로 남고 취소도 안 된다(2026-10-28 프로브). 내지 않는다.
                self.j.error("order_window", msg="연속매매 마감(15:35) 직전이라 청산 주문을 내지 않습니다. 다음 영업일 09:00 이후 수동 청산이 필요합니다.", action="manual_check")
                self._unresolved = True
                break
            attempts += 1
            wait_until = self._at(c.auction_fill_time) if (purpose == "exit" and c.exit_mode == "auction") else None
            res = self.broker.market_order(side, remaining, purpose, wait_until=wait_until)
            self._orders_sent = True
            self._log_exec(purpose, side, remaining, res)
            if res.filled_qty > 0:
                # 체결 수량이 있으면 가격이 비어도 체결로 센다 (미체결로 보고 다시 내면 반대 포지션이 생긴다)
                px = res.price if res.price is not None else (self.feed.snapshot().last or self.entry_price or Decimal(0))
                if res.price is None:
                    self.exit_estimated = True
                fills.append((res.filled_qty, px))
                remaining -= res.filled_qty
            if remaining <= 0:
                break
            if res.status in ("unknown", "accepted"):
                left = self._reconcile_exit(remaining)
                if left is None:
                    break
                if left < remaining:
                    est = self.feed.snapshot().last
                    fills.append((remaining - left, est if est is not None else (self.entry_price or Decimal(0))))
                    self.exit_estimated = True
                    remaining = left
                if remaining <= 0:
                    self.j.event("exit_reconciled", "결과 모름 → 잔고 0 확인, 청산된 것으로 봄(가격 추정)")
                    break
                self.j.error("order_unknown", msg=f"청산 결과 모름 → 잔고에 {remaining} 남음, 미체결 취소 후 남은 수량만 다시 냅니다.", action="resend")
                continue
            self.j.error("order_rejected", msg=f"청산 주문 실패({attempts}/{MAX_EXIT_ATTEMPTS}): {res.code} {res.message}", action="retry")
            if purpose == "exit" and not continuous and self.clock() < self._at(c.exit_retry_time):
                self._wait_until(c.exit_retry_time)
            else:
                self.sleep(1.0)
        self._exited = remaining <= 0
        if not fills:
            return None
        qty = sum(q for q, _ in fills)
        avg = sum(q * p for q, p in fills) / qty
        return ExecResult("filled" if remaining <= 0 else "partial", qty, avg, source=self.broker.source)

    def _reconcile_exit(self, remaining: int) -> int | None:
        """청산 결과 모름 → 잔고로 확인. 아직 남은 수량(0 이상)을 돌려준다. 이상하면 None(경보, 더 내지 않음)."""
        sign = 1 if self.side == "buy" else -1
        c = self.cfg
        self.sleep(1.0)
        pos = self._position()
        now = self.clock()
        auction_pending = c.exit_mode == "auction" and self._at(c.stop_until) <= now < self._at(c.auction_fill_time)
        if pos is not None and pos * sign > 0 and auction_pending:
            # 종가 동시호가: 15:45 전엔 체결 전이라 잔고가 그대로인 게 정상. 걸린 주문이 있거나 모르면 다시 내지 말고 15:45 까지 기다린다
            n = self._open_orders()
            if n is None or n > 0:
                self.j.event("auction_wait", "동시호가 청산 주문이 걸려 있거나 확인 불가 → 15:45 체결을 기다린 뒤 잔고 확인", open_orders=n)
                self._sleep_until(c.auction_fill_time)      # 킬 스위치로도 끊지 않는다 (끊으면 같은 매도를 또 낸다)
                self.sleep(30.0)
                pos = self._position()
        elif pos is not None and pos * sign > 0:
            self.broker.cancel_open()          # 걸려 있는 청산 주문이 나중에 체결돼 반대 포지션이 생기지 않게
            self.sleep(0.5)
            pos = self._position()
        if pos is not None and pos * sign >= 0 and abs(pos) <= remaining:
            return abs(pos)
        self._unresolved = True
        self._alarm("position_mismatch", f"청산 대사 실패: 잔고 {pos} (남은 예상 {sign * remaining}). 더 내지 않습니다. 사람이 확인하세요.", "manual_check")
        return None

    def _finalize_trade(self, purpose: str, exit_res: ExecResult | None, values: dict[str, Any]) -> dict[str, Any]:
        c = self.cfg
        reason = "scheduled" if purpose == "exit" else purpose
        if exit_res is None or not self._exited:
            return self._done("exit_failed", reason, side=self.side)
        gross = core.pnl_pt(self.side or "", self.entry_price or Decimal(0), exit_res.price or Decimal(0))
        net = gross - c.cost_pt
        self.j.trade(
            side=self.side, qty=self.qty_held, entry_price=self.entry_price, exit_price=exit_res.price,
            exit_reason=reason, gross_pt=gross, assumed_cost_pt=c.cost_pt, net_pt=net,
            net_won=core.pnl_won(net, self.qty_held, c.multiplier), ret_pct=values.get("ret_pct"), source=exit_res.source,
            entry_estimated=self.entry_estimated, exit_estimated=self.exit_estimated,
            note="net 은 설정의 가정 비용(cost_pt_roundtrip)을 뺀 값. 실제 수수료는 예수금 조회로 따로 확인. *_estimated=True 면 그 가격은 잔고 대사 시점 시세 추정",
        )
        return self._done("traded", "", exit_reason=reason, gross_pt=str(gross), net_pt=str(net),
                          estimated=self.entry_estimated or self.exit_estimated)

    # ------------------------------------------------------------------ 기록·종료

    def _log_exec(self, purpose: str, side: str, qty: int, res: ExecResult, **extra: Any) -> None:
        self.j.order(
            "result", side=side, qty=qty, purpose=purpose, status=res.status, order_no=res.order_no,
            reject_code=res.code, msg=res.message, **extra,
        )
        if res.filled_qty > 0:
            self.j.fill(res.source or self.broker.source, side=side, qty=res.filled_qty, price=res.price, purpose=purpose, order_no=res.order_no)

    def _finish(self) -> None:
        auction_hold = False
        try:
            auction_hold = self._auction_order_pending()
        except Exception:  # noqa: BLE001
            auction_hold = True

        def cancel_all() -> None:
            if self._orders_sent and not auction_hold:   # 동시호가 청산 주문은 취소하지 않는다 (취소 실패 + 재주문 = 두 번 매도)
                try:
                    self.broker.cancel_open()
                except Exception as exc:  # noqa: BLE001
                    self.j.error("cancel_open", msg=f"{type(exc).__name__}: {exc}", action="continue")

        def flatten() -> None:
            if auction_hold:
                self.j.event("auction_wait", "종료 정리: 동시호가 청산 주문이 걸려 있어 15:45 체결까지 기다립니다 (새 주문 없음)")
                self._sleep_until(self.cfg.auction_fill_time)
                self.sleep(30.0)
            elif self._unresolved:
                self.j.error("shutdown", msg="대사로 풀지 못한 주문이 있어 자동 청산을 하지 않습니다. 잔고를 직접 확인하세요.", action="manual_check")
            else:
                pos = self._position()
                ours = pos and self.side is not None and (pos > 0) == (self.side == "buy") and abs(pos) <= self.qty_held
                if ours and self.entry_price is not None and not self._exited:   # 우리가 연 방향·수량 안에서만 정리한다
                    self.qty_held = abs(pos)
                    self._exit("shutdown")
                elif pos and self.entry_price is not None:
                    self._alarm("position_mismatch", f"종료 정리: 잔고 {pos} 가 우리가 연 포지션({self.side} {self.qty_held})과 다릅니다. 자동으로 건드리지 않습니다.", "manual_check")
            # 결과 모름이 있었어도 마지막 잔고는 반드시 본다 (15:35 보유 0 확인)
            final = self._position() if self._orders_sent else 0
            self.j.event("final_position", "종료 시 보유", position=final)
            self.result["final_position"] = final
            if final != 0:
                self._alarm("position", f"종료 시 보유 {final} (None=조회 실패) 이(가) 남았습니다. 밤을 넘기기 전에 수동 정리가 필요합니다.", "manual_check")

        shutdown_sequence(cancel_all=cancel_all, flatten=flatten, close_journal=lambda: self.j.run("end", **self.result), journal=self.j)


def _seconds(value: float):
    from datetime import timedelta
    return timedelta(seconds=value)
