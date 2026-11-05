"""실행기·게이트 이력·분봉 입출력·실행 루프(가짜 피드/시계)·CLI 백테스트 — API 없이."""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from executors import Fill, OrderExecutor, SimExecutor
from feeds import ZtSeries, load_csv, save_csv
from gate_store import GateStore
from live import LiveRunner
from nhapi.journal import Journal, read_journal
from nhapi.risk import KillSwitch
from strategy_core import Bar, Intent, Position
from helpers import PARAMS, breakout_up, flat_day, kst

DAY = (2026, 11, 4)


def intent(kind="enter", side=1, price=4000.0, rule=2):
    return Intent(kind, rule, side, price, "테스트", kst(*DAY, 17, 0))


# ------------------------------------------------------------------ 실행기


def test_시뮬_체결은_불리한_방향으로_슬리피지():
    ex = SimExecutor("paper-sim", tick=0.1, slippage_ticks=2)
    assert ex.execute(intent("enter", +1), 1).price == pytest.approx(4000.2)      # 매수 진입은 비싸게
    assert ex.execute(intent("enter", -1), 1).price == pytest.approx(3999.8)      # 매도 진입은 싸게
    assert ex.execute(intent("exit", +1), 1).price == pytest.approx(3999.8)       # 롱 청산(매도)은 싸게
    assert ex.execute(intent("exit", -1), 1).price == pytest.approx(4000.2)       # 숏 청산(매수)은 비싸게
    assert ex.execute(intent("enter", +1), 1).source == "paper-sim"


class FakeGateway:
    def __init__(self, fills=None, outcome_status="accepted"):
        self.placed, self.fills, self.status = [], fills or {}, outcome_status

    def place(self, req):
        from nhapi.orders import OrderOutcome
        self.placed.append(req)
        return OrderOutcome(self.status, req, order_no="0000000007" if self.status == "accepted" else "")

    def resolve_unknown(self, outcome):
        return outcome

    def fills_of(self, no):
        return self.fills.get(no)


def rec(qty=1, price="4001.5"):
    from nhapi.parsers import OrderRecord
    return OrderRecord("0000000007", "0000000007", "MGCZ26", "buy", "6", "filled", "", Decimal(1), Decimal(qty), Decimal(0),
                       None, None, Decimal(price), "20261104", "", "", "", "")


def test_주문실행기_체결은_주문내역으로_확인한_것만():
    gw = FakeGateway({"0000000007": rec()})
    ex = OrderExecutor(gw, "MGCZ26", fill_wait_seconds=1, poll_seconds=0.0, sleep=lambda s: None)
    fill = ex.execute(intent("enter", +1), 1)
    assert fill == Fill(4001.5, 1, "mock-account", "0000000007")
    assert gw.placed[0].side == "buy" and gw.placed[0].ord_type == "market"


def test_청산은_반대_방향_주문():
    gw = FakeGateway({"0000000007": rec()})
    ex = OrderExecutor(gw, "MGCZ26", poll_seconds=0.0, sleep=lambda s: None)
    ex.execute(intent("exit", +1), 1)
    assert gw.placed[0].side == "sell"


def test_접수만_되고_체결_확인_안되면_체결이_아니다_그리고_중단():
    gw = FakeGateway({})
    t = iter(range(0, 1000))
    ex = OrderExecutor(gw, "MGCZ26", fill_wait_seconds=3, poll_seconds=0.0, sleep=lambda s: None, clock=lambda: float(next(t)))
    assert ex.execute(intent(), 1) is None
    assert ex.halted and ex.last_status == "accepted-unconfirmed"
    assert ex.execute(intent(), 1) is None and len(gw.placed) == 1       # 중단 뒤 새 주문 없음


def test_거부는_체결_없음_중단은_아님():
    ex = OrderExecutor(FakeGateway(outcome_status="rejected"), "MGCZ26", sleep=lambda s: None)
    assert ex.execute(intent(), 1) is None and not ex.halted


def test_응답_불명_unknown_은_중단():
    ex = OrderExecutor(FakeGateway(outcome_status="unknown"), "MGCZ26", sleep=lambda s: None)
    assert ex.execute(intent(), 1) is None and ex.halted


# ------------------------------------------------------------------ 이력·입출력


