"""전략 핵심 로직 — 장 막판 모멘텀(3% 규칙). API·시계·파일을 모르는 순수 함수만 둡니다.

규칙 (strategy.json 의 값):
    15:19 등락률 r = (현재가 / 전일 종가 − 1) × 100
    |r| < threshold_pct 이면 r 의 부호 방향(양 → 매수, 음 → 매도)으로 15:20 에 시장가 진입
    |r| ≥ threshold_pct 또는 r == 0 이면 쉼
    재난 손절: 진입가에서 불리하게 stop_pt 만큼 움직이면(15:34:55 까지) 시장가 청산
    청산: 15:45 종가 단일가 (exit_mode=auction) 또는 15:34 연속매매 (exit_mode=continuous)
근거: Baltussen et al.(2021) 장 마감 직전 모멘텀, 성태균·정형찬(2019) KOSDAQ150 ETF 재조정 수요.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Any, Literal, Mapping

Side = Literal["buy", "sell"]
EXIT_MODES = ("auction", "continuous")


def opposite(side: str) -> str:
    if side == "buy":
        return "sell"
    if side == "sell":
        return "buy"
    raise ValueError(f"알 수 없는 방향: {side!r}")


def day_return_pct(prev_close: Decimal, price: Decimal) -> Decimal:
    """전일 종가 대비 등락률(%)."""
    if prev_close <= 0:
        raise ValueError("전일 종가는 0 보다 커야 합니다.")
    return (price / prev_close - 1) * 100


@dataclass(frozen=True)
class Decision:
    side: str | None
    reason: str  # enter / skip_threshold / skip_flat


def decide(ret_pct: Decimal, threshold_pct: Decimal) -> Decision:
    """진입 방향. 경계는 |r| < threshold 만 진입(팀 백테스트와 같음)."""
    if ret_pct == 0:
        return Decision(None, "skip_flat")
    if abs(ret_pct) >= threshold_pct:
        return Decision(None, "skip_threshold")
    return Decision("buy" if ret_pct > 0 else "sell", "enter")


def stop_price(side: str, entry: Decimal, stop_pt: Decimal) -> Decimal:
    return entry - stop_pt if side == "buy" else entry + stop_pt


def stop_hit(side: str, price: Decimal, stop: Decimal) -> bool:
    """가격이 손절선에 닿았거나 넘었는지."""
    return price <= stop if side == "buy" else price >= stop


def pnl_pt(side: str, entry: Decimal, exit_: Decimal, cost_pt: Decimal = Decimal(0)) -> Decimal:
    """계약당 손익(pt), 비용 차감."""
    gross = exit_ - entry if side == "buy" else entry - exit_
    return gross - cost_pt


def pnl_won(pt: Decimal, qty: int, multiplier: Decimal) -> Decimal:
    return pt * qty * multiplier


def _t(text: str) -> time:
    parts = [int(p) for p in str(text).split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2])


@dataclass(frozen=True)
class StrategyConfig:
    """strategy.json 을 검사해 담은 값. 칸이 빠지면 KeyError, 값이 이상하면 ValueError."""

    name: str
    version: str
    sym: str
    contract: str
    qty: int
    multiplier: Decimal
    tick: Decimal
    threshold_pct: Decimal
    stop_pt: Decimal
    decision_time: time
    entry_time: time
    entry_grace_s: float
    stop_until: time
    exit_mode: str
    exit_submit_auction: time
    exit_submit_continuous: time
    exit_retry_time: time
    auction_fill_time: time
    end_time: time
    start_not_before: time
    cost_pt: Decimal
    sim_slippage_ticks: Decimal
    price_max_age_s: float
    poll_interval_s: float
    fill_poll_interval_s: float
    fill_poll_max_s: float
    risk: Mapping[str, Any]
    backtest: Mapping[str, Any]

    @property
    def one_stop_loss_won(self) -> Decimal:
        """1회 손절 금액(원) = 손절폭 × 승수 × 수량."""
        return self.stop_pt * self.multiplier * self.qty

    @property
    def exit_submit_time(self) -> time:
        return self.exit_submit_auction if self.exit_mode == "auction" else self.exit_submit_continuous

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StrategyConfig":
        s, p = data["strategy"], data["params"]
        cfg = cls(
            name=s["name"], version=s["version"], sym=s["sym"], contract=s["contract"],
            qty=int(s["order_qty"]), multiplier=Decimal(str(s["multiplier"])), tick=Decimal(str(s["tick"])),
            threshold_pct=Decimal(str(p["threshold_pct"])), stop_pt=Decimal(str(p["stop_pt"])),
            decision_time=_t(p["decision_time"]), entry_time=_t(p["entry_time"]), entry_grace_s=float(p["entry_grace_s"]), stop_until=_t(p["stop_until"]),
            exit_mode=p["exit_mode"],
            exit_submit_auction=_t(p["exit_submit_time_auction"]),
            exit_submit_continuous=_t(p["exit_submit_time_continuous"]),
            exit_retry_time=_t(p["exit_retry_time"]), auction_fill_time=_t(p["auction_fill_time"]),
            end_time=_t(p["end_time"]), start_not_before=_t(p["start_not_before"]),
            cost_pt=Decimal(str(p["cost_pt_roundtrip"])), sim_slippage_ticks=Decimal(str(p["sim_slippage_ticks"])),
            price_max_age_s=float(p["price_max_age_s"]), poll_interval_s=float(p["poll_interval_s"]),
            fill_poll_interval_s=float(p["fill_poll_interval_s"]), fill_poll_max_s=float(p["fill_poll_max_s"]),
            risk=data["risk"], backtest=data.get("backtest", {}),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.exit_mode not in EXIT_MODES:
            raise ValueError(f"exit_mode 는 {EXIT_MODES} 중 하나여야 합니다: {self.exit_mode!r}")
        if self.qty <= 0 or self.threshold_pct <= 0 or self.stop_pt <= 0 or self.tick <= 0:
            raise ValueError("수량·임계값·손절폭·틱은 0 보다 커야 합니다.")
        order = [self.start_not_before, self.decision_time, self.entry_time, self.stop_until,
                 self.exit_submit_auction, self.exit_retry_time, self.auction_fill_time, self.end_time]
        if order != sorted(order) or len(set(order)) != len(order):
            raise ValueError("시각은 start_not_before < decision < entry < stop_until < 종가단일가 제출 < 재시도 < 체결 < 종료 순이어야 합니다.")
        if not self.entry_time > self.decision_time:
            raise ValueError("진입 시각은 판단 시각보다 늦어야 합니다.")
        if not self.exit_submit_continuous > self.entry_time:
            raise ValueError("연속매매 청산 시각은 진입 시각보다 늦어야 합니다.")
