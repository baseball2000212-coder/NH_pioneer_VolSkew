"""시각 도우미. 이 패키지의 모든 시각은 한국시간(KST)입니다."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: 한국은 서머타임이 없어 고정 오프셋으로 충분합니다.
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    """지금, 한국시간. 컴퓨터의 시간대 설정과 무관합니다."""
    return datetime.now(tz=KST)


def to_kst(moment: datetime) -> datetime:
    """시각을 한국시간으로. 시간대가 없는 값은 한국시간으로 봅니다."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=KST)
    return moment.astimezone(KST)


def iso_ms(moment: datetime | None = None) -> str:
    """기록에 남기는 시각 글자. `2026-10-27T09:01:02.345+09:00`."""
    return to_kst(moment or now_kst()).isoformat(timespec="milliseconds")


def yyyymmdd(day: date | datetime | None = None) -> str:
    """API 가 받는 날짜 여덟 자리."""
    if day is None:
        day = now_kst()
    return day.strftime("%Y%m%d")
