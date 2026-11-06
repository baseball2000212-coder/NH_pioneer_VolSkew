"""시각 도우미 — 모든 봉 시각은 한국시간(KST)이고, 뉴욕 시계 규칙(규칙 3)은 여기서 뉴욕 분으로 바꿉니다.

규칙 3 의 진입·청산은 뉴욕 현지 시계(08:30·09:45)라 서머타임에는 한국시간으로 한 시간 당겨집니다
(겨울 22:30·23:45, 서머타임 21:30·22:45). 규칙 2 와 게이트는 한국시간 고정입니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9), "KST")
NY = ZoneInfo("America/New_York")


def hm(text: str) -> int:
    """'HH:MM' → 자정부터의 분."""
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def kst_minutes(ts: datetime) -> int:
    local = ts.astimezone(KST)
    return local.hour * 60 + local.minute


def ny_minutes(ts: datetime) -> int:
    local = ts.astimezone(NY)
    return local.hour * 60 + local.minute


def ny_is_dst(ts: datetime) -> bool:
    """그 시각에 뉴욕이 서머타임인지(겨울이면 False)."""
    offset = ts.astimezone(NY).dst()
    return bool(offset) and offset != timedelta(0)


def kst_day(ts: datetime):
    return ts.astimezone(KST).date()
