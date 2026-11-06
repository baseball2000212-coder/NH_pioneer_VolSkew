"""전략 핵심 — 「아시아 시간대 금 변동성 게이트 + 필터형 규칙 2·3」. 네트워크·파일 접근이 없는 순수 로직입니다.

백테스트·자체 시뮬레이션·모의계좌 주문이 **같은 `DayEngine` 을 씁니다**(코드와 문서가 어긋나지 않도록).
엔진은 봉을 받아 `Intent`(진입/청산 의도)를 돌려주고, 체결은 실행기(`executors.py`)가 합니다.

하루(한국시간 날짜) 흐름
 1. 아시아 시간대 금 변동성 = 10:00~15:55 KST 금(MGC/GC) 1분 로그수익률 제곱합(RV). 중국 지수·A50 데이터는 쓰지 않음. 16:00 에 '오늘 이전 모든 날의 67 백분위' 이상이면 게이트 통과.
    통과하지 못하거나 이력이 120일 미만이면 그날은 거래하지 않습니다.
 2. 규칙 2 : 08:00~16:00 고저 범위(봉 200개 이상). 16:00~22:00 에 범위 고가+1틱 위/저가−1틱 아래를 처음 뚫는 쪽으로,
    직전 30분 ZT 변화 부호가 돌파 방향과 같을 때만 진입. 손절 = 범위 중간값, 23:50 청산. 하루 한 번(양방향 동시 돌파는 건너뜀).
 3. 규칙 3 : 규칙 2 포지션이 없고 오늘 규칙 2 로 진입한 적이 없을 때만. 뉴욕 08:29 종가가 세션 시가(뉴욕 18:00)보다 높으면 매수·낮으면 매도,
    뉴욕 08:30 진입 / 09:45 청산. 비상 손절 −$300.
 4. 하루 최대 1계약, 일일 손실 한도, 킬 스위치는 `RiskManager` 가 점검합니다(진입 전).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from nhapi.risk import RiskManager  # 보유 한도·일일 손실 한도·킬 스위치
from sessions import hm, kst_minutes, ny_is_dst, ny_minutes


@dataclass(frozen=True)
class Bar:
    ts: datetime          # 봉 시작 시각(시간대 포함)
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass(frozen=True)
class Intent:
    kind: str             # "enter" | "exit"
    rule: int             # 2 | 3
    side: int             # +1 매수 / −1 매도 (exit 에서는 청산할 포지션의 방향)
    ref_price: float      # 판단에 쓴 기준가(진입 수준·봉 시가·손절선 등)
    reason: str
    ts: datetime
    at_level: bool = False  # 기준가가 '봉 안에서 닿은 수준가'(돌파선·손절선)인가. 봉 시가·종가처럼 관찰된 값이면 False.
                            # 백테스트 체결 모델(backtest.BarFillExecutor)이 이 값을 보고 감지 봉 종가·다음 봉 시가로 바꿉니다.


@dataclass
class Position:
    rule: int
    side: int
    entry: float
    stop: float | None
    entry_ts: datetime


@dataclass
class TradeRecord:
    rule: int
    side: int
    entry: float
    exit: float
    entry_ts: datetime
    exit_ts: datetime
    usd: float
    reason: str


def china_rv(closes: list[float]) -> float:
    """종가 열의 1분 로그수익률 제곱합(첫 값 앞의 종가는 이미 포함해서 넘깁니다)."""
    arr = np.log(np.asarray(closes, dtype=float))
    return float(np.sum(np.diff(arr) ** 2))


def gate_threshold(history: list[float], quantile: float, min_days: int) -> float | None:
    """오늘 이전 모든 날의 아시아 시간대 금 RV 의 분위수. 이력이 min_days 미만이면 None(거래하지 않음)."""
    clean = [x for x in history if x is not None and x > 0 and not math.isnan(x)]
    if len(clean) < min_days:
        return None
    return float(np.quantile(np.asarray(clean), quantile))


class DayEngine:
    """하루치 상태기계. `on_bar(bar, final)` 을 시간 순서로 부르세요.

    `final=False` 인 봉(진행 중인 분)은 진입·손절 발동만 보고 지표(범위·RV·신호)에는 넣지 않습니다.
    """

    def __init__(
        self,
        day,
        params: dict[str, Any],
        costs: dict[str, Any],
        *,
        threshold: float | None,
        zt_move: Callable[[datetime, int], float | None],
        risk: RiskManager | None = None,
        rule3_emergency_stop_usd: float = 0.0,
        tick: float = 0.1,
        multiplier: float = 10.0,
        qty: int = 1,
        stop_gap_buffer_usd: float = 0.0,
    ) -> None:
        self.day = day
        g, r2, r3 = params["gate"], params["rule2"], params["rule3"]
        # 판단이 필요한 동작 변경은 설정 플래그로만 켭니다. 기본값은 원래 동작(리뷰 반영, CHANGES.md '사람이 결정할 것').
        self.gate_label = str(g.get("label", "아시아 시간대 금 RV"))                 # 기록·화면용 이름만 바뀝니다
        self.r3_skip_winter = bool(r3.get("skip_us_winter", False))       # 참이면 미국 겨울(서머타임 아님)에 규칙 3 을 쓰지 않음
        self.gap_buffer = float(stop_gap_buffer_usd)                      # 일일 한도 점검 때 1회 손절 금액에 더할 갭 여유(달러)
        self.q_gate_start, self.q_gate_end, self.q_gate_dec = hm(g["window_start"]), hm(g["window_end"]), hm(g["decision_time"])
        self.q_r_start, self.q_r_end = hm(r2["range_start"]), hm(r2["range_end"])
        self.q_e_start, self.q_e_end, self.q_exit2 = hm(r2["entry_start"]), hm(r2["entry_end"]), hm(r2["exit_time"])
        self.brk, self.min_range_bars = int(r2["breakout_ticks"]), int(r2["min_range_bars"])
        self.confirm_minutes, self.confirm_delay = int(r2["confirm_minutes"]), int(r2["confirm_delay_minutes"])
        self.zt_sign = 1 if float(r2["zt_fair_coeff"]) > 0 else -1
        self.n_open, self.n_sig = hm(r3["session_open_ny"]), hm(r3["signal_ny"])
        self.n_entry, self.n_exit, self.n_tol = hm(r3["entry_ny"]), hm(r3["exit_ny"]), int(r3["entry_tolerance_minutes"])
        self.cost = float(costs["round_trip_usd"])
        self.slip = float(costs.get("slippage_ticks", 0)) * tick
        self.tick, self.mult, self.qty = tick, multiplier, qty
        self.threshold, self.zt_move, self.risk = threshold, zt_move, risk
        self.e3_stop = float(rule3_emergency_stop_usd)
        # 상태
        self.prev_close: float | None = None
        self.rv = 0.0
        self.gate_pass: bool | None = None          # None = 아직 판정 전
        self.gate_note = ""
        self.top = self.bot = None
        self.range_bars = 0
        self.rule2_done = False
        self.rule2_entered = False
        self.open_px: float | None = None
        self.sig_close: float | None = None
        self.rule3_done = False
        self.position: Position | None = None
        self.realized = 0.0
        self.trades: list[TradeRecord] = []
        self.notes: list[str] = []                   # 기록용 사유(차단·건너뜀)
        self.pending_exit = False

    # ------------------------------------------------------------------ 지표
    def _update_indicators(self, bar: Bar) -> None:
        k, n = kst_minutes(bar.ts), ny_minutes(bar.ts)
        if self.q_gate_start <= k < self.q_gate_end and self.prev_close is not None:
            self.rv += math.log(bar.c / self.prev_close) ** 2
        if self.q_r_start <= k < self.q_r_end:
            self.top = bar.h if self.top is None else max(self.top, bar.h)
            self.bot = bar.l if self.bot is None else min(self.bot, bar.l)
            self.range_bars += 1
        if n >= self.n_open and self.open_px is None:
            self.open_px = bar.o
        if n <= self.n_sig and self.open_px is not None:
            self.sig_close = bar.c
        self.prev_close = bar.c

    def _decide_gate(self) -> None:
        if self.threshold is None:
            self.gate_pass, self.gate_note = False, "이력 부족: 거래하지 않음"
        else:
            self.gate_pass = self.rv >= self.threshold
            self.gate_note = f"{self.gate_label} {self.rv:.3e} {'≥' if self.gate_pass else '<'} 67 백분위 {self.threshold:.3e}"

    # ------------------------------------------------------------------ 메인
    def on_bar(self, bar: Bar, final: bool = True) -> list[Intent]:
        if final:
            self._update_indicators(bar)
        k, n = kst_minutes(bar.ts), ny_minutes(bar.ts)
        out: list[Intent] = []
        if self.gate_pass is None and k >= self.q_gate_dec:
            self._decide_gate()
        # ---- 보유 중: 손절·시간 청산
        pos = self.position
        if pos is not None:
            out += self._manage(bar, k, n)
            return out
        if not self.gate_pass:
            return out
        # ---- 규칙 2 진입
        if not self.rule2_done and self.q_e_start <= k < self.q_e_end:
            out += self._rule2_entry(bar)
            if out:
                return out
        # ---- 규칙 3 진입 (규칙 2 로 오늘 진입한 적이 없을 때만)
        if (not self.rule3_done and not self.rule2_entered and self.n_entry <= n < self.n_entry + self.n_tol
                and self.open_px is not None and self.sig_close is not None):
            out += self._rule3_entry(bar)
        return out

    def _allowed(self, side: int, one_stop_usd: float) -> bool:
        if self.risk is None:
            return True
        d = self.risk.can_enter("buy" if side > 0 else "sell", self.qty, position=0,
                                realized_pnl=_dec(self.realized), one_stop_loss=_dec(one_stop_usd + self.gap_buffer))
        if not d.allowed:
            self.notes.append(f"진입 차단: {d.reason}")
        return d.allowed

    def _rule2_entry(self, bar: Bar) -> list[Intent]:
        if self.top is None or self.range_bars < self.min_range_bars:
            self.rule2_done = True
            self.notes.append(f"규칙 2: 범위 봉 {self.range_bars}개 < {self.min_range_bars} → 건너뜀")
            return []
        up_lvl, dn_lvl = self.top + self.brk * self.tick, self.bot - self.brk * self.tick
        up, dn = bar.h >= up_lvl, bar.l <= dn_lvl
        if not (up or dn):
            return []
        self.rule2_done = True                       # 첫 돌파만 봅니다
        if up and dn:
            self.notes.append("규칙 2: 한 봉에서 양방향 동시 돌파 → 건너뜀")
            return []
        side = 1 if up else -1
        end = bar.ts - timedelta(minutes=self.confirm_delay)
        mv = self.zt_move(end, self.confirm_minutes)
        if mv is None or mv == 0 or int(math.copysign(1, mv)) * self.zt_sign != side:
            self.notes.append(f"규칙 2: 매크로(ZT) 확인 불일치 → 건너뜀 (변화 {mv})")
            return []
        entry = max(up_lvl, bar.o) if side > 0 else min(dn_lvl, bar.o)
        mid = (self.top + self.bot) / 2
        if not self._allowed(side, abs(entry - mid) * self.mult * self.qty):
            return []
        self.rule2_entered = True
        self.position = Position(2, side, entry, mid, bar.ts)
        intent = Intent("enter", 2, side, entry, f"범위 {'상단' if side > 0 else '하단'} 돌파 + ZT 확인, 손절 {mid:.1f}", bar.ts,
                        at_level=entry != bar.o)
        return [intent] + self._manage(bar, kst_minutes(bar.ts), ny_minutes(bar.ts), same_bar=True)

    def _rule3_entry(self, bar: Bar) -> list[Intent]:
        self.rule3_done = True
        if self.r3_skip_winter and not ny_is_dst(bar.ts):
            self.notes.append("규칙 3: 미국 겨울(서머타임 아님) → 건너뜀 (rule3.skip_us_winter)")
            return []
        diff = self.sig_close - self.open_px
        if diff == 0:
            self.notes.append("규칙 3: 신호 없음(종가=시가)")
            return []
        side = 1 if diff > 0 else -1
        stop_px = None
        if self.e3_stop > 0:
            stop_px = bar.o - side * self.e3_stop / (self.mult * self.qty)
        if not self._allowed(side, self.e3_stop or 1e9):
            return []
        self.position = Position(3, side, bar.o, stop_px, bar.ts)
        return [Intent("enter", 3, side, bar.o, f"뉴욕 08:29 종가 {self.sig_close} vs 세션 시가 {self.open_px}", bar.ts)]

    def _manage(self, bar: Bar, k: int, n: int, same_bar: bool = False) -> list[Intent]:
        pos = self.position
        assert pos is not None
        # 손절 (범위 중간값 / 규칙 3 비상 손절)
        if pos.stop is not None:
            hit = bar.l <= pos.stop if pos.side > 0 else bar.h >= pos.stop
            if hit:
                px = min(pos.stop, bar.o) if pos.side > 0 else max(pos.stop, bar.o)
                return [Intent("exit", pos.rule, pos.side, px, "손절" if pos.rule == 2 else "비상 손절", bar.ts, at_level=px != bar.o)]
        # 시간 청산
        if pos.rule == 2 and k >= self.q_exit2:
            return [Intent("exit", 2, pos.side, bar.o, "23:50 시간 청산", bar.ts)]
        if pos.rule == 3 and self.n_exit <= n < self.n_open:
            return [Intent("exit", 3, pos.side, bar.o, "뉴욕 09:45 시간 청산", bar.ts)]
        return []

    # ------------------------------------------------------------------ 체결 반영
    def apply_exit(self, intent: Intent, fill_price: float, ts: datetime) -> TradeRecord:
        pos = self.position
        assert pos is not None
        usd = (fill_price - pos.entry) * pos.side * self.mult * self.qty - self.cost * self.qty
        record = TradeRecord(pos.rule, pos.side, pos.entry, fill_price, pos.entry_ts, ts, usd, intent.reason)
        self.trades.append(record)
        self.realized += usd
        self.position = None
        return record

    def apply_entry(self, fill_price: float) -> None:
        """실제 체결가로 진입가·손절선을 맞춥니다(손절 거리는 유지)."""
        pos = self.position
        if pos is None:
            return
        if pos.stop is not None and pos.rule == 3:
            pos.stop += fill_price - pos.entry
        pos.entry = fill_price

    def cancel_entry(self) -> None:
        """진입 주문이 거부·미체결이면 포지션을 되돌립니다."""
        pos = self.position
        self.position = None
        if pos is not None and pos.rule == 2:
            self.rule2_entered = False
        if pos is not None and pos.rule == 3:
            self.rule3_done = True

    def flatten_intent(self, bar: Bar, reason: str) -> Intent | None:
        pos = self.position
        if pos is None:
            return None
        return Intent("exit", pos.rule, pos.side, bar.c, reason, bar.ts)


def _dec(x: float):
    from decimal import Decimal
    return Decimal(str(round(x, 6)))
