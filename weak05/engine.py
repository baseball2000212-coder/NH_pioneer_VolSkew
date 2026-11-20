"""실시간 실행 엔진 (paper-sim / paper-order 공통): 5분봉 완성 → 신호 → 지정가 진입 → 손절·시간·세션 청산.

상태: IDLE(무포지션, 신호 대기) → PENDING(진입 지정가 대기, 제한시간 후 취소) → LONG/SHORT(보유) → 청산 → IDLE → … → DONE.
안전 규칙(CLAUDE.md §4-2):
- 주문 요청은 자동 재시도하지 않는다. 응답이 없으면 주문내역→잔고로 접수 여부를 판정한다 (`broker.resolve`).
- 시작 때 잔고·미체결을 대사한다. 보유·미체결이 있으면 시작하지 않는다(`resume` 이면 보유를 이어받음).
- 30초마다 잔고 대사. 어긋나면 신규 진입을 멈추고 잔고 기준으로 정리한다.
- 킬 스위치: `STOP` 파일 / Ctrl+C → 미체결 취소 → 청산(reduce_only) → 잔고 0 확인 → 기록 마감.
- 최대 보유 1계약, 하루 최대 거래 수, 일일 손실 한도(남은 한도 < 1회 손절이면 신규 진입 금지), 거래 창과 세션 정리 시각.
신호 계산은 `signals.py`(백테스트와 같은 함수)를 쓴다. 매 5분봉마다 특징·T²·SVM 점수를 기록해 사후 리플레이와 대조한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

from bars import minute_from_rows, to_bars
from signals import FEATURES, SignalModel, compute_features, decide, in_window

IDLE, PENDING, HOLD, DONE = "IDLE", "PENDING", "HOLD", "DONE"


# ------------------------------------------------------------------ 순수 판단 함수 (단위 테스트 대상)

def stop_distance(rv_bp: float, close: float, h: int, k: float) -> float:
    """손절 거리(가격 pt) = k × σ5m × √h. backtest.simulate 와 같은 식."""
    return k * rv_bp / 1e4 * close * math.sqrt(h)


def stop_hit(side: int, entry: float, stop_dist: float, bid: float, ask: float) -> bool:
    """보유 방향에서 불리한 쪽 호가가 손절선을 넘었나 (롱은 매도호가가 아니라 팔 수 있는 가격 = 매수호가 기준)."""
    return bid <= entry - stop_dist if side == 1 else ask >= entry + stop_dist


def entry_allowed(*, now: datetime, entry_time: datetime, h: int, bar_min: int, flatten: datetime, window: tuple[str, str],
                  trades_today: int, max_trades: int, realized_usd: float, daily_limit_usd: float, one_stop_usd: float,
                  halted: bool) -> tuple[bool, str]:
    """신규 진입 가능 여부와 사유. 백테스트 simulate 의 제약(창·세션 정리·하루 거래 수)에 일일 손실 한도를 더한 것."""
    if halted:
        return False, "halted"
    if not bool(in_window(pd.DatetimeIndex([entry_time]), *window)[0]):
        return False, "outside_window"
    if entry_time + timedelta(minutes=bar_min * h) > flatten:
        return False, "no_time_before_flatten"
    if trades_today >= max_trades:
        return False, "max_trades"
    if daily_limit_usd + realized_usd < one_stop_usd:
        return False, "daily_loss_limit"
    return True, ""


@dataclass
class Position:
    side: int
    entry_px: float
    entry_ts: datetime
    exit_by: datetime
    stop_dist: float
    qty: int = 1
    entry_order: str = ""


@dataclass
class Stats:
    trades: int = 0
    realized_usd: float = 0.0
    signals: int = 0
    skipped: dict[str, int] = field(default_factory=dict)


class Engine:
    def __init__(self, broker: Any, cfg: dict[str, Any], model: SignalModel, *, sym: str, source: str,
                 clock: Callable[[], datetime], sleep: Callable[[float], None], log: Any, kill: Any,
                 window: str = "live", exit_at: datetime | None = None, h: int, gate: bool,
                 realized_before_usd: float = 0.0, resume: bool = False, poll_sec: float = 2.0,
                 bars_out: list[dict[str, Any]] | None = None) -> None:
        self.b, self.cfg, self.model, self.sym, self.source = broker, cfg, model, sym, source
        self.clock, self.sleep, self.log, self.kill = clock, sleep, log, kill
        self.h, self.gate, self.resume, self.poll = h, gate, resume, poll_sec
        self.window = tuple(cfg["windows"][window])
        a = cfg["assets"][sym]
        self.point_value = a["tick_value"] / a["tick"]
        self.fee_rt = a["fee_roundtrip_usd"]
        self.risk = cfg["risk"]
        now = clock()
        fh, fm = map(int, cfg["flatten"].split(":"))
        self.flatten = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
        self.exit_at = exit_at or self.flatten
        dh, dm = map(int, self.risk["deadline"].split(":"))
        self.deadline = now.replace(hour=dh, minute=dm, second=0, microsecond=0)
        self.state, self.pos = IDLE, None
        self.stats = Stats(realized_usd=realized_before_usd)
        self.realized_run = 0.0
        self.halted = False
        self._last_bar: pd.Timestamp | None = None
        self._last_recon = now
        self.bars_out = bars_out if bars_out is not None else []
        self.pending: dict[str, Any] | None = None

    # ------------------------------------------------------------ 기록
    def j(self, name: str, text: str = "", **data: Any) -> None:
        self.log.event(name, text, self.sym, **data)

    def err(self, cat: str, **data: Any) -> None:
        self.log.error(cat, self.sym, **data)

    # ------------------------------------------------------------ 실행
    def run(self) -> dict[str, Any]:
        self.log.run("start", sym=self.sym, source=self.source, strategy=self.cfg["strategy_id"], window=list(self.window),
                     h=self.h, gate=self.gate, flatten=self.flatten.isoformat(), model=self.model.meta)
        if not self.startup():
            return self.finish("startup_refused")
        reason = ""
        try:
            while self.state != DONE:
                if self.kill.triggered():
                    reason = f"kill: {self.kill.reason}"
                    break
                now = self.clock()
                if now >= self.deadline:
                    reason = "deadline"
                    break
                if now >= self.exit_at and self.state == IDLE:
                    reason = "exit_time"
                    break
                self.tick(now)
                self.sleep(1.0 if self.state in (PENDING, HOLD) else self.poll)
        except KeyboardInterrupt:
            reason = "KeyboardInterrupt"
        return self.shutdown(reason or "done")

    def startup(self) -> bool:
        pos = self.b.positions()
        orders = self.b.open_orders()
        self.j("startup", positions=pos, open_orders=len(orders))
        if orders:
            self.err("startup_open_orders", count=len(orders))
            return False
        held = pos.get(self.sym, 0)
        if pos and not (self.resume and set(pos) == {self.sym}):
            self.err("startup_position", positions=pos)
            return False
        if held:
            q = self.b.quote(self.sym)
            ref = q.mid if q else 0.0
            self.pos = Position(1 if held > 0 else -1, ref, self.clock(), self.clock(), 0.0, abs(held))
            self.state = HOLD
            self.j("resume", qty=held, ref_px=ref)
        return True

    # ------------------------------------------------------------ 한 주기
    def tick(self, now: datetime) -> None:
        if (now - self._last_recon).total_seconds() >= 30:
            self.reconcile(now)
        if self.state == PENDING:
            self.check_pending(now)
            self.check_signal(now, entry=False)        # 대기·보유 중에도 봉 특징을 기록(리플레이 대조용), 진입은 안 함
        elif self.state == HOLD:
            self.check_exit(now)
            if self.state == HOLD:
                self.check_signal(now, entry=False)
        elif self.state == IDLE:
            self.check_signal(now)

    def reconcile(self, now: datetime) -> None:
        self._last_recon = now
        pos = self.b.positions()
        have = pos.get(self.sym, 0)
        want = 0 if self.pos is None else self.pos.side * self.pos.qty
        if self.state == PENDING:
            return                                         # 진입 대기 중에는 체결 순간과 겹칠 수 있어 건너뜀
        if have != want or set(pos) - {self.sym}:
            self.err("recon_mismatch", have=pos, want=want)
            self.halted = True
            self.ensure_flat("recon_mismatch")
            self.pos, self.state = None, IDLE
        else:
            self.j("recon_ok", position=have)

    # ------------------------------------------------------------ 신호
    def completed_bar_start(self, now: datetime) -> pd.Timestamp:
        step = self.cfg["bar_minutes"]
        return (pd.Timestamp(now) - pd.Timedelta(seconds=10)).floor(f"{step}min") - pd.Timedelta(minutes=step)

    def check_signal(self, now: datetime, entry: bool = True) -> None:
        start = self.completed_bar_start(now)
        if self._last_bar is not None and start <= self._last_bar:
            return
        self._last_bar = start
        try:
            rows = self.b.minute_rows(self.sym)
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 다음 봉에 다시
            self.err("minute_fetch", msg=f"{type(exc).__name__}: {str(exc)[:120]}")
            return
        bars = to_bars(minute_from_rows(rows), self.cfg["bar_minutes"], self.cfg["min_sub"])
        bars = bars[bars.index <= start]
        if start not in bars.index:
            self.j("bar_missing", bar=str(start), reason="완성된 5분봉 없음(체결 부족)")
            return
        feats = compute_features(bars, self.cfg)
        sig = decide(self.model, feats, self.cfg, use_gate=self.gate)
        row, f = sig.loc[start], feats.loc[start]
        rec = {"bar": str(start), "close": float(bars.loc[start, "close"]), **{k: (None if pd.isna(f[k]) else round(float(f[k]), 6)) for k in FEATURES},
               "t2": None if pd.isna(row["t2"]) else round(float(row["t2"]), 4), "in_control": bool(row["in_control"]),
               "svm_side": int(row["svm_side"]), "side": int(row["side"]), "ok": bool(f["ok"])}
        self.bars_out.append(rec)
        self.j("bar", **rec)
        side = int(row["side"])
        if side == 0 or not entry:
            return
        late = (now - (start + pd.Timedelta(minutes=self.cfg["bar_minutes"])).to_pydatetime()).total_seconds()
        if late > self.risk["stale_signal_sec"]:               # 시작이 봉 중간이었거나 조회가 늦음 → 백테스트가 가정한 '다음 봉 시가' 진입이 아님
            self.stats.skipped["stale_signal"] = self.stats.skipped.get("stale_signal", 0) + 1
            self.j("skip_entry", why="stale_signal", side=side, late_sec=round(late, 1))
            return
        self.stats.signals += 1
        self.log.signal("buy" if side == 1 else "sell", "svm_t2", self.sym, bar=str(start), t2=rec["t2"], close=rec["close"])
        self.try_enter(now, side, bars, feats, start)

    def try_enter(self, now: datetime, side: int, bars: pd.DataFrame, feats: pd.DataFrame, start: pd.Timestamp) -> None:
        step = self.cfg["bar_minutes"]
        entry_time = (start + pd.Timedelta(minutes=step)).to_pydatetime()
        sd = stop_distance(float(feats.loc[start, "rv_bp"]), float(bars.loc[start, "close"]), self.h, self.cfg["stop_k"])
        one_stop = sd * self.point_value + self.fee_rt
        ok, why = entry_allowed(now=now, entry_time=entry_time, h=self.h, bar_min=step, flatten=self.flatten, window=self.window,
                                trades_today=self.stats.trades, max_trades=self.risk["max_trades_per_day"],
                                realized_usd=self.stats.realized_usd + self.realized_run, daily_limit_usd=self.risk["daily_loss_limit_usd"],
                                one_stop_usd=one_stop, halted=self.halted)
        if not ok:
            self.stats.skipped[why] = self.stats.skipped.get(why, 0) + 1
            self.j("skip_entry", why=why, side=side)
            return
        q = self.b.quote(self.sym)
        if q is None or not q.ok():
            self.stats.skipped["no_quote"] = self.stats.skipped.get("no_quote", 0) + 1
            self.j("skip_entry", why="no_quote", side=side)
            return
        price = q.bid if side == 1 else q.ask                      # 같은 쪽 최우선 호가에 지정가(호가 폭을 건너지 않음)
        ack = self.b.place(self.sym, "buy" if side == 1 else "sell", 1, price)
        self.log.order("entry_limit", self.sym, side=side, price=price, status=ack.status, order_no=ack.order_no, code=ack.code, msg=ack.message)
        if ack.status == "unknown":
            ack = self.b.resolve(ack)
            self.log.order("entry_resolve", self.sym, status=ack.status, order_no=ack.order_no)
        if ack.status != "accepted":
            if ack.status == "unknown":                            # 접수 여부 불명 → 잔고로 판정할 때까지 진입 금지
                self.halted = True
                self.err("entry_unknown", note="접수 여부 불명, 신규 진입 중단")
            return
        exit_by = entry_time + timedelta(minutes=step * self.h)
        self.pending = {"side": side, "order_no": ack.order_no, "since": now, "price": price, "exit_by": exit_by, "stop_dist": sd}
        self.state = PENDING

    # ------------------------------------------------------------ 진입 대기
    def check_pending(self, now: datetime) -> None:
        p = self.pending
        assert p is not None
        f = self.b.fill(p["order_no"])
        if f is not None and f.qty >= 1:
            self.on_entry_fill(now, f.price or p["price"])
            return
        if (now - p["since"]).total_seconds() < self.risk["entry_timeout_sec"] and not self.kill.triggered():
            return
        ack = self.b.cancel(self.sym, "buy" if p["side"] == 1 else "sell", 1, p["order_no"])
        self.log.order("entry_cancel", self.sym, status=ack.status, order_no=p["order_no"], code=ack.code)
        f = self.b.fill(p["order_no"])                              # 취소 직전 체결됐을 수 있음
        if f is not None and f.qty >= 1:
            self.on_entry_fill(now, f.price or p["price"])
            return
        self.j("entry_timeout", side=p["side"], price=p["price"])
        self.pending, self.state = None, IDLE

    def on_entry_fill(self, now: datetime, px: float) -> None:
        p = self.pending
        assert p is not None
        self.log.fill(self.source, self.sym, role="entry", side=p["side"], px=px, qty=1, order_no=p["order_no"])
        self.pos = Position(p["side"], px, now, p["exit_by"], p["stop_dist"], 1, p["order_no"])
        self.pending, self.state = None, HOLD

    # ------------------------------------------------------------ 보유 중
    def check_exit(self, now: datetime) -> None:
        pos = self.pos
        assert pos is not None
        reason = ""
        q = self.b.quote(self.sym)
        if q is not None and q.ok() and pos.stop_dist > 0 and stop_hit(pos.side, pos.entry_px, pos.stop_dist, q.bid, q.ask):
            reason = "stop"
        elif now >= pos.exit_by:
            reason = "time"
        elif now >= self.flatten or now >= self.exit_at:
            reason = "session"
        elif self.kill.triggered():
            reason = "kill"
        if reason:
            self.exit_position(reason)

    def exit_position(self, reason: str) -> None:
        pos = self.pos
        assert pos is not None
        side = "sell" if pos.side == 1 else "buy"
        ack = self.b.place(self.sym, side, pos.qty, None, reduce_only=True)
        self.log.order("exit_market", self.sym, reason=reason, status=ack.status, order_no=ack.order_no, code=ack.code, msg=ack.message)
        if ack.status == "unknown":
            ack = self.b.resolve(ack)
        px = None
        if ack.status == "accepted":
            for _ in range(10):
                f = self.b.fill(ack.order_no)
                if f is not None and f.qty >= pos.qty:
                    px = f.price
                    break
                self.sleep(1.0)
        if self.b.positions().get(self.sym, 0) != 0:                # 청산이 확인되지 않으면 잔고 기준으로 정리
            self.ensure_flat(f"exit_{reason}")
            px = px or (self.b.quote(self.sym) or type("x", (), {"mid": pos.entry_px})()).mid
        if px is None:
            q = self.b.quote(self.sym)
            px = q.mid if q else pos.entry_px
            self.j("exit_px_estimated", px=px)
        gross_pt = pos.side * (float(px) - pos.entry_px)
        gross = gross_pt * self.point_value
        net = gross - self.fee_rt
        self.log.fill(self.source, self.sym, role="exit", reason=reason, side=-pos.side, px=px, qty=pos.qty)
        self.log.trade(self.sym, side=pos.side, entry_px=pos.entry_px, exit_px=float(px), reason=reason, gross_usd=round(gross, 2),
                       fee_usd_assumed=self.fee_rt, net_usd=round(net, 2), held_sec=int((self.clock() - pos.entry_ts).total_seconds()),
                       note="수수료는 가정값(편도 $2×2), 호가 폭은 실제 체결가에 포함")
        self.stats.trades += 1
        self.realized_run += net
        self.pos, self.state = None, IDLE

    # ------------------------------------------------------------ 정리
    def ensure_flat(self, why: str) -> bool:
        """잔고 기준 정리: 미체결 취소 → 보유가 있으면 반대 시장가(reduce_only). 새 판단이므로 최대 3번, 매번 잔고로 확인."""
        for o in self.b.open_orders():
            ack = self.b.cancel(o.sym, o.side, o.qty, o.order_no)
            self.log.order("cancel_open", o.sym, why=why, status=ack.status, order_no=o.order_no)
        for _ in range(3):
            held = self.b.positions().get(self.sym, 0)
            if held == 0:
                return True
            ack = self.b.place(self.sym, "sell" if held > 0 else "buy", abs(held), None, reduce_only=True)
            self.log.order("flatten_market", self.sym, why=why, held=held, status=ack.status, order_no=ack.order_no, code=ack.code)
            self.sleep(2.0)
        left = self.b.positions().get(self.sym, 0)
        if left:
            self.err("flatten_failed", held=left, why=why)
        return left == 0

    def shutdown(self, reason: str) -> dict[str, Any]:
        self.j("shutdown_start", reason=reason, state=self.state)
        if self.state == PENDING and self.pending:
            p = self.pending
            self.b.cancel(self.sym, "buy" if p["side"] == 1 else "sell", 1, p["order_no"])
            f = self.b.fill(p["order_no"])
            if f is not None and f.qty >= 1:
                self.on_entry_fill(self.clock(), f.price or p["price"])
        if self.state == HOLD and self.pos is not None:
            self.exit_position("kill" if reason.startswith(("kill", "Keyboard")) else reason)
        flat = self.ensure_flat(reason)
        return self.finish(reason, flat=flat)

    def finish(self, reason: str, flat: bool | None = None) -> dict[str, Any]:
        if flat is None:
            flat = self.b.positions().get(self.sym, 0) == 0
        summary = {"reason": reason, "flat": flat, "trades": self.stats.trades, "signals": self.stats.signals,
                   "net_usd": round(self.realized_run, 2), "skipped": self.stats.skipped, "source": self.source}
        self.log.run("summary", **summary)
        self.log.run("end", reason=reason)
        self.state = DONE
        return summary
