"""[자체 시뮬레이션(종이 체결)]·[모의계좌 주문] 실행 루프 — 같은 `DayEngine` 에 실시간 분봉을 먹입니다.

흐름: 시작 대사(보유·미체결) → 분봉 백필 → 몇 초마다 최신 분봉 조회 → 엔진이 낸 의도를 실행기로 → 기록.
멈춤: `STOP` 파일 또는 Ctrl+C → 미체결 취소 → 포지션 정리 → 기록 마감 (`shutdown_sequence`).
마감: `live.end_time`(기본 23:55) 에 남은 포지션을 정리하고, 모의계좌 모드는 잔고가 0 계약인지 확인합니다.
분봉 시각은 한국시간이며, 현재 진행 중인 분의 봉은 '진행 중'으로만 엔진에 넣습니다.

안전 규칙(리뷰 반영, CHANGES.md)
- 킬 스위치·주문 중단(halted)은 **진입만** 막습니다. 청산 주문은 `reduce_only` 로 내서 막히지 않습니다.
- 종료 정리는 **계좌 잔고 기준**입니다: 미체결 취소 → 잔고 조회 → 반대 방향 시장가 → 다시 조회를
  `live.flatten_deadline`(기본 23:58)까지 간격을 늘려 가며 되풀이하고, 마지막 잔고는 **항상** 확인해 남았으면 경보합니다.
- 실행 루프는 try/finally 로 감싸 어떤 예외가 나도 종료 정리를 합니다. 정리 중의 Ctrl+C 는 무시합니다.
- 오래된 데이터: 확정봉이 `stale_entry_seconds`(120초)보다 오래됐으면 지표만 갱신하고 신규 진입은 하지 않습니다.
  보유 중에 분봉이 `stale_hold_seconds`(60초) 넘게 갱신되지 않으면 현재가(`overseas_price`)로 손절을 보고 경보합니다.
- 호출 제한(EGW020): 조회를 10 → 20 → 40초 쉽니다. 최신 분봉은 적게(`latest_req_qty`) 받고, 빈 분이 생기면 넓게 다시 받습니다.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from backtest import handle, make_engine
from executors import SimExecutor, cancel_open_orders, net_position
from feeds import ZtSeries, save_csv
from gate_store import GateStore
from nhapi.journal import Journal
from nhapi.overseas import OverseasOrderRequest, parse_overseas_bars, parse_overseas_quote
from nhapi.risk import KillSwitch, RiskLimits, RiskManager, shutdown_sequence
from sessions import KST, hm
from strategy_core import Bar

CHART_API = "overseas_chart-minute"
WIDE_REQ_QTY = 300


def make_alarm(journal: Journal, alert: Callable[[str], None] | None, sym: str) -> Callable[..., None]:
    """경보 = 기록(error 줄) + 알림 채널(`run.make_alert`: 파일·텔레그램). 알림이 실패해도 매매는 멈추지 않습니다."""

    def alarm(category: str, msg: str = "", **fields: Any) -> None:
        journal.error(category, sym, msg=msg, **fields)
        if alert is not None:
            try:
                alert(f"[MGC {sym}] {category}: {msg}")
            except Exception:  # noqa: BLE001 — 알림 실패는 무시
                pass

    return alarm


def flatten_account(gateway, sym: str, *, journal: Journal, alarm: Callable[..., None], now: Callable[[], datetime],
                    sleep: Callable[[float], None], deadline: datetime, max_attempts: int = 30,
                    backoff: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 60.0), report: dict | None = None) -> int | None:
    """계좌 잔고 기준 정리. 미체결 취소 → 잔고 조회 → 반대 방향 시장가(청산 전용, 킬 스위치에 막히지 않음) → 다시 조회.

    잔고가 0 이 되거나, `deadline` 이 지나거나, `max_attempts` 번 시도하면 멈춥니다(시도는 적어도 한 번).
    반환: 마지막으로 본 그 종목 순포지션(조회 실패·방향 모름이면 None). 0 이 아니면 부르는 쪽이 경보합니다.
    `report` 를 주면 낸 정리 주문 수를 `report["orders"]` 에 적습니다.
    """
    if report is not None:
        report["orders"] = 0
    def log(category: str, **fields: Any) -> None:
        journal.error(category, sym, **fields)

    attempt = 0
    while True:
        try:
            net = net_position(gateway.positions(), sym)
        except Exception as exc:  # noqa: BLE001
            net = None
            log("position_query", msg=f"{type(exc).__name__}: {exc}", action="retry")
        if net == 0:
            return 0
        if attempt >= max_attempts or (attempt > 0 and now() >= deadline):
            return net
        attempt += 1
        if net is not None:
            cancel_open_orders(gateway, sym, log)
            side = "sell" if net > 0 else "buy"
            try:
                outcome = gateway.place(OverseasOrderRequest(sym, side, abs(net), reduce_only=True))
                if report is not None:
                    report["orders"] += 1
                journal.event("flatten_order", f"잔고 기준 정리 {attempt}회째: {side} {abs(net)}", sym, attempt=attempt,
                              status=outcome.status, order_no=outcome.order_no, code=outcome.code)
            except Exception as exc:  # noqa: BLE001
                log("flatten_order", msg=f"{type(exc).__name__}: {exc}", attempt=attempt, action="retry")
        if attempt == 3:
            alarm("flatten_retry", f"잔고 기준 정리가 {attempt}번째입니다(잔고 {net}). {deadline:%H:%M}까지 계속 시도합니다.", action="retry")
        sleep(backoff[min(attempt - 1, len(backoff) - 1)])


def prepare_account(gateway, sym: str, *, flatten_leftover: bool, journal: Journal, alarm: Callable[..., None],
                    now: Callable[[], datetime], sleep: Callable[[float], None], deadline: datetime,
                    max_attempts: int = 5) -> tuple[bool, str]:
    """시작 대사. 깨끗하면 (True, 설명). 남은 것이 있으면 `flatten_leftover` 일 때만 이 종목을 정리하고 다시 대사합니다.

    다른 종목의 보유·미체결은 우리 것이 아닐 수 있어 자동으로 건드리지 않습니다(시작 거부).
    """
    report = gateway.startup_reconcile()
    summary = f"보유 {len(report.positions)}건, 미체결 {len(report.open_orders)}건"
    if not report.positions and not report.open_orders:
        return True, summary
    if not flatten_leftover:
        journal.error("startup_not_clean", sym, positions=len(report.positions), open_orders=len(report.open_orders))
        return False, f"기존 포지션·미체결이 있어 시작하지 않습니다({summary}). 정리한 뒤 다시 실행하거나 --flatten-leftover 를 붙이세요."
    others = sorted({p.sym for p in report.positions if p.sym != sym} | {o.sym for o in report.open_orders if o.sym != sym})
    if others:
        journal.error("startup_not_clean", sym, others=others, action="refuse")
        return False, f"다른 종목({', '.join(others)})의 보유·미체결은 자동으로 정리하지 않습니다. 직접 정리하세요."
    journal.event("flatten_leftover", f"시작 시 남은 {sym} 정리: {summary}", sym)
    cancel_open_orders(gateway, sym, lambda category, **f: journal.error(category, sym, **f))
    left = flatten_account(gateway, sym, journal=journal, alarm=alarm, now=now, sleep=sleep, deadline=deadline, max_attempts=max_attempts)
    after = gateway.startup_reconcile()
    if left == 0 and not after.positions and not after.open_orders:
        return True, f"시작 시 남은 것({summary})을 정리했습니다"
    alarm("startup_not_clean", f"시작 시 정리 실패: 잔고 {left}, 미체결 {len(after.open_orders)}건. 직접 확인하세요.", action="refuse")
    return False, "시작 시 정리에 실패해 시작하지 않습니다."


class LiveFeed:
    """NH 해외 차트(분) REST 조회. 최신 쪽부터 오래된 쪽으로 이어받습니다(연속조회 `nxt_key`)."""

    def __init__(self, client, exch: str, sym: str, zt_exch: str, zt_sym: str, backfill_pages: int = 4, latest_qty: int = 10) -> None:
        self.client, self.exch, self.sym, self.zt_exch, self.zt_sym, self.pages = client, exch, sym, zt_exch, zt_sym, backfill_pages
        self.latest_qty = latest_qty
        self._seen: dict[str, datetime] = {}      # 종목별로 지금까지 본 가장 최근 봉 시각

    @staticmethod
    def _payload(exch: str, sym: str, today: date, req_qty: int = WIDE_REQ_QTY) -> dict[str, str]:
        start = today - timedelta(days=1)
        return {"exch_cd": exch, "sym": sym, "inq_strt_dt": start.strftime("%Y%m%d"), "strt_tm": "000000",
                "inq_ed_dt": today.strftime("%Y%m%d"), "ed_tm": "235959", "req_qty": str(req_qty), "dcnt": "1", "cidx_yn": "0", "nxt_key": "99999999"}

    @staticmethod
    def _bars(body: Any) -> list[Bar]:
        return [Bar(b.ts, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume or 0)) for b in parse_overseas_bars(body)]

    def _saw(self, sym: str, bars: list[Bar]) -> None:
        if bars:
            seen = self._seen.get(sym)
            self._seen[sym] = bars[-1].ts if seen is None else max(seen, bars[-1].ts)

    def backfill(self, today: date, since: datetime) -> tuple[list[Bar], list[Bar]]:
        out = []
        for exch, sym in ((self.exch, self.sym), (self.zt_exch, self.zt_sym)):
            got: dict[datetime, Bar] = {}
            for body in self.client.paginate(CHART_API, self._payload(exch, sym, today), max_pages=self.pages):
                bars = self._bars(body)
                for b in bars:
                    got[b.ts] = b
                if bars and bars[0].ts <= since:
                    break
            out.append([got[t] for t in sorted(got) if t >= since])
            self._saw(sym, out[-1])
        return out[0], out[1]

    def latest(self, today: date) -> tuple[list[Bar], list[Bar]]:
        """최근 봉 몇 개만 받습니다(`latest_qty`). 지난번에 본 봉과 사이가 비면(조회 장애 뒤 등) 300개로 다시 받습니다.

        조회 재시도는 한 번만 합니다(호출 제한 때 한 poll 이 4번 부르지 않게). 쉼(쿨다운)은 실행 루프가 합니다.
        """
        res = []
        for exch, sym in ((self.exch, self.sym), (self.zt_exch, self.zt_sym)):
            bars = self._bars(self.client.query(CHART_API, self._payload(exch, sym, today, self.latest_qty), retries=1).body)
            seen = self._seen.get(sym)
            if self.latest_qty < WIDE_REQ_QTY and seen is not None and (not bars or bars[0].ts > seen + timedelta(minutes=1) or bars[-1].ts < seen):
                if bars and bars[-1].ts < seen:
                    # 적게 받은 결과가 최신 봉이 아님 → 이 API 는 req_qty 를 '최근 N개'로 쓰지 않는 것. 원래 크기로 되돌립니다.
                    print(f"[주의] 분봉 req_qty={self.latest_qty} 가 최신 봉을 주지 않아 {WIDE_REQ_QTY} 로 되돌립니다.")
                    self.latest_qty = WIDE_REQ_QTY
                bars = self._bars(self.client.query(CHART_API, self._payload(exch, sym, today), retries=1).body)
            self._saw(sym, bars)
            res.append(bars)
        return res[0], res[1]

    def price(self) -> float | None:
        """해외 현재가(보유 중 분봉이 끊겼을 때 손절 감시용). 조회 실패는 예외로 올립니다."""
        body = self.client.query("overseas_price", {"Iccurs1": [{"iccurs1_exch_cd": self.exch, "iccurs1_sym": self.sym}]}, retries=0).body
        quote = parse_overseas_quote(body)
        return float(quote.last) if quote.last else None


class LiveRunner:
    def __init__(self, cfg: dict[str, Any], feed, executor, store: GateStore, journal: Journal, kill: KillSwitch, *,
                 now: Callable[[], datetime] = lambda: datetime.now(KST), sleep: Callable[[float], None] = time.sleep,
                 gateway=None, mode: str = "paper-sim", bars_path=None, alarm: Callable[..., None] | None = None) -> None:
        self.cfg, self.feed, self.executor, self.store, self.journal, self.kill = cfg, feed, executor, store, journal, kill
        self.now, self.sleep, self.gateway, self.mode = now, sleep, gateway, mode
        self.sym, self.qty = cfg["strategy"]["sym"], int(cfg["strategy"]["order_qty"])
        self.live = cfg["live"]
        self.alarm = alarm or make_alarm(journal, None, self.sym)
        self.risk = RiskManager(RiskLimits.from_dict(cfg["risk"]), kill)
        self.zt = ZtSeries()
        self.last_final: datetime | None = None
        self.last_bar: Bar | None = None
        self.engine = None
        self.bars_path = bars_path
        self.bars: dict[datetime, Bar] = {}      # 그날 확정된 분봉(차트용으로 results/ 에 저장)
        self._gate_logged = False
        self._last_hb: datetime | None = None
        # 안전 설정(값은 strategy.json 의 live)
        lv = self.live
        self.stale_entry_s = float(lv.get("stale_entry_seconds", 120))
        self.stale_hold_s = float(lv.get("stale_hold_seconds", 60))
        self.cooldowns = tuple(float(x) for x in lv.get("rate_limit_cooldown_seconds", [10, 20, 40]))
        self.flatten_deadline = str(lv.get("flatten_deadline", "23:58"))
        self.flatten_attempts = int(lv.get("flatten_max_attempts", 30))
        self.flatten_backoff = tuple(float(x) for x in lv.get("flatten_backoff_seconds", [5, 10, 20, 40, 60]))
        self._fresh_at: datetime | None = None    # 최근 분봉(이번 분·직전 분)을 마지막으로 받은 시각
        self._stale_alerted = False
        self._cooldown_until: datetime | None = None
        self._rate_hits = 0
        self.final_position: int | None = None

    # ------------------------------------------------------------------ 실행
    def run(self) -> dict[str, Any]:
        reason = "시작 전 오류"
        try:
            reason = self._run()
        except KeyboardInterrupt:
            self.kill.trigger("Ctrl+C")
            reason = "킬 스위치: Ctrl+C (중단)"
        except Exception as exc:  # noqa: BLE001 — 어떤 오류든 종료 정리는 반드시 합니다.
            reason = f"오류: {type(exc).__name__}: {exc}"
            self.alarm("runner", f"실행 루프 오류로 종료 정리에 들어갑니다 — {type(exc).__name__}: {exc}", action="shutdown")
        finally:
            self._finish(reason)
        e = self.engine
        out = {"reason": reason, "trades": len(e.trades) if e else 0, "pnl": round(e.realized, 2) if e else 0.0}
        if self.gateway is not None:
            out["final_position"] = self.final_position
        return out

    def _run(self) -> str:
        today = self.now().date()
        g = self.cfg["params"]["gate"]
        thr = self.store.threshold(today, float(g["quantile"]), int(g["min_history_days"]))
        self.engine = make_engine(today, self.cfg, thr, self.zt, self.risk)
        self.journal.run("start", day=today.isoformat(), threshold=thr, mode=self.mode, history_days=len(self.store.history_before(today)))
        self._check_gate_history(today)
        midnight = datetime.combine(today, datetime.min.time(), KST)
        end_at = midnight + timedelta(minutes=hm(self.live["end_time"]))
        mgc, zt = self.feed.backfill(today, midnight + timedelta(hours=6))
        self.zt.update(zt)
        self._replay(mgc)
        self._mark_fresh(mgc)
        while True:
            if self.kill.triggered():
                return f"킬 스위치: {self.kill.reason}"
            if self.now() >= end_at:
                return "마감 시각"
            if getattr(self.executor, "halted", False):
                reason = "주문 상태 불확실 — 신규 주문 중단"
                self.alarm("order_state", reason, action="halt")
                return reason
            self.sleep(float(self.live["poll_seconds"]))
            if self._cooldown_until is not None and self.now() < self._cooldown_until:
                self._guard_stale()                # 호출 제한으로 쉬는 중: 조회는 하지 않고, 보유 중이면 경보만
                continue
            try:
                mgc, zt = self.feed.latest(today)
            except Exception as exc:  # noqa: BLE001 — 조회 실패는 기록하고 다음 주기에 다시 시도
                self._poll_failed(exc)
                self._guard_stale()
                continue
            self._rate_hits = 0
            self.zt.update(zt)
            self._feed(mgc)
            self._guard_stale()
            self._heartbeat()

    def _check_gate_history(self, today: date) -> None:
        missing = self.store.missing_weekdays(today)
        if missing:
            msg = (f"게이트 이력에 최근 평일 {len(missing)}일이 없습니다({', '.join(d.isoformat() for d in missing)}). "
                   "휴장일이 아니면 임계값이 그만큼 오래된 이력으로 계산됩니다.")
            print(f"[경고] {msg}")
            self.journal.error("gate_history_gap", self.sym, msg=msg, days=[d.isoformat() for d in missing], action="warn")

    def _poll_failed(self, exc: Exception) -> None:
        self.journal.error("poll", self.sym, msg=f"{type(exc).__name__}: {exc}", action="retry-next-poll")
        if getattr(exc, "code", "") == "EGW020" or getattr(exc, "status", None) == 429:
            wait = self.cooldowns[min(self._rate_hits, len(self.cooldowns) - 1)]
            self._rate_hits += 1
            self._cooldown_until = self.now() + timedelta(seconds=wait)
            self.journal.event("rate_limit_cooldown", f"호출 제한(EGW020) → 조회를 {wait:.0f}초 쉽니다", self.sym, wait_s=wait, hits=self._rate_hits)

    def _heartbeat(self) -> None:
        """게이트 판정은 한 번, 상태는 5분마다 기록합니다(운영 중 화면·사후 점검용)."""
        e = self.engine
        if e.gate_pass is not None and not self._gate_logged:
            self._gate_logged = True
            print(f"[게이트] {e.gate_note}")
            self.journal.event("gate", e.gate_note, self.sym, gate_pass=e.gate_pass, rv=e.rv, threshold=e.threshold)
        now = self.now()
        if self._last_hb is None or now - self._last_hb >= timedelta(minutes=5):
            self._last_hb = now
            pos = e.position
            print(f"[{now:%H:%M:%S}] 봉 {self.last_final:%H:%M} 게이트 {e.gate_pass} 포지션 {'없음' if pos is None else f'규칙{pos.rule} {pos.side:+d}@{pos.entry}'} 실현 {e.realized:+.1f}$" if self.last_final else "")
            self.journal.event("heartbeat", f"{now:%H:%M:%S} 봉 {self.last_final:%H:%M}" if self.last_final else "", self.sym,
                               last_bar=self.last_final.isoformat() if self.last_final else None, close=self.bars[self.last_final].c if self.last_final in self.bars else None, gate_pass=e.gate_pass,
                               position=None if pos is None else dict(rule=pos.rule, side=pos.side, entry=pos.entry, stop=pos.stop),
                               realized=round(e.realized, 2), rule2_done=e.rule2_done, rule2_entered=e.rule2_entered)

    def _replay(self, bars: list[Bar]) -> None:
        """시작 전에 이미 지난 봉으로 지표(범위·RV·게이트·규칙 2 상태)만 복원합니다. **주문은 내지 않습니다.**

        지나간 신호는 이미 놓친 것입니다(지금 가격에 뒤늦게 주문하지 않음). 놓친 진입이 있었으면 그날 규칙 3 도 쓰지 않습니다
        (규칙 2 로 진입한 날은 규칙 3 을 쓰지 않는 필터형 규칙과 같게). 복원 중 가상으로 생긴 손익은 지웁니다.
        """
        now_min = self.now().replace(second=0, microsecond=0)
        ghost = SimExecutor("replay-no-order", float(self.cfg["strategy"]["tick"]), 0.0)
        for b in sorted(bars, key=lambda x: x.ts):
            if b.ts < now_min and (self.last_final is None or b.ts > self.last_final):
                self.last_bar = b
                handle(self.engine, self.engine.on_bar(b, True), ghost, None, self.qty, self.sym)
                self.bars[b.ts] = b
                self.last_final = b.ts
        missed = [dict(rule=t.rule, side=t.side, entry=t.entry, exit=t.exit, entry_ts=t.entry_ts.isoformat(), exit_ts=t.exit_ts.isoformat()) for t in self.engine.trades]
        pos = self.engine.position
        if pos is not None:
            missed.append(dict(rule=pos.rule, side=pos.side, entry=pos.entry, open=True, entry_ts=pos.entry_ts.isoformat()))
            self.engine.position = None
        self.engine.trades, self.engine.realized = [], 0.0
        print(f"[복원] 시작 전 놓친 신호 {len(missed)}건 (주문 없음), 게이트 {self.engine.gate_pass}, 아시아 시간대 금 RV {self.engine.rv:.3e}")
        self.journal.event("replay", f"시작 전 복원: 놓친 신호 {len(missed)}건, 주문 없음", self.sym, missed=missed,
                           gate_pass=self.engine.gate_pass, rv=self.engine.rv, last_bar=self.last_final.isoformat() if self.last_final else None)

    def _mark_fresh(self, bars: list[Bar]) -> None:
        now = self.now()
        now_min = now.replace(second=0, microsecond=0)
        if bars and max(b.ts for b in bars) >= now_min - timedelta(minutes=1):
            self._fresh_at = now

    def _feed(self, bars: list[Bar]) -> None:
        now = self.now()
        now_min = now.replace(second=0, microsecond=0)
        for b in sorted(bars, key=lambda x: x.ts):
            if b.ts < now_min and (self.last_final is None or b.ts > self.last_final):
                self.last_bar = b
                intents = self.engine.on_bar(b, True)
                age = (now - b.ts).total_seconds()
                if age > self.stale_entry_s and any(i.kind == "enter" for i in intents):
                    # 몇 분 지난 봉의 신호로 지금 시장가 진입을 내지 않습니다(놓친 신호로 처리, 시작 전 복원과 같은 규칙).
                    entered = self.engine.position
                    self.engine.cancel_entry()
                    if entered is not None and entered.rule == 2:
                        self.engine.rule2_entered = True     # 놓친 규칙 2 진입이 있던 날은 규칙 3 도 쓰지 않음
                    self.journal.event("stale_entry_skipped", f"{age:.0f}초 지난 봉({b.ts:%H:%M})의 진입 신호는 내지 않습니다", self.sym,
                                       bar_ts=b.ts.isoformat(), age_s=round(age, 1), rule=entered.rule if entered else None)
                    intents = []
                handle(self.engine, intents, self.executor, self.journal, self.qty, self.sym)
                self.bars[b.ts] = b
                self.last_final = b.ts
            elif b.ts == now_min:
                self.last_bar = b
                handle(self.engine, self.engine.on_bar(b, False), self.executor, self.journal, self.qty, self.sym)
        self._mark_fresh(bars)

    def _guard_stale(self) -> None:
        """보유 중 분봉이 끊기면 현재가로 손절·시간 청산을 봅니다(kq150 `_monitor_price` 와 같은 생각)."""
        e = self.engine
        if e is None or e.position is None:
            self._stale_alerted = False
            return
        now = self.now()
        age = float("inf") if self._fresh_at is None else (now - self._fresh_at).total_seconds()
        if age <= self.stale_hold_s:
            if self._stale_alerted:
                self.journal.event("feed_recovered", "분봉 조회가 돌아왔습니다", self.sym)
            self._stale_alerted = False
            return
        if not self._stale_alerted:
            self._stale_alerted = True
            self.alarm("stale_feed", f"보유 중 분봉이 {age:.0f}초째 갱신되지 않습니다. 현재가로 손절을 감시합니다.", action="price_fallback")
        price_of = getattr(self.feed, "price", None)
        if price_of is None or (self._cooldown_until is not None and now < self._cooldown_until):
            return
        try:
            px = price_of()
        except Exception as exc:  # noqa: BLE001
            self.journal.error("price", self.sym, msg=f"{type(exc).__name__}: {exc}", action="retry-next-poll")
            return
        if not px:
            return
        bar = Bar(now.replace(second=0, microsecond=0), px, px, px, px)
        self.last_bar = bar
        handle(e, e.on_bar(bar, False), self.executor, self.journal, self.qty, self.sym)

    # ------------------------------------------------------------------ 종료
    def _deadline(self) -> datetime:
        day = self.engine.day if self.engine is not None else self.now().date()
        return datetime.combine(day, datetime.min.time(), KST) + timedelta(minutes=hm(self.flatten_deadline))

    def _final_check(self) -> None:
        """마지막 잔고는 무슨 일이 있어도 확인하고, 남았거나 조회를 못 하면 경보합니다."""
        held = None
        for _ in range(3):
            try:
                held = self.gateway.positions()
                break
            except Exception as exc:  # noqa: BLE001
                self.journal.error("position_query", self.sym, msg=f"{type(exc).__name__}: {exc}", action="retry")
                self.sleep(1.0)
        if held is None:
            self.final_position = None
            self.alarm("not_flat", "마감 잔고를 조회하지 못했습니다. 직접 확인하세요.", action="manual_check")
            return
        self.journal.event("final_positions", f"마감 잔고 {len(held)}건", self.sym, positions=[(p.sym, p.side, str(p.qty)) for p in held])
        self.final_position = net_position(held, self.sym)
        if held:
            self.alarm("not_flat", "마감 후 보유 포지션이 남아 있습니다. 직접 정리하세요.", positions=len(held), action="manual_check")

    def _finish(self, reason: str) -> None:
        self.kill.shielded = True                 # 정리 중 Ctrl+C 는 무시
        e = self.engine

        def log(category: str, **fields: Any) -> None:
            self.journal.error(category, self.sym, **fields)

        def cancel_all() -> None:
            if self.gateway is not None and cancel_open_orders(self.gateway, None, log) < 0:
                raise RuntimeError("미체결 조회에 실패해 취소하지 못했습니다.")

        def flatten() -> None:
            try:
                if e is not None and e.position is not None and self.last_bar is not None:
                    intent = e.flatten_intent(self.last_bar, f"마감 정리: {reason}")
                    failed = handle(e, [intent], self.executor, self.journal, self.qty, self.sym)
                    if failed and self.gateway is None:
                        raise RuntimeError("청산 주문이 확인되지 않았습니다. 잔고를 직접 확인하세요.")
            except Exception as exc:  # noqa: BLE001
                if self.gateway is None:
                    raise
                log("flatten_engine", msg=f"{type(exc).__name__}: {exc}", action="account_flatten")
            if self.gateway is None:
                return
            try:
                sent: dict = {}
                left = flatten_account(self.gateway, self.sym, journal=self.journal, alarm=self.alarm, now=self.now, sleep=self.sleep,
                                       deadline=self._deadline(), max_attempts=self.flatten_attempts, backoff=self.flatten_backoff, report=sent)
                if left == 0 and e is not None and e.position is not None and not sent.get("orders"):
                    # 엔진은 보유로 알지만 계좌는 처음부터 0 → 체결된 적 없는 진입. 거래로 남기지 않고 엔진만 맞춥니다.
                    self.alarm("position_mismatch", "엔진은 보유로 알았지만 계좌 잔고는 0 입니다. 거래로 기록하지 않습니다.", action="sync_engine")
                    e.position = None
                elif left == 0 and e is not None and e.position is not None and self.last_bar is not None:
                    # 잔고 기준 정리로 청산됨 → 엔진을 맞추고 거래로 남김(가격은 마지막 봉 종가로 추정)
                    intent = e.flatten_intent(self.last_bar, f"마감 정리(잔고 기준, 가격 추정): {reason}")
                    rec = e.apply_exit(intent, self.last_bar.c, self.now())
                    self.journal.trade(self.sym, rule=rec.rule, side=rec.side, entry=rec.entry, exit=rec.exit, usd=round(rec.usd, 2),
                                       entry_ts=rec.entry_ts.isoformat(), exit_ts=rec.exit_ts.isoformat(), reason=rec.reason,
                                       source="mock-account", estimated=True)
            finally:
                self._final_check()

        def close() -> None:
            if self.bars_path is not None and self.bars:
                save_csv(self.bars_path, [self.bars[t] for t in sorted(self.bars)])
            if e is None:
                self.journal.run("end", reason=reason, trades=0, pnl=0.0, final_position=self.final_position)
                return
            if e.rv > 0 and self.now().date() == e.day:
                self.store.add(e.day, e.rv)
            self.journal.run("end", reason=reason, trades=len(e.trades), pnl=round(e.realized, 2), rv=e.rv,
                             gate_pass=e.gate_pass, gate_note=e.gate_note, notes=e.notes, final_position=self.final_position)

        try:
            for r in shutdown_sequence(cancel_all=cancel_all, flatten=flatten, close_journal=close, journal=self.journal):
                if not r.ok:
                    self.alarm("shutdown", f"종료 단계 {r.name} 실패: {r.error}", step=r.name)
        finally:
            self.kill.shielded = False
