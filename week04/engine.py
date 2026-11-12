# 출처: 사용자 제공 초안 nh_w04_engine_20261111.zip 의 nh_w04/engine.py (2026-11-11). 4주차에 수정: 위험 한도(MES 상한·일일 손실),
# 패리티 잔차 기록, 중지 파일 `STOP`, 평가 종목 호가 조회.
"""스트래들 + MES 델타헤지 엔진: 진입 → 헤지 루프 → 청산.

원칙
- 포지션의 진실은 계좌 잔고다. 주문 뒤와 `recon_every` 초마다 잔고를 읽어 내부 상태를 덮어쓴다(재시작해도 잔고에서 이어 감).
- 주문은 재시도하지 않는다. 응답이 없으면 주문내역으로 확인하고, 그래도 모르면 잔고 차이로 판정한다.
- 옵션은 매수와 '보유분 매도'만 한다(NH 모의는 옵션 신규 매도를 7093 으로 거부). 매도 수량은 항상 잔고 조회값 이하.
- 델타는 ES 호가로 매 주기 다시 계산하고, IV 는 옵션 호가 가운데 값에서 갱신한다(체결가·현재가 조회는 쓰지 않는다).
- 풋 매수가 실패하면 콜만 든 채 계속한다 — 헤저가 MES 를 팔아 '콜 + MES' 구조가 된다.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from brokers import Ack
from strategy_core import (Attribution, Cfg, HedgeThrottle, Leg, ParityStats, Pnl, Q, StopWatch, atm_strike, clip_hedge,
                           entry_allowed, hedge_order, mismatch, opt_sym, parity_residual, parse_opt_sym,
                           portfolio_greeks, year_frac)

DEADLINE_MARGIN = timedelta(minutes=2)      # 마감 이만큼 전부터는 전부 시장가로 정리


class Engine:
    def __init__(self, broker: Any, cfg: Cfg, *, clock: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[dict], None] | None = None,
                 resume: bool = False, stay: bool = False, realized_today: float = 0.0) -> None:
        self.b, self.cfg, self.clock, self.sleep = broker, cfg, clock, sleep
        self._log_fn = log
        self.resume, self.stay = resume, stay
        self.realized_today = realized_today                # 오늘 이미 확정된 손익($, 이전 실행 요약에서 읽음) — 일일 손실 한도 점검용
        self.parity = ParityStats()
        self.state = "INIT"
        self.legs: list[Leg] = []
        self.mes_pos = 0
        self.pnl = Pnl()
        self.attr = Attribution()
        self.throttle = HedgeThrottle(cfg.min_gap, cfg.max_hedges_per_hour)
        self.stop = StopWatch(cfg.stop_per_pair * max(cfg.pairs, 1), cfg.stop_hold)
        self.exit_reason = ""
        self.n_hedges = 0
        self.mes_traded = 0
        self.last: dict[str, Any] = {}
        self._hedge_fail = 0                              # 연속으로 체결 0 이었던 헤지 주문 수
        self._hedge_pause_until: datetime | None = None
        self._t_recon: datetime | None = None
        self._t_log: datetime | None = None
        self._warned: dict[str, datetime] = {}

    # ------------------------------------------------------------ 기록
    def log(self, kind: str, **fields: Any) -> None:
        rec = {"ts": self.clock().isoformat(timespec="seconds"), "kind": kind, "state": self.state, **fields}
        if self._log_fn is not None:
            self._log_fn(rec)
        else:
            print(json.dumps(rec, ensure_ascii=False, default=str), flush=True)

    def _warn(self, key: str, **fields: Any) -> None:
        """같은 경고는 30초에 한 번만."""
        now = self.clock()
        if key not in self._warned or (now - self._warned[key]).total_seconds() >= 30:
            self._warned[key] = now
            self.log("warn", what=key, **fields)

    # ------------------------------------------------------------ 잔고
    def _ours(self, sym: str) -> bool:
        return sym == self.cfg.mes_sym or sym.startswith(self.cfg.series + "-")

    def _positions(self) -> dict[str, int] | None:
        try:
            return {s: q for s, q in self.b.positions().items() if self._ours(s)}
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 '없음'이 아니다
            self._warn("positions", msg=f"{type(exc).__name__}: {str(exc)[:160]}")
            return None

    def _positions_sure(self, tries: int = 4) -> dict[str, int] | None:
        """청산처럼 잔고를 꼭 알아야 할 때: 조회가 실패하면 1초 간격으로 다시 본다."""
        for i in range(tries):
            pos = self._positions()
            if pos is not None:
                return pos
            if i < tries - 1:
                self.sleep(1.0)
        return None

    def _pos(self, sym: str) -> int | None:
        pos = self._positions()
        return None if pos is None else pos.get(sym, 0)

    def _recon(self, force: bool = False) -> None:
        """잔고를 읽어 내부 수량을 덮어쓴다. 다르면 기록한다."""
        now = self.clock()
        if not force and self._t_recon is not None and (now - self._t_recon).total_seconds() < self.cfg.recon_every:
            return
        pos = self._positions()
        if pos is None:
            return
        self._t_recon = now
        diff = {}
        for leg in self.legs:
            real = pos.get(leg.sym, 0)
            if real != leg.qty:
                diff[leg.sym] = [leg.qty, real]
                leg.qty = real
        real = pos.get(self.cfg.mes_sym, 0)
        if real != self.mes_pos:
            diff[self.cfg.mes_sym] = [self.mes_pos, real]
            self.mes_pos = real
        known = {l.sym for l in self.legs} | {self.cfg.mes_sym}
        extra = {s: q for s, q in pos.items() if s not in known}
        if diff or extra:
            self.log("recon_fix", diff=diff, extra=extra)

    # ------------------------------------------------------------ 주문 한 건
    def _wait_fill(self, order_no: str, qty: int, wait: float) -> tuple[int, float | None]:
        done, px = 0, None
        t_end = self.clock() + timedelta(seconds=wait)
        while True:
            f = self.b.fill(order_no)
            if f is not None:
                done, px = f.qty, f.price if f.price else px
                if done >= qty:
                    break
            if self.clock() >= t_end:
                break
            self.sleep(1.0)
        return done, px

    def _place(self, sym: str, side: str, qty: int, price: float | None, reduce_only: bool) -> Ack:
        """주문을 내고, 응답이 없으면 주문내역으로 확인한다. 예외는 '불명'으로 돌려 잔고 판정에 맡긴다(재주문하지 않음)."""
        try:
            ack = self.b.place(sym, side, qty, price, reduce_only)
        except ValueError as exc:                                  # 보내기 전 검증 실패 → 나가지 않았음
            return Ack("rejected", code="INVALID", message=str(exc)[:160])
        except Exception as exc:  # noqa: BLE001
            return Ack("unknown", code="EXC", message=f"{type(exc).__name__}: {str(exc)[:160]}")
        if ack.status == "unknown":
            try:
                ack = self.b.resolve(ack)
            except Exception as exc:  # noqa: BLE001
                self._warn("resolve", msg=f"{type(exc).__name__}: {str(exc)[:160]}")
        return ack

    def _cancel(self, sym: str, side: str, qty: int, order_no: str) -> Ack:
        try:
            return self.b.cancel(sym, side, qty, order_no)
        except Exception as exc:  # noqa: BLE001
            return Ack("unknown", code="EXC", message=f"{type(exc).__name__}: {str(exc)[:160]}")

    def _execute(self, sym: str, side: str, qty: int, *, limit: bool, reduce_only: bool = False, why: str = "") -> tuple[int, float | None]:
        """주문 → 체결 확인 → (지정가 미체결이면 취소 후 남은 수량 시장가) → 잔고 차이로 최종 판정.

        반환: (부호 있는 체결 수량, 평균 체결가 또는 추정가). 손익 장부에도 반영한다.
        """
        cfg = self.cfg
        before = self._pos(sym)
        q = self.b.quote(sym)
        price = None
        if limit and q is not None and q.ok():
            price = q.ask if side == "buy" else q.bid
        fills: list[tuple[int, float | None]] = []
        ack = self._place(sym, side, qty, price, reduce_only)
        self.log("order", why=why, sym=sym, side=side, qty=qty, price=price, status=ack.status, order_no=ack.order_no, code=ack.code, msg=ack.message)
        if ack.status == "accepted":
            done, px = self._wait_fill(ack.order_no, qty, cfg.fill_wait)
            if done < qty and price is not None:
                c = self._cancel(sym, side, qty - done, ack.order_no)
                self.log("order", why="cancel_unfilled", sym=sym, side=side, qty=qty - done, status=c.status, code=c.code, order_no=ack.order_no)
                self.sleep(1.0)
                f = self.b.fill(ack.order_no)
                if f is not None:
                    done, px = f.qty, f.price if f.price else px
            if done:
                fills.append((done, px))
            rest = qty - done
            if rest > 0 and price is not None:
                mid_pos = self._pos(sym)
                if before is not None and mid_pos is not None:
                    rest = qty - abs(mid_pos - before)           # 취소 전에 더 체결됐을 수 있다 → 잔고 기준
                if rest > 0:
                    ack2 = self._place(sym, side, rest, None, reduce_only)
                    self.log("order", why=why + "/market", sym=sym, side=side, qty=rest, price=None, status=ack2.status, order_no=ack2.order_no, code=ack2.code, msg=ack2.message)
                    if ack2.status == "accepted":
                        d2, p2 = self._wait_fill(ack2.order_no, rest, cfg.fill_wait)
                        if d2:
                            fills.append((d2, p2))
        # 최종 판정은 잔고
        after = self._pos(sym)
        sign = 1 if side == "buy" else -1
        told = sum(n for n, _ in fills)
        if before is not None and after is not None:
            signed = after - before
            if signed != sign * told:
                self.log("recon_fix", why="fill_vs_position", sym=sym, told=sign * told, position_diff=signed)
        else:
            signed = sign * told
        known = [(n, p) for n, p in fills if p]
        if known:
            px_avg = sum(n * p for n, p in known) / sum(n for n, _ in known)
            est = False
        else:
            q2 = self.b.quote(sym) or q
            px_avg = (q2.ask if side == "buy" else q2.bid) if q2 is not None and q2.ok() else None
            est = True
        if signed != 0 and px_avg is not None:
            is_opt = parse_opt_sym(sym) is not None
            self.pnl.fill(signed, px_avg, cfg.opt_mult if is_opt else cfg.mes_mult, cfg.opt_fee if is_opt else cfg.mes_fee)
        self.log("fill", why=why, sym=sym, signed=signed, px=px_avg, px_estimated=est)
        return signed, px_avg

    # ------------------------------------------------------------ 시작·진입
    def _startup(self) -> None:
        cfg = self.cfg
        if os.path.exists(cfg.stop_file):                          # 지난번 중지 신호가 남아 있으면 지운다(안 지우면 진입 직후 청산됨)
            try:
                os.remove(cfg.stop_file)
                self.log("warn", what="stale_stop_file_removed", path=cfg.stop_file)
            except OSError as exc:
                self.exit_reason = f"중지 신호 파일을 지울 수 없음: {exc}"
                self.state = "DONE"
                return
        if os.path.exists(cfg.kill_file) and not self.resume:      # 공용 킬스위치가 켜져 있으면 새로 들어가지 않는다
            self.exit_reason = f"킬스위치({cfg.kill_file})가 켜져 있어 진입하지 않음"
            self.log("finish", reason=self.exit_reason)
            self.state = "DONE"
            return
        pos = self._positions()
        if pos is None:
            self.exit_reason = "잔고 조회 실패"
            self.state = "DONE"
            return
        try:
            oo = [o for o in self.b.open_orders() if self._ours(o.sym)]
        except Exception as exc:  # noqa: BLE001
            self._warn("open_orders", msg=f"{type(exc).__name__}: {exc}")
            oo = []
        self.log("startup", positions=pos, open_orders=[(o.sym, o.side, o.qty) for o in oo])
        if not pos and not oo:
            self.state = "ENTER"
            return
        if not self.resume:
            self.exit_reason = "이미 포지션·미체결이 있음 (--resume 없이 진입 거부)"
            self.state = "DONE"
            return
        for o in oo:
            c = self._cancel(o.sym, o.side, o.qty, o.order_no)
            self.log("order", why="resume_cancel", sym=o.sym, status=c.status, order_no=o.order_no)
        es = self.b.quote(self.cfg.es_sym)
        now = self.clock()
        for sym, qty in pos.items():
            parsed = parse_opt_sym(sym)
            if parsed is None:
                continue
            leg = Leg(sym, parsed[1], parsed[2], qty)
            q = self.b.quote(sym)
            if q is not None and q.ok() and es is not None and es.ok():
                leg.update_iv(q, es.mid, year_frac(now, self.cfg.expiry), self.cfg)
                leg.entry_px = q.mid
                self.pnl.fill(qty, q.mid, self.cfg.opt_mult, 0.0)      # 이어받은 시점의 가운데 값이 기준
            self.legs.append(leg)
        self.mes_pos = pos.get(self.cfg.mes_sym, 0)
        if self.mes_pos and es is not None and es.ok():
            self.pnl.fill(self.mes_pos, es.mid, self.cfg.mes_mult, 0.0)
        self.log("resume", legs=[(l.sym, l.qty) for l in self.legs], mes=self.mes_pos)
        self.state = "HEDGE"

    def _wait_entry(self) -> bool:
        """진입 시각(`enter_at`)까지 기다린다(미장 개장 전에 구축하기 위함). 기다리는 동안에도 중지 신호·마감을 본다.

        기다림을 끝내고 진입해도 되면 True, 중지·마감으로 끝내야 하면 False(이미 _finish 호출).
        """
        cfg = self.cfg
        if cfg.enter_at is None or self.clock() >= cfg.enter_at:
            return True
        if cfg.exit_at is not None and cfg.enter_at >= cfg.exit_at - timedelta(seconds=30):
            self._finish(f"진입 거부: 진입 시각({cfg.enter_at:%H:%M:%S})이 청산 시각({cfg.exit_at:%H:%M:%S}) 30초 전보다 늦음")
            return False
        self.log("wait_entry", enter_at=cfg.enter_at, note="진입 시각까지 대기(포지션 없음)")
        last = self.clock()
        while self.clock() < cfg.enter_at:
            if os.path.exists(cfg.kill_file) or os.path.exists(cfg.stop_file):
                self._finish("진입 전 중지 신호(STOP/KILL)")
                return False
            if cfg.deadline is not None and self.clock() >= cfg.deadline - DEADLINE_MARGIN:
                self._finish("진입 전 마감 도달")
                return False
            if (self.clock() - last).total_seconds() >= 60:
                last = self.clock()
                es = self.b.quote(cfg.es_sym)
                self.log("waiting", remaining_sec=round((cfg.enter_at - self.clock()).total_seconds()), es=None if es is None else [es.bid, es.ask])
            self.sleep(min(cfg.poll, 5.0))
        return True

    def _enter(self) -> None:
        cfg = self.cfg
        if not self._wait_entry():
            return
        soon = self.clock() + timedelta(seconds=30)
        if cfg.exit_at is not None and cfg.exit_at <= soon:
            self._finish(f"진입 거부: 청산 시각({cfg.exit_at:%H:%M:%S})이 이미 지났거나 30초 안")
            return
        if cfg.deadline is not None and cfg.deadline - DEADLINE_MARGIN <= soon:
            self._finish(f"진입 거부: 마감({cfg.deadline:%H:%M})이 임박")
            return
        ok, remaining = entry_allowed(cfg.daily_loss_limit, self.realized_today, cfg.stop_per_pair * max(cfg.pairs, 1))
        if not ok:
            self._finish(f"진입 거부: 오늘 남은 손실 한도 ${remaining:.0f} 가 1회 손절 금액 ${cfg.stop_per_pair * max(cfg.pairs, 1):.0f} 보다 작음")
            return
        t_end = self.clock() + timedelta(seconds=cfg.entry_timeout)
        picked = None
        while self.clock() < t_end:
            es = self.b.quote(cfg.es_sym)
            if es is not None and es.ok():
                k = atm_strike(es.mid, cfg.strike_step)
                syms = [opt_sym(cfg.series, cp, k) for cp in ("C", "P")]
                qs = [self.b.quote(s) for s in syms]
                if all(q is not None and q.ok(cfg.entry_max_spread) for q in qs):
                    picked = (es, k, syms, qs)
                    break
                self._warn("entry_quotes", strike=k, quotes=[None if q is None else (q.bid, q.ask) for q in qs])
            else:
                self._warn("es_quote")
            self.sleep(cfg.poll)
        if picked is None:
            self._finish("진입 실패: 호가가 조건을 못 맞춤")
            return
        es, k, syms, qs = picked
        T = year_frac(self.clock(), cfg.expiry)
        self.legs = [Leg(syms[0], "C", k), Leg(syms[1], "P", k)]
        self.log("entry", es=es.mid, strike=k, call=(qs[0].bid, qs[0].ask), put=(qs[1].bid, qs[1].ask))
        for leg, q in zip(self.legs, qs):
            leg.update_iv(q, es.mid, T, cfg)
            signed, px = self._execute(leg.sym, "buy", cfg.pairs, limit=True, why="entry")
            leg.qty, leg.entry_px = signed, px or q.ask
            if signed <= 0 and leg.cp == "C":
                self._finish("진입 실패: 콜 매수가 체결되지 않음")
                return
            if signed <= 0:
                self.log("warn", what="put_failed", note="콜만 보유 → MES 로 델타를 맞춘다")
        self._recon(force=True)
        self.state = "HEDGE"

    def _finish(self, reason: str) -> None:
        self.exit_reason = reason
        self.log("finish", reason=reason)
        self.state = "IDLE" if self.stay else "DONE"

    # ------------------------------------------------------------ 헤지 루프
    def _cycle(self) -> None:
        cfg, now = self.cfg, self.clock()
        if os.path.exists(cfg.kill_file) or os.path.exists(cfg.stop_file):
            self.state, self.exit_reason = "EXIT", "kill"
            return
        if cfg.deadline is not None and now >= cfg.deadline - DEADLINE_MARGIN:
            self.state, self.exit_reason = "EXIT", "deadline"
            return
        if cfg.exit_at is not None and now >= cfg.exit_at:
            self.state, self.exit_reason = "EXIT", "time"
            return
        self._recon()
        if not any(l.qty for l in self.legs) and self.mes_pos == 0:
            self._finish("포지션 없음")
            return
        es = self.b.quote(cfg.es_sym)
        if es is None or not es.ok():
            self._warn("es_quote")
            self.sleep(cfg.poll)
            return
        F, T = es.mid, year_frac(now, cfg.expiry)
        quotes: dict[str, Q | None] = {}
        for leg in self.legs:
            if leg.qty:
                quotes[leg.sym] = self.b.quote(leg.sym)
                if quotes[leg.sym] is not None:
                    leg.update_iv(quotes[leg.sym], F, T, cfg)
        self._parity_sample(quotes)
        g = portfolio_greeks(self.legs, F, T)
        self.attr.step(F, T, self.legs, self.mes_pos, cfg)
        value, marked = self.mes_pos * F * cfg.mes_mult, True
        for leg in self.legs:
            if leg.qty:
                m = leg.mark(quotes.get(leg.sym), F, T, cfg)
                if m is None:
                    marked = False
                else:
                    value += leg.qty * m * cfg.opt_mult
        net = self.pnl.net(value) if marked else None
        stale = es.ts is not None and (now - es.ts).total_seconds() > cfg.stale_sec
        band = self.throttle.band(cfg.band, now)
        miss = None if g is None else mismatch(g["delta"], self.mes_pos, cfg.mes_per_es)
        self.last = {"F": F, "T_hours": round(T * 8760, 2), "greeks": g, "mes": self.mes_pos, "mismatch": miss, "band": band,
                     "pnl_net": net, "pnl_gross": self.pnl.gross(value) if marked else None, "fees": self.pnl.fees,
                     "iv": {l.sym: l.iv for l in self.legs}, "attr": self.attr.as_dict()}
        if self._t_log is None or (now - self._t_log).total_seconds() >= cfg.log_every:
            self._t_log = now
            self.log("state", **self._state_fields(quotes))
        if net is not None and self.stop.update(now, net):
            self.state, self.exit_reason = "EXIT", "stop"
            self.log("stop", pnl_net=round(net, 2), threshold=-self.stop.threshold)
            return
        if stale:
            self._warn("es_stale", age=(now - es.ts).total_seconds())
        elif g is not None:
            n = hedge_order(g["delta"], self.mes_pos, band, cfg.mes_per_es)
            cap = cfg.max_mes_per_pair * max(cfg.pairs, 1)
            clipped = clip_hedge(n, self.mes_pos, cap)
            if clipped != n:
                self._warn("mes_cap", wanted=n, allowed=clipped, mes=self.mes_pos, cap=cap)
                n = clipped
            paused = self._hedge_pause_until is not None and now < self._hedge_pause_until
            if n != 0 and not paused and self.throttle.allowed(now):
                self.log("hedge", F=F, opt_delta=round(g["delta"], 4), mes=self.mes_pos, mismatch=round(miss, 3), band=band, order=n)
                signed, _ = self._execute(cfg.mes_sym, "buy" if n > 0 else "sell", abs(n), limit=False, why="hedge")
                self.mes_pos += signed
                self.throttle.record(now)
                self.n_hedges += 1
                self.mes_traded += abs(signed)
                if signed == 0:                                   # 거부·미체결이 이어지면 10초마다 계속 두드리지 않는다
                    self._hedge_fail += 1
                    if self._hedge_fail >= 3:
                        self._hedge_pause_until = now + timedelta(seconds=60 * min(self._hedge_fail - 2, 5))
                        self.log("warn", what="hedge_failing", fails=self._hedge_fail, pause_until=self._hedge_pause_until)
                else:
                    self._hedge_fail = 0
                self._recon(force=True)
        self.sleep(cfg.poll)

    def _parity_sample(self, quotes: dict[str, Q | None]) -> None:
        """같은 행사가 콜·풋을 둘 다 들고 있고 호가가 멀쩡하면 패리티 잔차(콜−풋+K−MES)를 모은다. 기록·분석용, 주문에 쓰지 않는다."""
        cfg = self.cfg
        calls = [l for l in self.legs if l.qty and l.cp == "C"]
        puts = [l for l in self.legs if l.qty and l.cp == "P"]
        if not calls or not puts or calls[0].strike != puts[0].strike:
            self.last_parity = None
            return
        qc, qp = quotes.get(calls[0].sym), quotes.get(puts[0].sym)
        qm = self.b.quote(cfg.mes_sym)
        if not all(q is not None and q.ok(cfg.iv_max_spread) for q in (qc, qp)) or qm is None or not qm.ok():
            self.last_parity = None
            return
        resid = parity_residual(qc.mid, qp.mid, calls[0].strike, qm.mid)
        half_cost = 0.5 * (qc.spread + qp.spread + qm.spread)
        self.parity.add(resid, half_cost)
        self.last_parity = {"resid": round(resid, 4), "half_cost": round(half_cost, 4), "mes_mid": qm.mid}

    def _state_fields(self, quotes: dict[str, Q | None]) -> dict[str, Any]:
        s = self.last
        g = s.get("greeks")
        c = self.cfg
        out = {"F": s["F"], "T_hours": s["T_hours"], "mes": s["mes"], "band": s["band"],
               "mismatch": None if s["mismatch"] is None else round(s["mismatch"], 3),
               "pnl_net": None if s["pnl_net"] is None else round(s["pnl_net"], 2),
               "pnl_gross": None if s["pnl_gross"] is None else round(s["pnl_gross"], 2), "fees": s["fees"],
               "iv": {k: None if v is None else round(v, 5) for k, v in s["iv"].items()}, "attr": s["attr"],
               "quotes": {k: None if q is None else [q.bid, q.ask] for k, q in quotes.items()},
               "parity": getattr(self, "last_parity", None)}
        if g is not None:
            out["delta"] = round(g["delta"], 4)
            out["gamma_per_pt"] = round(g["gamma"], 6)
            out["theta_usd_per_hour"] = round(g["theta"] / 8760 * c.opt_mult, 2)
            out["vega_usd_per_volpt"] = round(g["vega"] / 100 * c.opt_mult, 2)
        return out

    # ------------------------------------------------------------ 청산
    def _exit(self) -> None:
        cfg = self.cfg
        self.log("exit_start", reason=self.exit_reason)
        try:
            for o in self.b.open_orders():
                if self._ours(o.sym):
                    c = self._cancel(o.sym, o.side, o.qty, o.order_no)
                    self.log("order", why="exit_cancel", sym=o.sym, status=c.status, order_no=o.order_no)
        except Exception as exc:  # noqa: BLE001
            self._warn("open_orders", msg=f"{type(exc).__name__}: {exc}")
        rush = cfg.deadline is not None and self.clock() >= cfg.deadline - DEADLINE_MARGIN
        for attempt in (1, 2, 3):
            pos = self._positions_sure()
            if pos is None:                                        # 잔고를 못 읽으면 아무것도 팔지 않는다(옵션 초과 매도 방지)
                self.log("warn", what="exit_no_positions", attempt=attempt)
                continue
            for leg in self.legs:
                held = pos.get(leg.sym, 0)
                if held > 0:                                       # 매도 수량 ≤ 잔고 (초과하면 7093)
                    signed, _ = self._execute(leg.sym, "sell", held, limit=not rush and attempt == 1, reduce_only=True, why="exit")
                    leg.qty = held + signed
            mes = (self._positions_sure() or {}).get(cfg.mes_sym, 0)
            if mes != 0:
                signed, _ = self._execute(cfg.mes_sym, "sell" if mes > 0 else "buy", abs(mes), limit=False, reduce_only=True, why="exit")
                self.mes_traded += abs(signed)
                self.mes_pos = mes + signed
            left = self._positions_sure()
            if left is not None and not left:
                break
            self.log("warn", what="exit_not_flat", attempt=attempt, left=left)
        self._recon(force=True)
        left = self._positions_sure()
        self.log("summary", reason=self.exit_reason, flat=(left == {}), left=left, pnl_gross=round(self.pnl.gross(0.0), 2),
                 fees=self.pnl.fees, pnl_net=round(self.pnl.net(0.0), 2), attr=self.attr.as_dict(),
                 hedges=self.n_hedges, mes_traded=self.mes_traded, parity=self.parity.as_dict())
        self.state = "IDLE" if self.stay else "DONE"

    def _idle(self) -> None:
        """포지션 없이 시세만 기록하며 버틴다(대회 창에서 프로그램을 멈추지 않기 위함)."""
        cfg = self.cfg
        end = cfg.deadline or cfg.exit_at
        if end is None or self.clock() >= end:
            self.state = "DONE"
            return
        es = self.b.quote(cfg.es_sym)
        self.log("idle", es=None if es is None else [es.bid, es.ask])
        self.sleep(max(cfg.poll, 30.0))

    # ------------------------------------------------------------ 실행
    def run(self) -> None:
        cfg = self.cfg
        self.log("start", series=cfg.series, expiry=cfg.expiry, pairs=cfg.pairs, band=cfg.band, min_gap=cfg.min_gap,
                 stop=-self.stop.threshold, enter_at=cfg.enter_at, exit_at=cfg.exit_at, deadline=cfg.deadline, resume=self.resume)
        try:
            self._startup()
            if self.state == "ENTER":
                self._enter()
            while self.state not in ("DONE",):
                if self.state == "HEDGE":
                    self._cycle()
                elif self.state == "EXIT":
                    self._exit()
                elif self.state == "IDLE":
                    self._idle()
                else:
                    break
        except KeyboardInterrupt:
            self.exit_reason = "interrupt"
            self.log("warn", what="interrupt")
            if any(l.qty for l in self.legs) or self.mes_pos:
                self.state = "EXIT"
                self._exit()
        self.log("end", reason=self.exit_reason)
