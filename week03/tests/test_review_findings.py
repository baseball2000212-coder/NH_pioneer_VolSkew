"""리뷰어 재현 테스트 F1~F5 를 **고친 동작**으로 뒤집은 회귀 테스트.

원본(review/week03_findings/code/tests/test_review_findings.py)은 위험한 현재 동작을 assert 했습니다(통과 = 버그 재현).
여기서는 같은 시나리오에서 안전한 동작을 assert 합니다. NH 에 접속하지 않습니다(가짜 client).
"""
from __future__ import annotations

from datetime import datetime

from helpers import breakout_up, flat_day, kst
from fakes import DAY, categories, records, runner_with
from nhapi.risk import KillSwitch
from strategy_core import Position


# ---------------------------------------------------------------- F1: 킬 스위치가 켜져도 청산 주문은 나가고 잔고를 확인한다
def test_F1_킬스위치가_켜져도_마감_청산은_나가고_잔고확인을_한다(tmp_path):
    kill = KillSwitch()
    t = runner_with(tmp_path, flat_day(end=(22, 0)), start=(23, 0), kill=kill)
    base = t.runner._replay

    def hooked(bars):
        base(bars)
        t.runner.engine.position = Position(3, 1, 4000.0, None, kst(*DAY, 22, 30))
        t.client.net = 1                      # 계좌에도 매수 1계약이 있음
        t.executor.net = 1
        kill.trigger("STOP 파일")

    t.runner._replay = hooked
    result = t.runner.run()
    assert len(t.client.sells()) == 1         # 청산 주문이 킬 스위치에 막히지 않음
    assert t.client.net == 0                  # 계좌 정리됨
    assert "not_flat" not in categories(t.journal)
    assert any(r.get("name") == "final_positions" for r in records(t.journal))   # 마감 잔고 확인을 함
    trades = [r for r in records(t.journal) if r["kind"] == "trade"]
    assert len(trades) == 1 and "마감 정리" in trades[0]["reason"]
    assert result["final_position"] == 0 and "킬 스위치" in result["reason"]


# ---------------------------------------------------------------- F2: 체결 확인 실패 → 잔고 대사로 체결 확인, 세션 계속, 마감에 청산
def test_F2_진입_체결확인_시간초과면_잔고로_대사해_보유로_이어가고_청산한다(tmp_path):
    bars = flat_day()
    breakout_up(bars)
    t = runner_with(tmp_path, bars, client_kw={"fill": False})
    result = t.runner.run()
    assert [o["trd_div_cd"] for o in t.client.orders if o["qt_div_cd"] == "1"] == ["1", "2"]   # 매수 진입, 매도 청산
    assert t.client.net == 0                                                   # 계좌 정리됨
    fills = [r for r in records(t.journal) if r["kind"] == "fill"]
    assert fills[0]["action"] == "enter" and "잔고 대사" in fills[0]["note"]   # 가격 추정 체결로 표시
    assert not t.executor.halted
    assert result["reason"] == "마감 시각" and result["trades"] == 1          # 17:00 에 멈추지 않고 세션을 끝까지 운영
    trade = [r for r in records(t.journal) if r["kind"] == "trade"][0]
    assert trade["exit_ts"] == kst(*DAY, 23, 50).isoformat()
    assert "not_flat" not in categories(t.journal) and "order_state" not in categories(t.journal)


# ---------------------------------------------------------------- F3: 체결 확인 중 조회 예외 → 죽지 않고, '체결 없음'으로 보지 않는다
def test_F3_체결확인_조회가_예외여도_run_은_살아있고_잔고로_대사한다(tmp_path):
    bars = flat_day()
    breakout_up(bars)
    t = runner_with(tmp_path, bars, client_kw={"history_raises": True})
    result = t.runner.run()                    # 예외가 밖으로 나오지 않음
    cats = categories(t.journal)
    assert "fill_query" in cats                # 조회 실패를 기록(체결 없음으로 보지 않음)
    assert t.client.net == 0 and result["trades"] == 1
    recs = records(t.journal)
    assert any(x.get("name") == "shutdown_step" for x in recs)
    assert any(x["kind"] == "run" and x.get("phase") == "end" for x in recs)


# ---------------------------------------------------------------- F4: 조회 장애 뒤 지난 봉의 신호로 늦게 진입하지 않는다
def test_F4_분봉_조회_장애가_끝나도_몇분_전_돌파로_진입하지_않는다(tmp_path):
    bars = flat_day()
    breakout_up(bars)                                      # 17:00 돌파
    down = [(kst(*DAY, 16, 58), kst(*DAY, 17, 9), "EGW033")]
    t = runner_with(tmp_path, bars, down=down)
    t.runner.run()
    recs = records(t.journal)
    assert not [x for x in recs if x["kind"] == "order" and x.get("stage") == "sent"]   # 주문 없음
    skipped = [x for x in recs if x.get("name") == "stale_entry_skipped"]
    assert skipped and skipped[0]["bar_ts"] == kst(*DAY, 17, 0).isoformat() and skipped[0]["age_s"] > 120
    assert t.runner.engine.rule2_entered and t.runner.engine.position is None            # 놓친 신호로 처리


# ---------------------------------------------------------------- F5: 거부된 청산은 백오프·상한·경보
def test_F5_청산이_거부되면_간격을_늘려_다시_내고_경보하며_마감잔고를_확인한다(tmp_path):
    t = runner_with(tmp_path, flat_day(), start=(23, 44))
    base = t.runner._replay

    def hooked(bars):
        base(bars)
        t.runner.engine.position = Position(3, 1, 4000.0, None, kst(*DAY, 22, 30))
        t.runner.engine.gate_pass = True
        t.client.net = 1
        t.executor.net = 1
        t.client.reject_orders = True

    t.runner._replay = hooked
    t.runner.run()
    sells = t.client.sells()
    assert 5 <= len(sells) < 30                                   # 원본은 23:45~23:55 동안 수십 번(>50)
    cats = categories(t.journal)
    assert "exit_rejected" in cats and "not_flat" in cats          # 3회째 경보, 끝내 못 정리하면 마감 경보
    assert any("exit_rejected" in a for a in t.alerts) and any("not_flat" in a for a in t.alerts)
    sent_ts = [datetime.fromisoformat(x["ts"]) for x in records(t.journal) if x["kind"] == "order" and x.get("stage") == "sent"]
    assert max(sent_ts) < kst(*DAY, 23, 59)                       # 마감 정리 시한(23:58) 뒤로는 내지 않음
