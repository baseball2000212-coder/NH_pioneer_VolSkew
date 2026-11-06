"""합성 분봉 만들기 — 테스트 전용."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from sessions import KST
from strategy_core import Bar

PARAMS = json.loads((Path(__file__).resolve().parents[1] / "strategy.json").read_text(encoding="utf-8"))
PARAMS["costs"]["round_trip_usd"] = 3.5      # 테스트 기대값 고정(설정 파일의 비용과 무관)


def kst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=KST)


def flat_day(day=(2026, 11, 4), start=(8, 0), end=(23, 59), price=4000.0, wiggle=0.5):
    """start~end 분봉을 가격 주변에서 ±wiggle 로 흔들며 만든 평평한 하루. 반환: {분봉시각: Bar}"""
    bars = {}
    t = kst(*day, *start)
    last = kst(*day, *end)
    i = 0
    while t <= last:
        # 게이트 집계 창(10:00~15:55)에는 종가를 ±0.01 로 번갈아 흔들어 RV 가 0 이 되지 않게 합니다(고저 범위는 그대로).
        noise = 0.01 if (10 * 60 <= t.hour * 60 + t.minute < 15 * 60 + 55 and i % 2) else 0.0
        bars[t] = Bar(t, price, price + wiggle, price - wiggle, price + noise)
        t += timedelta(minutes=1)
        i += 1
    return bars


def put(bars, t, o=None, h=None, l=None, c=None):
    b = bars[t]
    bars[t] = Bar(t, b.o if o is None else o, b.h if h is None else h, b.l if l is None else l, b.c if c is None else c)


def lift(bars, start, delta):
    """start 이후(포함) 모든 봉을 delta 만큼 평행 이동 — 돌파 뒤 가격이 범위 위에 머무는 하루를 만들 때 씁니다."""
    from strategy_core import Bar
    for t in sorted(bars):
        if t >= start:
            b = bars[t]
            bars[t] = Bar(t, b.o + delta, b.h + delta, b.l + delta, b.c + delta)


def breakout_up(bars, t=None):
    """17:00 에 범위 고가(4000.5)+1틱(4000.6)을 위로 뚫고, 이후 가격은 4001 근처(중간값 4000.0 위)에 머뭅니다."""
    t = t or kst(2026, 11, 4, 17, 0)
    lift(bars, t, 1.0)
    put(bars, t, o=4000.5, h=4000.7, l=4000.2, c=4000.6)
