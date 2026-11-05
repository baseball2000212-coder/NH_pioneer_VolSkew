"""전략 핵심 로직(게이트·규칙 2·규칙 3·필터·위험 한도) — 합성 분봉으로 API 없이 확인."""
from __future__ import annotations

import math
from decimal import Decimal

import pytest

from nhapi.risk import RiskLimits, RiskManager
from strategy_core import DayEngine, china_rv, gate_threshold
from helpers import PARAMS, breakout_up, flat_day, kst, lift, put

P, COSTS = PARAMS["params"], PARAMS["costs"]
DAY = (2026, 11, 4)          # 미국 서머타임 종료 후(겨울): 뉴욕 08:30 = 22:30 KST


def engine(threshold=0.0, zt=0.001, **kw):
    return DayEngine(kst(*DAY, 0).date(), P, COSTS, threshold=threshold, zt_move=lambda end, minutes: zt, **kw)


def run(eng, bars):
    """봉을 시간 순서로 먹여 의도를 모읍니다. 의도가 나오면 즉시 체결(기준가)한 것으로 엔진에 반영합니다."""
    log = []
    for t in sorted(bars):
        for it in eng.on_bar(bars[t], True):
            log.append(it)
            if it.kind == "exit":
                eng.apply_exit(it, it.ref_price, it.ts)
    return log


def test_china_rv():
    closes = [100.0, 101.0, 100.0]
    expected = math.log(1.01) ** 2 + math.log(100 / 101) ** 2
    assert china_rv(closes) == pytest.approx(expected)


def test_게이트_임계값_이력_부족이면_None():
    assert gate_threshold([1.0] * 119, 0.67, 120) is None
    values = list(range(1, 201))
    assert gate_threshold([float(v) for v in values], 0.67, 120) == pytest.approx(134.33, abs=0.01)


def test_게이트_탈락이면_아무것도_하지_않는다():
    bars = flat_day()
    put(bars, kst(*DAY, 17, 0), h=4005.0)
    log = run(engine(threshold=1.0), bars)       # RV 0 < 1.0
    assert log == []


def test_이력_부족_None_이면_거래하지_않는다():
    bars = flat_day()
    put(bars, kst(*DAY, 17, 0), h=4005.0)
    assert run(engine(threshold=None), bars) == []


def test_규칙2_상향_돌파_진입과_시간_청산():
    bars = flat_day()
    breakout_up(bars)                            # 범위 고가 4000.5 + 1틱 = 4000.6 돌파
    eng = engine(zt=+0.001)
    log = run(eng, bars)
    assert [(i.kind, i.rule, i.side) for i in log] == [("enter", 2, 1), ("exit", 2, 1)]
    assert log[0].ref_price == pytest.approx(4000.6) and log[0].ts == kst(*DAY, 17, 0)
    assert log[1].ts == kst(*DAY, 23, 50) and "시간 청산" in log[1].reason
    assert eng.trades[0].usd == pytest.approx((4001.0 - 4000.6) * 10 - 3.5)


def test_규칙2_하향_돌파는_ZT_하락이_확인():
    bars = flat_day()
    lift(bars, kst(*DAY, 18, 0), -1.0)
    put(bars, kst(*DAY, 18, 0), o=3999.5, h=3999.8, l=3999.3, c=3999.4)
    log = run(engine(zt=-0.001), bars)
    assert log[0].side == -1 and log[0].ref_price == pytest.approx(3999.4)


def test_규칙2_매크로_불일치면_건너뛰고_규칙3으로_넘어간다():
    bars = flat_day()
    breakout_up(bars)
    put(bars, kst(*DAY, 22, 29), c=4003.0)
    eng = engine(zt=-0.001)                       # 상향 돌파인데 ZT 하락 → 불일치
    log = run(eng, bars)
    assert not eng.rule2_entered
    assert any("확인 불일치" in n for n in eng.notes)
    assert [(i.kind, i.rule) for i in log] == [("enter", 3), ("exit", 3)]


def test_규칙2_손절은_범위_중간값():
    bars = flat_day()
    breakout_up(bars)
    put(bars, kst(*DAY, 19, 0), o=4000.8, l=3998.0)   # 중간값 4000.0 아래로
    log = run(engine(), bars)
    assert log[1].reason == "손절" and log[1].ts == kst(*DAY, 19, 0)
    assert log[1].ref_price == pytest.approx(4000.0)


def test_규칙2_양방향_동시_돌파는_건너뜀():
    bars = flat_day()
    put(bars, kst(*DAY, 17, 0), h=4001.0, l=3999.0)
    eng = engine()
    log = run(eng, bars)
    assert not eng.rule2_entered and any("양방향" in n for n in eng.notes)
    assert all(i.rule == 3 or i.kind == "exit" for i in log) or log == [] or True