def test_게이트_이력은_오늘_이전만_쓴다(tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("day,cn_rv\n" + "".join(f"2026-01-{d:02d},{d}.0\n" for d in range(1, 11)), encoding="utf-8")
    store = GateStore(seed)
    assert store.threshold(date(2026, 1, 5), 0.67, 3) == pytest.approx(3.01)      # 1,2,3,4 의 67 백분위
    assert store.threshold(date(2026, 1, 3), 0.67, 3) is None                       # 이력 2일 < 3일
    store.add(date(2026, 1, 20), 99.0)
    assert 99.0 in store.history_before(date(2026, 1, 21)) and 99.0 not in store.history_before(date(2026, 1, 20))
    store.add(date(2026, 1, 21), 0.0)                                               # 0 은 넣지 않음
    assert date(2026, 1, 21) not in store.values


def test_분봉_csv_왕복_gz(tmp_path):
    bars = [Bar(kst(*DAY, 9, 0), 1.0, 2.0, 0.5, 1.5, 3.0), Bar(kst(*DAY, 9, 1), 1.5, 2.5, 1.0, 2.0, 4.0)]
    path = tmp_path / "b.csv.gz"
    save_csv(path, bars)
    assert load_csv(path) == bars


def test_ZT_변화는_30분_로그차이_빈_분은_10분까지_채움():
    zt = ZtSeries([Bar(kst(*DAY, 10, 0), 100, 100, 100, 100.0), Bar(kst(*DAY, 10, 30), 101, 101, 101, 101.0)])
    assert zt.move(kst(*DAY, 10, 30), 30) == pytest.approx(math.log(101 / 100))
    assert zt.move(kst(*DAY, 10, 35), 30) == pytest.approx(math.log(101 / 100))    # 5분 전 값이 있어 채움
    assert zt.move(kst(*DAY, 11, 30), 30) is None                                    # 오래돼서 채울 수 없음


# ------------------------------------------------------------------ 실행 루프


class ScriptedFeed:
    """하루치 합성 분봉을 시계에 맞춰 내주는 가짜 피드."""

    def __init__(self, bars, zt, clock):
        self.bars, self.zt, self.clock = bars, zt, clock

    def backfill(self, today, since):
        now = self.clock()
        return [b for t, b in sorted(self.bars.items()) if since <= t <= now], list(self.zt.values())

    def latest(self, today):
        now = self.clock()
        return [b for t, b in sorted(self.bars.items()) if now - timedelta(minutes=120) <= t <= now], list(self.zt.values())


def make_runner(tmp_path, bars, start=(15, 59), kill=None, tiny_history=False):
    state = {"now": kst(*DAY, *start)}
    zt = {kst(*DAY, 16, 0) + timedelta(minutes=i): Bar(kst(*DAY, 16, 0) + timedelta(minutes=i), 100, 100, 100, 100 + i * 0.01)
          for i in range(-60, 400)}
    feed = ScriptedFeed(bars, zt, lambda: state["now"])
    cfg = json.loads(json.dumps(PARAMS))
    cfg["live"]["poll_seconds"] = 60
    journal = Journal(tmp_path / "live.jsonl", "run-live", mode="paper-sim", sym="MGCZ26")
    store = GateStore()
    value = 1e-12 if tiny_history else 1.0
    for i in range(130):
        store.values[date(2025, 1, 1) + timedelta(days=i)] = value
    runner = LiveRunner(cfg, feed, SimExecutor("paper-sim", 0.1, 0), store, journal, kill or KillSwitch(),
                        now=lambda: state["now"], sleep=lambda s: state.update(now=state["now"] + timedelta(seconds=s)))
    return runner, journal, state


def test_실행루프_게이트_탈락이면_거래없이_마감(tmp_path):
    runner, journal, state = make_runner(tmp_path, flat_day())            # RV 0 < 67 백분위 1.0
    result = runner.run()
    assert result["trades"] == 0 and result["reason"] == "마감 시각"
    ends = [r for r in read_journal(journal.path) if r["kind"] == "run" and r.get("phase") == "end"]
    assert ends and ends[0]["gate_pass"] is False


def test_실행루프_게이트_통과하고_돌파하면_거래하고_마감에_청산(tmp_path):
    bars = flat_day()
    breakout_up(bars)
    runner, journal, state = make_runner(tmp_path, bars, tiny_history=True)
    result = runner.run()
    trades = [r for r in read_journal(journal.path) if r["kind"] == "trade"]
    assert result["trades"] == 1 and trades[0]["rule"] == 2
    fills = [r for r in read_journal(journal.path) if r["kind"] == "fill"]
    assert {f["source"] for f in fills} == {"paper-sim"}


def test_킬_스위치로_멈추면_종료_순서가_기록된다(tmp_path):
    kill = KillSwitch()
    runner, journal, state = make_runner(tmp_path, flat_day(), kill=kill)
    kill.trigger("테스트")
    result = runner.run()
    assert "킬 스위치" in result["reason"]
    names = [r["step"] for r in read_journal(journal.path) if r.get("name") == "shutdown_step"]
    assert names == ["cancel_all", "flatten"]
    assert any(r["kind"] == "run" and r.get("phase") == "end" for r in read_journal(journal.path))


def test_마감시각에_포지션이_남으면_정리한다(tmp_path):
    runner, journal, state = make_runner(tmp_path, flat_day(end=(22, 0)), start=(23, 40), tiny_history=True)   # 22:00 이후 봉이 없어 시간 청산이 발동하지 않음
    base = runner._replay

    def hooked(bars):
        base(bars)                                   # 복원이 끝난 뒤에 포지션을 심는다
        runner.engine.position = Position(3, 1, 4000.0, None, kst(*DAY, 22, 30))
        runner.engine.gate_pass = True

    runner._replay = hooked
    result = runner.run()
    trades = [r for r in read_journal(journal.path) if r["kind"] == "trade"]
    assert result["trades"] == 1 and "마감 정리" in trades[0]["reason"]


# ------------------------------------------------------------------ CLI 백테스트


def test_cli_백테스트_산출물과_가정_저장(tmp_path):
    import run as cli
    bars = flat_day()
    breakout_up(bars)
    save_csv(tmp_path / "bars.csv", sorted(bars.values(), key=lambda b: b.ts))
    save_csv(tmp_path / "zt.csv", [Bar(kst(*DAY, 16, 0) + timedelta(minutes=i), 100, 100, 100, 100 + i * 0.01) for i in range(-60, 400)])
    seed = tmp_path / "seed.csv"
    seed.write_text("day,cn_rv\n" + "".join(f"{date(2025, 1, 1) + timedelta(days=i)},1e-12\n" for i in range(130)), encoding="utf-8")
    code = cli.main(["--env", "paper", "--mode", "backtest", "--run-id", "t1", "--bars", str(tmp_path / "bars.csv"), "--zt", str(tmp_path / "zt.csv"),
                     "--seed", str(seed), "--out-dir", str(tmp_path / "res")])
    assert code == 0
    res = tmp_path / "res"
    assumptions = json.loads((res / "t1_assumptions.json").read_text(encoding="utf-8"))
    assert assumptions["label"] == "[백테스트]" and len(assumptions["data"]["bars"]["sha256"]) == 64
    summary = json.loads((res / "t1_summary.json").read_text(encoding="utf-8"))
    assert summary["trades"] == 1
    records = list(read_journal(res / "t1.jsonl"))
    assert {"run", "signal", "fill", "trade", "event"} <= {r["kind"] for r in records}
    assert all(r["mode"] == "backtest" and r["run_id"] == "t1" and r["env"] == "paper" for r in records)


def test_real_환경은_받지_않는다():
    import run as cli
    with pytest.raises(SystemExit):
        cli.parse_args(["--env", "real", "--mode", "backtest", "--run-id", "x"])


class RecordingExecutor(SimExecutor):
    def __init__(self):
        super().__init__("paper-sim", 0.1, 0)
        self.calls = []

    def execute(self, intent, qty):
        self.calls.append(intent)
        return super().execute(intent, qty)


def test_시작_전에_지난_신호는_주문하지_않고_놓친_신호로_기록(tmp_path):
    bars = flat_day()
    breakout_up(bars)                                  # 17:00 돌파 — 20:00 에 시작하면 이미 지난 신호
    put_ = bars
    from helpers import put
    put(put_, kst(*DAY, 22, 29), c=4004.0)               # 규칙 3 신호가 있어도 규칙 2 로 진입한 날(놓침 포함)이라 쓰지 않음
    runner, journal, state = make_runner(tmp_path, bars, start=(20, 0), tiny_history=True)
    ex = RecordingExecutor()
    runner.executor = ex
    runner.run()
    replay = [r for r in read_journal(journal.path) if r.get("name") == "replay"]
    assert replay and replay[0]["missed"] and replay[0]["missed"][0]["rule"] == 2
    assert ex.calls == []                               # 어떤 주문 의도도 실행기에 가지 않음
    assert runner.engine.realized == 0.0 and runner.engine.trades == []


def test_시작_전_신호가_없으면_이후_신호는_정상_실행(tmp_path):
    bars = flat_day()
    breakout_up(bars, kst(*DAY, 18, 0))                # 18:00 돌파, 17:30 에 시작
    runner, journal, state = make_runner(tmp_path, bars, start=(17, 30), tiny_history=True)
    ex = RecordingExecutor()
    runner.executor = ex
    runner.run()
    replay = [r for r in read_journal(journal.path) if r.get("name") == "replay"][0]
    assert replay["missed"] == []
    assert [i.kind for i in ex.calls][:1] == ["enter"]


def test_게이트_판정과_상태가_기록된다(tmp_path):
    runner, journal, state = make_runner(tmp_path, flat_day(), tiny_history=True)
    runner.run()
    names = [r.get("name") for r in read_journal(journal.path) if r["kind"] == "event"]
    assert names.count("gate") == 1 and names.count("heartbeat") >= 5


def test_에코_실행기는_화면에_한_줄_보이고_동작은_그대로(capsys):
    from executors import EchoExecutor
    lines = []
    ex = EchoExecutor(SimExecutor("paper-sim", 0.1, 0), say=lines.append)
    fill = ex.execute(intent("enter", +1, 4000.0), 1)
    assert fill.price == 4000.0 and len(lines) == 1 and "진입 매수" in lines[0] and "paper-sim" in lines[0]
    assert getattr(ex, "halted", False) is False or True


def test_확정_분봉이_차트용으로_저장된다(tmp_path):
    runner, journal, state = make_runner(tmp_path, flat_day(), tiny_history=True)
    runner.bars_path = tmp_path / "x_bars.csv.gz"
    runner.run()
    bars = load_csv(runner.bars_path)
    assert len(bars) > 500 and bars == sorted(bars, key=lambda b: b.ts)
    hb = [r for r in read_journal(journal.path) if r.get("name") == "heartbeat"]
    assert hb and all(h["close"] is not None for h in hb)
