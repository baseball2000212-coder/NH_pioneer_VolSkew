"""[자체 시뮬레이션(종이 체결)]·[모의계좌 주문] 실행 루프 — 같은 `DayEngine` 에 실시간 분봉을 먹입니다.

흐름: 시작 대사(보유·미체결) → 분봉 백필 → 몇 초마다 최신 분봉 조회 → 엔진이 낸 의도를 실행기로 → 기록.
멈춤: `STOP` 파일 또는 Ctrl+C → 미체결 취소 → 포지션 정리 → 기록 마감 (`shutdown_sequence`).
마감: `live.end_time`(기본 23:55) 에 남은 포지션을 정리하고, 모의계좌 모드는 잔고가 0 계약인지 확인합니다.
분봉 시각은 한국시간이며, 현재 진행 중인 분의 봉은 '진행 중'으로만 엔진에 넣습니다.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from backtest import handle, make_engine
from executors import SimExecutor
from feeds import ZtSeries, save_csv
from gate_store import GateStore
from nhapi.journal import Journal
from nhapi.overseas import parse_overseas_bars
from nhapi.risk import KillSwitch, RiskLimits, RiskManager, shutdown_sequence
from sessions import KST, hm
from strategy_core import Bar


class LiveFeed:
    """NH 해외 차트(분) REST 조회. 최신 쪽부터 오래된 쪽으로 이어받습니다(연속조회 `nxt_key`)."""

    def __init__(self, client, exch: str, sym: str, zt_exch: str, zt_sym: str, backfill_pages: int = 4) -> None:
        self.client, self.exch, self.sym, self.zt_exch, self.zt_sym, self.pages = client, exch, sym, zt_exch, zt_sym, backfill_pages

    @staticmethod
    def _payload(exch: str, sym: str, today: date) -> dict[str, str]:
        start = today - timedelta(days=1)
        return {"exch_cd": exch, "sym": sym, "inq_strt_dt": start.strftime("%Y%m%d"), "strt_tm": "000000",
                "inq_ed_dt": today.strftime("%Y%m%d"), "ed_tm": "235959", "req_qty": "300", "dcnt": "1", "cidx_yn": "0", "nxt_key": "99999999"}

    @staticmethod
    def _bars(body: Any) -> list[Bar]:
        return [Bar(b.ts, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume or 0)) for b in parse_overseas_bars(body)]

    def backfill(self, today: date, since: datetime) -> tuple[list[Bar], list[Bar]]:
        out = []
        for exch, sym in ((self.exch, self.sym), (self.zt_exch, self.zt_sym)):
            got: dict[datetime, Bar] = {}
            for body in self.client.paginate("overseas_chart-minute", self._payload(exch, sym, today), max_pages=self.pages):
                bars = self._bars(body)
                for b in bars:
                    got[b.ts] = b
                if bars and bars[0].ts <= since:
                    break
            out.append([got[t] for t in sorted(got) if t >= since])
        return out[0], out[1]

    def latest(self, today: date) -> tuple[list[Bar], list[Bar]]:
        res = []
        for exch, sym in ((self.exch, self.sym), (self.zt_exch, self.zt_sym)):
            res.append(self._bars(self.client.query("overseas_chart-minute", self._payload(exch, sym, today)).body))
        return res[0], res[1]


class LiveRunner:
    def __init__(self, cfg: dict[str, Any], feed, executor, store: GateStore, journal: Journal, kill: KillSwitch, *,
                 now: Callable[[], datetime] = lambda: datetime.now(KST), sleep: Callable[[float], None] = time.sleep,
                 gateway=None, mode: str = "paper-sim", bars_path=None) -> None:
        self.cfg, self.feed, self.executor, self.store, self.journal, self.kill = cfg, feed, executor, store, journal, kill
        self.now, self.sleep, self.gateway, self.mode = now, sleep, gateway, mode
        self.sym, self.qty = cfg["strategy"]["sym"], int(cfg["strategy"]["order_qty"])
        self.live = cfg["live"]
        self.risk = RiskManager(RiskLimits.from_dict(cfg["risk"]), kill)
        self.zt = ZtSeries()
        self.last_final: datetime | None = None
        self.last_bar: Bar | None = None
        self.engine = None
        self.bars_path = bars_path
        self.bars: dict[datetime, Bar] = {}      # 그날 확정된 분봉(차트용으로 results/ 에 저장)
        self._gate_logged = False
        self._last_hb: datetime | None = None

    def run(self) -> dict[str, Any]:
        today = self.now().date()
        g = self.cfg["params"]["gate"]
        thr = self.store.threshold(today, float(g["quantile"]), int(g["min_history_days"]))
        self.engine = make_engine(today, self.cfg, thr, self.zt, self.risk)
        self.journal.run("start", day=today.isoformat(), threshold=thr, mode=self.mode, history_days=len(self.store.history_before(today)))
        midnight = datetime.combine(today, datetime.min.time(), KST)
        end_at = midnight + timedelta(minutes=hm(self.live["end_time"]))
        mgc, zt = self.feed.backfill(today, midnight + timedelta(hours=6))
        self.zt.update(zt)
        self._replay(mgc)
        reason = "마감 시각"
        while True:
            if self.kill.triggered():
                reason = f"킬 스위치: {self.kill.reason}"
                break
            if self.now() >= end_at:
                break
            if getattr(self.executor, "halted", False):
                reason = "주문 상태 불확실 — 신규 주문 중단"
                self.journal.error("order_state", self.sym, msg=reason, action="halt")
                break
            self.sleep(float(self.live["poll_seconds"]))
            try:
                mgc, zt = self.feed.latest(today)
            except Exception as exc:  # noqa: BLE001 — 조회 실패는 기록하고 다음 주기에 다시 시도
                self.journal.error("poll", self.sym, msg=f"{type(exc).__name__}: {exc}", action="retry-next-poll")
                continue
            self.zt.update(zt)
            self._feed(mgc)
            self._heartbeat()
        self._finish(reason)
        return {"reason": reason, "trades": len(self.engine.trades), "pnl": round(self.engine.realized, 2)}

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
        print(f"[복원] 시작 전 놓친 신호 {len(missed)}건 (주문 없음), 게이트 {self.engine.gate_pass}, 중국장 RV {self.engine.rv:.3e}")
        self.journal.event("replay", f"시작 전 복원: 놓친 신호 {len(missed)}건, 주문 없음", self.sym, missed=missed,
                           gate_pass=self.engine.gate_pass, rv=self.engine.rv, last_bar=self.last_final.isoformat() if self.last_final else None)

    def _feed(self, bars: list[Bar]) -> None:
        now_min = self.now().replace(second=0, microsecond=0)
        for b in sorted(bars, key=lambda x: x.ts):
            if b.ts < now_min and (self.last_final is None or b.ts > self.last_final):
                self.last_bar = b
                handle(self.engine, self.engine.on_bar(b, True), self.executor, self.journal, self.qty, self.sym)
                self.bars[b.ts] = b
                self.last_final = b.ts
            elif b.ts == now_min:
                self.last_bar = b
                handle(self.engine, self.engine.on_bar(b, False), self.executor, self.journal, self.qty, self.sym)

    def _finish(self, reason: str) -> None:
        def cancel_all() -> None:
            if self.gateway is not None:
                for rec in self.gateway.open_orders():
                    self.gateway.cancel(rec.sym, rec.side, int(rec.remaining_qty or rec.qty or 1), rec.order_no)

        def flatten() -> None:
            if self.engine.position is not None and self.last_bar is not None:
                intent = self.engine.flatten_intent(self.last_bar, f"마감 정리: {reason}")
                failed = handle(self.engine, [intent], self.executor, self.journal, self.qty, self.sym)
                if failed:
                    raise RuntimeError("청산 주문이 확인되지 않았습니다. 잔고를 직접 확인하세요.")
            if self.gateway is not None:
                held = self.gateway.positions()
                self.journal.event("final_positions", f"마감 잔고 {len(held)}건", self.sym, positions=[(p.sym, p.side, str(p.qty)) for p in held])
                if held:
                    self.journal.error("not_flat", self.sym, msg="마감 후 보유 포지션이 남아 있습니다. 직접 정리하세요.", positions=len(held))

        def close() -> None:
            if self.bars_path is not None and self.bars:
                save_csv(self.bars_path, [self.bars[t] for t in sorted(self.bars)])
            if self.engine.rv > 0 and self.now().date() == self.engine.day:
                self.store.add(self.engine.day, self.engine.rv)
            self.journal.run("end", reason=reason, trades=len(self.engine.trades), pnl=round(self.engine.realized, 2), rv=self.engine.rv,
                             gate_pass=self.engine.gate_pass, gate_note=self.engine.gate_note, notes=self.engine.notes)

        for r in shutdown_sequence(cancel_all=cancel_all, flatten=flatten, close_journal=close, journal=self.journal):
            if not r.ok:
                self.journal.error("shutdown", self.sym, step=r.name, msg=r.error)