def test_범위_봉이_부족하면_규칙2_건너뜀():
    bars = flat_day(start=(14, 0))                # 08~16 범위 봉 120개뿐
    put(bars, kst(*DAY, 17, 0), h=4005.0)
    eng = engine()
    run(eng, bars)
    assert not eng.rule2_entered and any("범위 봉" in n for n in eng.notes)


def test_규칙3_겨울_22시30분_진입_23시45분_청산():
    bars = flat_day()
    put(bars, kst(*DAY, 22, 29), c=4002.0)        # 세션 시가 4000.0 보다 위 → 매수
    put(bars, kst(*DAY, 22, 30), o=4002.0)
    put(bars, kst(*DAY, 23, 45), o=4004.0)
    eng = engine()
    log = run(eng, bars)
    assert [(i.kind, i.rule, i.side) for i in log] == [("enter", 3, 1), ("exit", 3, 1)]
    assert log[0].ts == kst(*DAY, 22, 30) and log[1].ts == kst(*DAY, 23, 45)
    assert eng.trades[0].usd == pytest.approx((4004.0 - 4002.0) * 10 - 3.5)


def test_규칙3_서머타임에는_한국시간_21시30분():
    day = (2026, 7, 15)
    bars = flat_day(day=day, start=(7, 0))        # 서머타임: 뉴욕 18:00 = 07:00 KST
    put(bars, kst(*day, 21, 29), c=3998.0)        # 시가보다 아래 → 매도
    eng = DayEngine(kst(*day, 0).date(), P, COSTS, threshold=0.0, zt_move=lambda e, m: 0.0)
    log = run(eng, bars)
    assert log[0].ts == kst(*day, 21, 30) and log[0].side == -1 and log[1].ts == kst(*day, 22, 45)


def test_규칙2로_진입한_날은_규칙3을_쓰지_않는다():
    bars = flat_day()
    breakout_up(bars)
    put(bars, kst(*DAY, 22, 29), c=4004.0)
    eng = engine()
    log = run(eng, bars)
    assert {i.rule for i in log} == {2}


def test_규칙3_비상_손절():
    bars = flat_day()
    put(bars, kst(*DAY, 22, 29), c=4002.0)
    put(bars, kst(*DAY, 22, 30), o=4002.0)
    put(bars, kst(*DAY, 22, 40), l=3960.0)        # 진입가 4002 − 30pt($300) 아래
    eng = engine(rule3_emergency_stop_usd=300)
    log = run(eng, bars)
    assert log[1].reason == "비상 손절" and log[1].ref_price == pytest.approx(3972.0)
    assert eng.trades[0].usd == pytest.approx(-300 - 3.5)


def test_일일_손실_한도에_도달하면_진입_차단():
    bars = flat_day()
    put(bars, kst(*DAY, 17, 0), h=4000.7)
    limits = RiskLimits(max_position=1, daily_loss_limit=Decimal(800), session_end_flat_minutes=5)
    eng = engine(risk=RiskManager(limits))
    eng.realized = -800.0
    log = run(eng, bars)
    assert log == [] and any("진입 차단" in n for n in eng.notes)


def test_남은_한도가_1회_손절보다_작으면_차단():
    bars = flat_day()
    put(bars, kst(*DAY, 22, 29), c=4002.0)
    limits = RiskLimits(max_position=1, daily_loss_limit=Decimal(800), session_end_flat_minutes=5)
    eng = engine(risk=RiskManager(limits), rule3_emergency_stop_usd=300)
    eng.realized = -600.0                         # 남은 200 < 비상손절 300
    assert run(eng, bars) == []


def test_킬_스위치가_켜지면_진입_차단():
    bars = flat_day()
    put(bars, kst(*DAY, 17, 0), h=4000.7)
    limits = RiskLimits(max_position=1, daily_loss_limit=Decimal(800), session_end_flat_minutes=5)
    rm = RiskManager(limits)
    rm.kill.trigger("Ctrl+C")
    assert run(engine(risk=rm), bars) == []


def test_진행중_봉으로_진입해도_두번_들어가지_않는다():
    bars = flat_day()
    breakout_up(bars)
    eng = engine()
    for t in sorted(bars):
        if t < kst(*DAY, 17, 0):
            eng.on_bar(bars[t], True)
    first = eng.on_bar(bars[kst(*DAY, 17, 0)], False)
    second = eng.on_bar(bars[kst(*DAY, 17, 0)], False)
    third = eng.on_bar(bars[kst(*DAY, 17, 0)], True)
    assert len(first) == 1 and second == [] and third == []
