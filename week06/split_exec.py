"""6주차(전략 + 위험관리): 여러 계약을 조각으로 나눠 내는 데 쓰는 순수 함수. API·시계를 모른다.

- `scale_schedule`  : 계약 수 n 과 조각 비중·시각(초)으로 조각 목록을 만든다 (합 = n, 각 조각 ≥ 1).
- `walk_book`       : 10단계 호가를 쓸어 n 계약 시장가의 평균 체결가 (그림자 비용 계산용).
- `slip_ticks`      : 체결가가 보낸 순간 중간가보다 몇 틱 불리했는지 (+ = 불리).
- `ChildGuard`      : 조각을 보내기 전 검사(시세 신선도·스프레드·마감 시각)와 보낸 뒤 판정(슬리피지 상한·거부 횟수).
근거·값: research/desks/kq150/risk_w06/exec/결과_exec.md (호가 충격·회복·감쇠 측정).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Sequence


@dataclass(frozen=True)
class Child:
    offset_s: float   # 진입 기준 시각에서 몇 초 뒤 (그 시각 '이후' 에 보낸다)
    qty: int


def scale_schedule(n: int, weights: Sequence[float], offsets_s: Sequence[float]) -> list[Child]:
    """n 계약을 비중대로 나눈다. 반올림 나머지는 앞 조각부터 1씩 더한다(앞에 몰기 = 감쇠 비용 줄이기).
    n 이 조각 수보다 작으면 앞쪽 조각만 1계약씩 쓴다. 수량 0 조각은 뺀다."""
    if n <= 0:
        return []
    if len(weights) != len(offsets_s) or not weights:
        raise ValueError("weights 와 offsets_s 길이가 같아야 하고 비어 있으면 안 됩니다.")
    if any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError("비중은 0 이상이고 합이 0 보다 커야 합니다.")
    if list(offsets_s) != sorted(offsets_s):
        raise ValueError("조각 시각은 오름차순이어야 합니다.")
    total = float(sum(weights))
    base = [int(Decimal(str(n * w / total)).to_integral_value(ROUND_FLOOR)) for w in weights]
    left = n - sum(base)
    i = 0
    while left > 0:
        base[i % len(base)] += 1
        left -= 1
        i += 1
    return [Child(float(o), q) for o, q in zip(offsets_s, base) if q > 0]


def walk_book(levels: Sequence[tuple[Decimal, int]], qty: int) -> tuple[Decimal | None, int]:
    """(가격, 잔량) 을 좋은 가격부터 쓸어 qty 계약 평균 체결가. 다 못 채우면 채운 만큼의 평균과 채운 수량."""
    got, cost = 0, Decimal(0)
    for price, size in levels:
        if price is None or price <= 0 or size <= 0:
            continue
        take = min(size, qty - got)
        cost += price * take
        got += take
        if got >= qty:
            break
    return (cost / got if got else None), got


def slip_ticks(side: str, fill: Decimal, bid: Decimal | None, ask: Decimal | None, tick: Decimal) -> Decimal | None:
    """+ 면 불리. 매수는 (체결가 − 중간가)/틱, 매도는 (중간가 − 체결가)/틱."""
    if fill is None or bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    s = 1 if side == "buy" else -1
    return s * (fill - mid) / tick


@dataclass(frozen=True)
class GuardConfig:
    """조각 감시 값. 근거: research/desks/kq150/risk_w06/exec/결과_exec.md 6절(15:15~15:35 매초 분위수)."""
    max_age_s: float              # 체결 시세가 이보다 오래되면 조각을 보내지 않는다(5초, 기존 A 와 같음)
    book_max_age_s: float         # 호가(KB)가 이보다 오래되면 잔량 대신 사다리 일정으로
    depth_levels: int             # 조각 크기 = 보내는 순간 1~L 호가 잔량(2)
    half_life_s: float            # 우리가 먹은 잔량이 돌아오는 반감기(2.78초) — 다음 조각 크기에서 덜 회복된 몫을 뺀다.
                                  #   모의서버 체결은 실제 호가를 안 건드려 피드에 안 보이므로 모형으로 뺀다. 실계좌면 0
    gap_s: float                  # 다음 조각까지 간격(5초)
    spread_first_ticks: Decimal   # 첫 조각: 스프레드 ≥ 이 값(6틱, p99)이면
    spread_first_wait_s: float    #   최대 이만큼(1초) 기다렸다가 그냥 보낸다(감쇠가 더 비쌈)
    spread_next_ticks: Decimal    # 다음 조각: 스프레드 ≥ 이 값(4틱, p95)이면
    spread_next_wait_s: float     #   1초씩 최대 이만큼(5초) 기다리고, 그래도 넓으면 남은 조각을 접는다
    slow_slip_ticks: Decimal      # 조각 슬리피지 > 이 값(3틱)이면 남은 조각을 1호가만큼·slow_gap_s 간격으로
    slow_gap_s: float             # (10초)
    halt_slip_ticks: Decimal      # 조각 슬리피지 > 이 값(5틱)이면 남은 조각 멈추고 경보
    window_s: float               # 진입 기준 시각에서 이 초가 지나면 남은 조각을 접는다(알파 감쇠, 30초)
    max_rejects: int              # 거부·미체결이 이만큼 쌓이면 남은 조각을 접는다(2)
    exit_gap_s: float             # 손절 분할 청산 간격(1초, 1·2호가 잔량씩)

    @classmethod
    def from_dict(cls, d) -> "GuardConfig":
        D = lambda k: Decimal(str(d[k]))  # noqa: E731
        return cls(float(d["max_age_s"]), float(d["book_max_age_s"]), int(d["depth_levels"]), float(d["half_life_s"]),
                   float(d["gap_s"]), D("spread_first_ticks"), float(d["spread_first_wait_s"]), D("spread_next_ticks"),
                   float(d["spread_next_wait_s"]), D("slow_slip_ticks"), float(d["slow_gap_s"]), D("halt_slip_ticks"),
                   float(d["window_s"]), int(d["max_rejects"]), float(d["exit_gap_s"]))


def spread_ticks(bid: Decimal | None, ask: Decimal | None, tick: Decimal) -> Decimal | None:
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        return None
    return (ask - bid) / tick


def depth_child(levels: Sequence[tuple[Decimal, int]], n_levels: int, remaining: int, own_left: float = 0.0) -> int:
    """다음 조각 크기 = 1~n_levels 호가 잔량 − 우리가 먹고 아직 덜 돌아온 몫, 1 이상 remaining 이하."""
    depth = sum(q for _, q in list(levels)[:n_levels] if q > 0)
    return max(1, min(remaining, int(depth - own_left)))


def own_left(prev: list[tuple[float, int]], now_s: float, half_life_s: float) -> float:
    """앞 조각들(보낸 시각 초, 수량)이 먹은 잔량 중 아직 안 돌아온 몫(지수 회복)."""
    if half_life_s <= 0:
        return 0.0      # 실계좌: 호가에 이미 우리 체결이 반영돼 있으니 빼지 않는다(half_life_s=0)
    return sum(q * 0.5 ** (max(0.0, now_s - t) / half_life_s) for t, q in prev)
