"""6주차: 2주차 국내전략(장 막판 모멘텀)을 20계약으로 — 호가 따라 분할 진입·분할 손절 청산·조각별 감시를 더한 Trader.

기존 `runner.Trader` 의 판단·보유·종가 단일가 청산·대사·종료 정리를 그대로 쓰고 두 곳만 바꾼다.
  1) 진입(_trade): `planner` 가 정한 계약 수 n 을 조각으로 낸다.
     조각 크기 = 보내는 순간 1·2호가 잔량 − 우리가 먹고 덜 돌아온 몫(반감기 2.78초), 남으면 5초 뒤 같은 규칙.
     조각마다: 스프레드 검사(첫 조각 ≥6틱이면 최대 1초 보류 뒤 보냄, 다음 조각 ≥4틱이면 1초씩 최대 5초 보류 뒤 접음)
     → 시장가 → **잔고로 실제 체결 수량 확인** → 보낸 순간 중간가 대비 슬리피지(틱) 기록
     → 3틱 넘으면 남은 조각을 1호가만큼·10초 간격으로 늦추고, 5틱 넘거나 거부가 2번이면 멈추고 경보.
     호가가 2초 넘게 안 왔으면 사다리 일정(설정)으로, 그것도 없으면 남은 수량 한 번에.
  2) 손절 청산(_exit, purpose='stop', 2주차 10pt 손절): 1·2호가 잔량씩 1초 간격. 남으면 기존 청산 경로로 한 번에.
     킬 스위치·시세 끊김(stale)·정해진 15:45 청산은 기존대로 한 번에 낸다(빨리·확실히 나오는 게 우선).
조각 체결 확인을 잔고로 하는 이유: 결과 모름·부분 체결이 섞여도 '지금 몇 계약 들고 있나'가 하나로 정해져
같은 주문을 두 번 내거나 반대 포지션을 만드는 일이 없다(quant-team P0 원칙).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable

from runner import ExecResult, Trader
import split_exec as sx
import strategy_core as core


@dataclass
class SplitPlan:
    qty: int                         # 오늘 목표 계약 수 (0 이면 진입 안 함)
    entry: list[sx.Child]            # 호가가 없을 때 쓸 사다리 일정
    exit_stop: list[sx.Child]        # (기록용) 호가가 없을 때 손절 청산 비중
    stop_pt: Decimal                 # 평균 진입가 기준 손절폭(pt)
    info: dict[str, Any] = field(default_factory=dict)   # 판단 근거(기록용)


class SplitTrader(Trader):
    def __init__(self, *args: Any, planner: Callable[[dict[str, Any], str], SplitPlan], guard: sx.GuardConfig,
                 book: Callable[[], dict[str, Any] | None] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.planner, self.guard = planner, guard
        self.book = book or (lambda: None)
        self.plan: SplitPlan | None = None
        self.children: list[dict[str, Any]] = []
        self.qty_entered = 0

    # ------------------------------------------------------------------ 진입

    def _trade(self, side: str, values: dict[str, Any]) -> dict[str, Any]:
        c, g = self.cfg, self.guard
        t0 = self._at(c.entry_time)
        plan = self.planner(values, side)
        self.plan = plan
        self.j.event("w06_plan", "오늘 크기·조각·손절", qty=plan.qty, ladder=[(ch.offset_s, ch.qty) for ch in plan.entry],
                     stop_pt=plan.stop_pt, **plan.info)
        if plan.qty <= 0:
            return self._done("skip", "w06_size_zero", **{k: str(v) for k, v in plan.info.items()})
        held, cost, halt = self._children(side, plan.qty, t0, purpose="entry", gap_s=g.gap_s,
                                          spread_first_wait=g.spread_first_wait_s, window_s=g.window_s, ladder=plan.entry)
        self.qty_entered = held
        self.j.event("entry_summary", "분할 진입 결과", planned=plan.qty, filled=held, halt=halt or "none",
                     children=sum(1 for r in self.children if r["purpose"] == "entry"))
        if held == 0:
            if halt == "kill":
                return self._done("aborted", "kill")
            return self._done("entry_not_filled", halt or "no_fill")
        self.side, self.qty_held, self.entry_price = side, held, cost / held
        stop = core.stop_price(side, self.entry_price, plan.stop_pt)
        self.j.event("position_open", "진입 완료", side=side, qty=held, entry_price=self.entry_price, stop_price=stop,
                     stop_pt=plan.stop_pt, source=self.broker.source)
        reason = "kill" if halt == "kill" else self._hold(side, stop)
        exit_res = self._exit(reason)
        return self._finalize_trade(reason, exit_res, values)

    # ------------------------------------------------------------------ 조각 공통

    def _children(self, side: str, total: int, t0, *, purpose: str, gap_s: float, spread_first_wait: float,
                  window_s: float, ladder: list[sx.Child] | None = None, sign_held: int | None = None):
        """total 계약을 호가 따라 조각으로 낸다. 진입이면 보유가 늘고(sign_held=None), 청산이면 줄어든다(sign_held=보유 방향).
        반환 (체결된 수량, 체결가×수량 합, 멈춘 이유 '' = 끝까지)."""
        c, g = self.cfg, self.guard
        closing = sign_held is not None
        sign = sign_held if closing else (1 if side == "buy" else -1)
        base = 0
        if closing:
            p0 = self._position()
            if p0 is None:
                return 0, Decimal(0), "position_query"
            base = sign * p0
        done, cost, rejects, halt, slowed = 0, Decimal(0), 0, "", False
        prev: list[tuple[float, int]] = []
        k, next_at = 0, t0
        while done < total:
            while self.clock() < next_at:
                if self.risk.kill.triggered() and not closing:
                    return done, cost, "kill"
                self._keep_warm(due=next_at)      # 조각 간격(5·10초) 사이에 연결이 닫히지 않게
                self.sleep(min(c.poll_interval_s, max(0.005, (next_at - self.clock()).total_seconds())))
            elapsed = (self.clock() - t0).total_seconds()
            if elapsed > window_s:
                halt = "late"
                self.j.event("child_halt", "창을 넘겨 남은 조각 접음", purpose=purpose, child=k, elapsed_s=elapsed)
                break
            if closing and self.clock() >= self._at(c.stop_until):
                halt = "stop_until"
                break
            # 스프레드 검사
            first = k == 0
            thr = g.spread_first_ticks if first else g.spread_next_ticks
            wait_s = spread_first_wait if first else g.spread_next_wait_s
            began = self.clock()
            while True:
                snap = self.feed.snapshot()
                sp = sx.spread_ticks(snap.bid, snap.ask, c.tick)
                if (sp is not None and sp < thr) or (self.clock() - began).total_seconds() >= wait_s:
                    break
                self.sleep(0.2 if first else 1.0)
            held_back = (self.clock() - began).total_seconds()
            age = (snap.ts - snap.last_ts).total_seconds() if snap.last_ts is not None else None
            if age is None or age > g.max_age_s:
                halt = "stale"
                self.j.event("child_halt", "시세가 끊겨 남은 조각 접음", purpose=purpose, child=k, age_s=age)
                break
            if not first and (sp is None or sp >= thr):
                halt = "wide"
                self.j.event("child_halt", "스프레드가 계속 넓어 남은 조각 접음", purpose=purpose, child=k, spread_ticks=sp)
                break
            remaining = total - done
            book = self.book()
            lv = list((book or {}).get("ask" if side == "buy" else "bid") or [])
            fresh_book = bool(lv) and float((book or {}).get("age_s") or 0.0) <= g.book_max_age_s
            now_s = (self.clock() - t0).total_seconds()
            left = sx.own_left(prev, now_s, g.half_life_s)
            if fresh_book:
                want = sx.depth_child(lv, 1 if slowed else g.depth_levels, remaining, left)
                sizing = "depth1" if slowed else f"depth{g.depth_levels}"
            elif ladder and k < len(ladder):
                want, sizing = min(remaining, ladder[k].qty), "ladder"
            else:
                want, sizing = remaining, "rest"
            shadow = sx.walk_book(lv, want)[0] if lv else None
            sent_at = self.clock()
            res = self.broker.market_order(side, want, f"{purpose}_{k}")
            self._orders_sent = True
            pos = self._position()
            now_held = sign * pos if pos is not None else None
            moved = None if now_held is None else ((base - now_held) if closing else now_held)
            if moved is None or now_held < 0 or moved < done or moved > done + want:
                self._unresolved = True
                self._alarm("position_mismatch", f"{purpose} 조각 {k} 뒤 잔고 {pos} (예상 이동 {done}~{done + want}). 남은 조각 접음.",
                            "manual_check")
                halt = "position_mismatch"
                break
            got = moved - done
            fill = res.price if (res.price is not None and got > 0) else (snap.last if got > 0 else None)
            if got > 0 and res.price is None:
                if closing:
                    self.exit_estimated = True
                else:
                    self.entry_estimated = True
            slip = sx.slip_ticks(side, fill, snap.bid, snap.ask, c.tick) if fill is not None else None
            row = {"purpose": purpose, "child": k, "sent": sent_at.isoformat(timespec="milliseconds"),
                   "late_ms": int((sent_at - next_at).total_seconds() * 1000), "spread_ticks": sp,
                   "held_back_s": round(held_back, 2), "sizing": sizing, "own_left": round(left, 2),
                   "depth_seen": sum(q for _, q in lv[:g.depth_levels]) if lv else None, "best_qty": lv[0][1] if lv else None,
                   "want": want, "got": got, "status": res.status, "order_no": res.order_no, "fill": fill,
                   "bid": snap.bid, "ask": snap.ask, "slip_ticks": slip, "shadow_book_fill": shadow, "code": res.code}
            self.children.append(row)
            self.j.event("child_fill", f"{purpose} 조각 {k}", **row)
            prev.append((now_s, want))
            if got > 0:
                done += got
                cost += fill * got
                if closing:
                    self.qty_held = now_held
            if res.status == "rejected" or got == 0:
                rejects += 1
                if rejects >= g.max_rejects:
                    halt = "rejects"
                    self._alarm("child_rejects", f"{purpose} 거부·미체결 {rejects}번 → 남은 조각 접음", "stop_children")
                    break
            if slip is not None and slip > g.halt_slip_ticks:
                halt = "slippage"
                self._alarm("child_slippage", f"{purpose} 조각 {k} 슬리피지 {slip}틱 > {g.halt_slip_ticks}틱 → 남은 조각 멈춤",
                            "stop_children")
                break
            if slip is not None and slip > g.slow_slip_ticks and not slowed:
                slowed = True
                self.j.event("child_slow", f"슬리피지 {slip}틱 > {g.slow_slip_ticks}틱 → 1호가만큼·{g.slow_gap_s:g}초 간격으로 늦춤",
                             purpose=purpose, child=k)
            k += 1
            next_at = sent_at + timedelta(seconds=g.slow_gap_s if slowed else gap_s)
        return done, cost, halt

    # ------------------------------------------------------------------ 청산

    def _exit(self, purpose: str) -> ExecResult | None:
        """손절만 호가 따라 분할(1·2호가씩 exit_gap_s 간격). 킬·시세 끊김·정해진 종가 청산은 기존대로 한 번에."""
        if purpose != "stop" or self.qty_held <= 1:
            return super()._exit(purpose)
        g = self.guard
        side = core.opposite(self.side or "")
        sign = 1 if self.side == "buy" else -1
        done, cost, halt = self._children(side, self.qty_held, self.clock(), purpose="stop", gap_s=g.exit_gap_s,
                                          spread_first_wait=0.0, window_s=60.0, sign_held=sign)
        fills = [(done, cost / done)] if done else []
        if self.qty_held > 0 and not self._unresolved:
            self.j.event("stop_child_rest", "남은 수량은 한 번에 청산", remaining=self.qty_held, halt=halt)
            rest = super()._exit("stop")
            if rest is not None and rest.filled_qty > 0:
                fills.append((rest.filled_qty, rest.price))
        exited = sum(q for q, _ in fills) >= self.qty_entered
        self._exited = exited
        if exited:
            self.qty_held = 0
        if not fills:
            return None
        qty = sum(q for q, _ in fills)
        avg = sum(q * p for q, p in fills) / qty
        return ExecResult("filled" if exited else "partial", qty, avg, source=self.broker.source)

    def _finalize_trade(self, purpose: str, exit_res: ExecResult | None, values: dict[str, Any]) -> dict[str, Any]:
        self.qty_held = self.qty_entered        # 기록의 수량 = 들어간 수량
        out = super()._finalize_trade(purpose, exit_res, values)
        out["qty"] = self.qty_entered
        out["planned_qty"] = self.plan.qty if self.plan else None
        out["entry_vwap"] = str(self.entry_price) if self.entry_price is not None else None
        out["exit_avg"] = str(exit_res.price) if (exit_res is not None and exit_res.price is not None) else None
        self.result = out
        return out
