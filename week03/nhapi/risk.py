"""리스크 규칙: 보유 한도, 일일 손실 한도, 세션 끝 정리, 킬 스위치.

모든 전략 실행 프로세스가 지나야 하는 안전장치입니다. 값은 `strategy.json` 의 `risk` 항목에서 읽고
코드에 숫자를 박지 않습니다.

- 최대 보유 수량: 새 진입으로 보유 절댓값이 한도를 넘으면 막습니다.
- 일일 손실 한도: 남은 한도가 **1회 손절 금액보다 작으면** 신규 진입을 막습니다(한도를 넘겨 잃지 않도록).
- 세션 끝 정리: 정리 시각까지 남은 분이 기준 이하이면 청산해야 합니다.
- 킬 스위치: `STOP` 파일이 생기거나 Ctrl+C 를 받으면 켜집니다.
  종료 순서는 미체결 취소 → 포지션 정리 → 기록 마감입니다(`shutdown_sequence`).
"""
from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .journal import Journal


@dataclass(frozen=True)
class RiskLimits:
    """리스크 한도. 모든 값은 설정(strategy.json)에서 옵니다."""

    max_position: int
    daily_loss_limit: Decimal
    session_end_flat_minutes: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RiskLimits":
        """`strategy.json` 의 `risk` 사전에서 만듭니다. 칸이 빠지면 KeyError 입니다."""
        return cls(
            max_position=int(data["max_position"]),
            daily_loss_limit=Decimal(str(data["daily_loss_limit"])),
            session_end_flat_minutes=int(data["session_end_flat_minutes"]),
        )


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


class KillSwitch:
    """멈춤 신호. `STOP` 파일이 있거나 Ctrl+C 를 받으면 켜집니다."""

    def __init__(self, stop_file: Path | str | None = None) -> None:
        self.stop_file = Path(stop_file) if stop_file else None
        self._event = threading.Event()
        self.reason = ""

    def trigger(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
        self._event.set()

    def triggered(self) -> bool:
        if self._event.is_set():
            return True
        if self.stop_file is not None and self.stop_file.exists():
            self.trigger(f"STOP 파일 발견: {self.stop_file.name}")
            return True
        return False

    def install_sigint(self) -> None:
        """Ctrl+C 를 멈춤 신호로 바꿉니다. 두 번째 Ctrl+C 는 평소처럼 KeyboardInterrupt 입니다."""

        def handler(signum: int, frame: Any) -> None:
            if self._event.is_set():
                raise KeyboardInterrupt
            self.trigger("Ctrl+C")

        signal.signal(signal.SIGINT, handler)


class RiskManager:
    """신규 진입 허용 여부와 세션 끝 정리 여부를 판정합니다."""

    def __init__(self, limits: RiskLimits, kill: KillSwitch | None = None) -> None:
        self.limits = limits
        self.kill = kill or KillSwitch()

    def can_enter(
        self,
        side: str,
        qty: int,
        *,
        position: int,
        realized_pnl: Decimal,
        one_stop_loss: Decimal,
    ) -> Decision:
        """신규 진입을 해도 되는지.

        Args:
            side: "buy" 또는 "sell".
            qty: 진입 수량(양수).
            position: 현재 순보유(매수 +, 매도 −).
            realized_pnl: 오늘 실현 손익(손실이면 음수).
            one_stop_loss: 1회 손절 금액(양수).
        """
        if self.kill.triggered():
            return Decision(False, f"킬 스위치: {self.kill.reason}")
        if side not in ("buy", "sell"):
            return Decision(False, f"알 수 없는 방향: {side!r}")
        if qty <= 0:
            return Decision(False, "수량은 1 이상이어야 합니다.")
        after = position + qty if side == "buy" else position - qty
        if abs(after) > self.limits.max_position and abs(after) > abs(position):
            return Decision(False, f"최대 보유 수량 {self.limits.max_position} 초과 (진입 후 {abs(after)})")
        loss = -min(realized_pnl, Decimal(0))
        remaining = self.limits.daily_loss_limit - loss
        if remaining <= 0:
            return Decision(False, "일일 손실 한도에 도달했습니다.")
        if remaining < one_stop_loss:
            return Decision(False, f"남은 손실 한도({remaining})가 1회 손절 금액({one_stop_loss})보다 작습니다.")
        return Decision(True)

    def must_flatten(self, minutes_to_end: float | None) -> bool:
        """세션 끝 정리 시각이 되었는지. `minutes_to_end` 는 정리 시각까지 남은 분(0 이하면 지남)."""
        if minutes_to_end is None:
            return False
        return minutes_to_end <= 0


@dataclass(frozen=True)
class StepResult:
    name: str
    ok: bool
    error: str = ""


def shutdown_sequence(
    *,
    cancel_all: Callable[[], Any],
    flatten: Callable[[], Any],
    close_journal: Callable[[], Any],
    journal: Journal | None = None,
) -> list[StepResult]:
    """종료 순서: 미체결 취소 → 포지션 정리 → 기록 마감. 한 단계가 실패해도 다음 단계를 계속합니다."""
    results: list[StepResult] = []
    for name, step in (
        ("cancel_all", cancel_all),
        ("flatten", flatten),
        ("close_journal", close_journal),
    ):
        try:
            step()
            results.append(StepResult(name, True))
            if journal is not None and name != "close_journal":
                journal.event("shutdown_step", name, step=name, ok=True)
        except Exception as exc:  # noqa: BLE001 — 실패해도 남은 단계를 시도해야 합니다.
            results.append(StepResult(name, False, f"{type(exc).__name__}: {exc}"))
            if journal is not None and name != "close_journal":
                journal.error("shutdown_step", step=name, msg=f"{type(exc).__name__}: {exc}", action="continue")
    return results
