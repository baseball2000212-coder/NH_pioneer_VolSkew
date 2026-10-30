"""품목별세부정보 기반 세션 판정.

거래시간은 **품목마다 다릅니다.** 같은 주간 안에도 개장이 08:45 인 품목과 09:00 인 품목이 함께 있을 수
있으므로 하드코딩하지 않고 `product-detail` 응답의 그 품목 줄을 씁니다.
시간표는 `08:45~15:45`, `18:00~익일06:00` 같은 글자로 오며 한국시간입니다.

참고: 운영진 예시 앱 `backend/sessions/hours.py`, `backend/domestic_futureoption/domestic_hours.py`.

이 모듈은 순수 함수만 있습니다. 시각은 인자로 받습니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from .parsers import NIGHT_MARK, ProductRow
from .timeutil import KST

SessionName = Literal["day", "night"]
State = Literal["open", "opening_auction", "closing_auction", "closed"]

_RANGE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*~\s*(?:익일)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$"
)

#: 품목 이름 → 품목코드 두 자리 (예시 앱 `PRODUCT_CODES`). 응답에 코드가 없고 이름만 있어서 둡니다.
PRODUCT_CODES: dict[str, str] = {
    "코스피200선물": "01",
    "코스피200옵션": "01",
    "코스피200위클리옵션": "09",
    "미니 코스피200선물": "05",
    "미니 코스피200옵션": "05",
    "코스닥150선물": "06",
    "코스닥150옵션": "06",
    "코스닥150위클리옵션": "AJ",
    "KRX300선물": "08",
    "코스피200변동성지수선물(VKOSPI)": "04",
    "미국달러선물(USD)": "75",
    "유로선물(EUR)": "77",
    "엔선물(JPY)": "76",
    "위안선물(CNH)": "78",
    "3년국채선물(KTB)": "65",
    "10년국채선물(10KTB)": "67",
    "5년국채선물(5KTB)": "66",
    "30년국채선물(30KTB)": "70",
    "코스닥 글로벌 지수": "AG",
    "KRX반도체 섹터지수선물": "AL",
}


@dataclass(frozen=True)
class TimeRange:
    """하루 안의 시간 구간. 닫는 시각이 여는 시각보다 앞이면 자정을 넘습니다."""

    open: time
    close: time

    @property
    def next_day(self) -> bool:
        return self.close < self.open

    def starting(self, day: date) -> tuple[datetime, datetime]:
        """`day` 에 여는 구간의 (시작, 끝)."""
        start = datetime.combine(day, self.open, KST)
        end = datetime.combine(day + timedelta(days=1 if self.next_day else 0), self.close, KST)
        return start, end

    def contains(self, moment: datetime, day: date) -> bool:
        """`day` 에 연 구간 안에 `moment` 가 드는지. 끝 시각은 들지 않습니다."""
        start, end = self.starting(day)
        return start <= moment < end


def parse_range(text: Any) -> TimeRange | None:
    """`HH:MM~HH:MM` 글자 → 구간. 비었거나 못 읽거나 `00:00~00:00`(없음)이면 None."""
    found = _RANGE.match(str(text or ""))
    if found is None:
        return None
    h1, m1, s1, h2, m2, s2 = found.groups()
    try:
        opened = time(int(h1), int(m1), int(s1 or 0))
        closed = time(int(h2), int(m2), int(s2 or 0))
    except ValueError:
        return None
    if opened == closed == time(0, 0):
        return None
    return TimeRange(opened, closed)


def product_code_of(name: str) -> str | None:
    """품목 이름의 품목코드. 야간 줄은 `(야간)` 을 떼고 찾습니다. 표에 없으면 None."""
    body = str(name or "").strip()
    if body.startswith(NIGHT_MARK):
        body = body[len(NIGHT_MARK):].strip()
    return PRODUCT_CODES.get(body)


@dataclass(frozen=True)
class SessionWindow:
    """한 품목·한 세션의 시간표."""

    session: SessionName
    trading: TimeRange | None
    opening_auction: TimeRange | None
    closing_auction: TimeRange | None

    @classmethod
    def from_row(cls, row: ProductRow) -> "SessionWindow":
        return cls(
            session="night" if row.is_night else "day",
            trading=parse_range(row.trading),
            opening_auction=parse_range(row.opening_auction),
            closing_auction=parse_range(row.closing_auction),
        )

    def state_at(self, moment: datetime) -> State:
        """이 세션 기준으로 `moment` 의 상태. 자정을 넘는 구간은 어제 연 것도 봅니다."""
        moment = moment.astimezone(KST)
        for day in (moment.date(), moment.date() - timedelta(days=1)):
            # 단일가 구간은 거래 구간 안에 들어 있을 수 있으므로 먼저 봅니다(예: 종가 단일가 15:35~15:45).
            for rng, state in (
                (self.opening_auction, "opening_auction"),
                (self.closing_auction, "closing_auction"),
                (self.trading, "open"),
            ):
                if rng is not None and rng.contains(moment, day):
                    return state
        return "closed"

    def flatten_deadline(self, day: date, minutes_before: int) -> datetime | None:
        """세션 끝 정리 시각: 거래 종료 `minutes_before` 분 전. 종가 단일가가 있으면 그 시작보다 늦지 않게 합니다.

        단일가 구간에는 시장가 체결이 미뤄지므로 그 전에 정리해야 합니다.
        """
        if self.trading is None:
            return None
        end = self.trading.starting(day)[1]
        deadline = end - timedelta(minutes=minutes_before)
        if self.closing_auction is not None:
            # 종가 단일가는 거래가 끝나는 날(야간이면 다음 날)에 있습니다.
            auction_start = datetime.combine(end.date(), self.closing_auction.open, KST)
            if auction_start > end:
                auction_start -= timedelta(days=1)
            deadline = min(deadline, auction_start)
        return deadline


def windows_for(rows: list[ProductRow], product_code: str, *, is_option: bool = False) -> dict[SessionName, SessionWindow]:
    """품목코드의 선물(또는 옵션) 줄에서 주간·야간 시간표를 만듭니다. 없는 세션은 빠집니다."""
    found: dict[SessionName, SessionWindow] = {}
    for row in rows:
        if product_code_of(row.name) != product_code:
            continue
        if ("옵션" in row.name) != is_option:
            continue
        window = SessionWindow.from_row(row)
        found[window.session] = window
    return found


def current_session(
    windows: dict[SessionName, SessionWindow], moment: datetime
) -> tuple[SessionName | None, State]:
    """지금 어느 세션이 열려 있는지. 둘 다 닫혀 있으면 (None, "closed")."""
    for name in ("day", "night"):
        window = windows.get(name)  # type: ignore[arg-type]
        if window is None:
            continue
        state = window.state_at(moment)
        if state != "closed":
            return name, state  # type: ignore[return-value]
    return None, "closed"


def minutes_to_session_end(window: SessionWindow, moment: datetime, minutes_before: int = 0) -> float | None:
    """세션 끝 정리 시각까지 남은 분. 이미 지났으면 0 이하. 시간표가 없으면 None.

    자정을 넘는 세션은 어제 연 것도 확인합니다.
    """
    moment = moment.astimezone(KST)
    for day in (moment.date(), moment.date() - timedelta(days=1)):
        if window.trading is not None and window.trading.contains(moment, day):
            deadline = window.flatten_deadline(day, minutes_before)
            if deadline is not None:
                return (deadline - moment).total_seconds() / 60
    return None
