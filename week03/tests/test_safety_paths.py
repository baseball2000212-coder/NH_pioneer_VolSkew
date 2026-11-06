"""리뷰 수정의 새 안전 경로 테스트 — API 없이(가짜 client·피드·시계)."""
from __future__ import annotations

import os
import signal
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import live as live_mod
from executors import EXIT_BACKOFF_SECONDS, OrderExecutor, cancel_open_orders, net_position
from gate_store import GateStore
from live import LiveFeed, flatten_account, make_alarm, prepare_account
from nhapi.errors import BusinessError
from nhapi.journal import Journal
from nhapi.overseas import OverseasGateway, OverseasOrderRequest
from nhapi.risk import Decision, KillSwitch
from strategy_core import Intent, Position
from helpers import breakout_up, flat_day, kst
from fakes import DAY, FakeClient, categories, gateway_for, records, runner_with


def intent(kind="enter", side=1, price=4000.0, rule=2):
    return Intent(kind, rule, side, price, "테스트", kst(*DAY, 17, 0))


def clocked(tmp_path, **client_kw):
    """가짜 시계 위의 FakeClient + 게이트웨이 + 실행기."""
    state = {"now": kst(*DAY, 17, 0)}
    journal = Journal(tmp_path / "j.jsonl", "t", mode="paper-order", sym="MGCZ26", clock=lambda: state["now"])
    client = FakeClient(journal, **client_kw)
    kill = KillSwitch()
    gw = gateway_for(client, state, kill)
    alerts = []
    ex = OrderExecutor(gw, "MGCZ26", fill_wait_seconds=20, poll_seconds=1,
                       sleep=lambda s: state.update(now=state["now"] + timedelta(seconds=s)),
                       clock=lambda: state["now"].timestamp(), alarm=make_alarm(journal, alerts.append, "MGCZ26"))
    return SimpleNamespace(state=state, journal=journal, client=client, kill=kill, gw=gw, ex=ex, alerts=alerts)


# ------------------------------------------------------------------ 1. 킬 스위치·halted 는 청산을 막지 않는다


def test_halted_여도_청산은_내고_진입은_막는다(tmp_path):
    c = clocked(tmp_path)
    c.client.net, c.ex.net = 1, 1
    c.ex.halted = True
    assert c.ex.execute(intent("enter"), 1) is None and c.client.orders == []
    fill = c.ex.execute(intent("exit", +1), 1)
    assert fill is not None and fill.source == "mock-account" and c.client.net == 0


def test_킬_스위치는_진입만_막고_reduce_only_청산은_통과(tmp_path):
    c = clocked(tmp_path)
    c.kill.trigger("STOP")
    blocked = c.gw.place(OverseasOrderRequest("MGCZ26", "buy", 1))
    assert blocked.status == "rejected" and blocked.not_sent and c.client.orders == []
    ok = c.gw.place(OverseasOrderRequest("MGCZ26", "sell", 1, reduce_only=True))
    assert ok.status == "accepted" and len(c.client.orders) == 1
    assert "reduce_only" not in c.client.orders[0]          # 주문 내용에는 실리지 않음


# ------------------------------------------------------------------ 2. 종료 정리는 계좌 잔고 기준


def test_엔진은_무포지션이어도_계좌에_남은_보유를_정리한다(tmp_path):
    kill = KillSwitch()
    t = runner_with(tmp_path, flat_day(), start=(20, 0), kill=kill)
    base = t.runner._replay

    def hooked(bars):
        base(bars)
        t.client.net = 1                     # 엔진은 모르는 보유(크래시·대사 실패 등)
        kill.trigger("테스트")

    t.runner._replay = hooked
    result = t.runner.run()
    assert len(t.client.sells()) == 1 and t.client.net == 0 and result["final_position"] == 0
    assert any(r.get("name") == "flatten_order" for r in records(t.journal))


def test_청산이_두번_거부돼도_재시도로_정리한다(tmp_path):
    kill = KillSwitch()
    t = runner_with(tmp_path, flat_day(), start=(20, 0), kill=kill, client_kw={"reject_first": 2})
    base = t.runner._replay

    def hooked(bars):
        base(bars)
        t.client.net = 1
        kill.trigger("테스트")

    t.runner._replay = hooked
    t.runner.run()
    flat_orders = [r for r in records(t.journal) if r.get("name") == "flatten_order"]
    assert [r["status"] for r in flat_orders] == ["rejected", "rejected", "accepted"]
    assert t.client.net == 0 and "not_flat" not in categories(t.journal)


def test_시한까지_정리하지_못하면_멈추고_경보한다(tmp_path):
    state = {"now": kst(*DAY, 23, 55)}
    journal = Journal(tmp_path / "j.jsonl", "t", mode="paper-order", sym="MGCZ26", clock=lambda: state["now"])
    client = FakeClient(journal, reject_orders=True)
    client.net = 1
    gw = gateway_for(client, state, KillSwitch())
    alerts = []
    left = flatten_account(gw, "MGCZ26", journal=journal, alarm=make_alarm(journal, alerts.append, "MGCZ26"),
                           now=lambda: state["now"], sleep=lambda s: state.update(now=state["now"] + timedelta(seconds=s)),
                           deadline=kst(*DAY, 23, 58), max_attempts=30)
    assert left == 1
    assert state["now"] >= kst(*DAY, 23, 58) and len(client.sells()) <= 8      # 백오프 5·10·20·40·60초
    assert any("flatten_retry" in a for a in alerts)


def test_정리_단계가_예외여도_마지막_잔고는_확인하고_경보한다(tmp_path):
    kill = KillSwitch()
    t = runner_with(tmp_path, flat_day(), start=(20, 0), kill=kill)
    base = t.runner._replay

    def hooked(bars):
        base(bars)
        t.client.net = 1
        kill.trigger("테스트")

    t.runner._replay = hooked
    original = live_mod.flatten_account
    live_mod.flatten_account = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("정리 코드 버그"))
    try:
        t.runner.run()
    finally:
        live_mod.flatten_account = original
    recs = records(t.journal)
    assert any(r.get("name") == "final_positions" for r in recs)
    cats = categories(t.journal)
    assert "not_flat" in cats and "shutdown" in cats                  # 정리 실패 + 잔고 남음 모두 경보
    assert any("not_flat" in a for a in t.alerts)
    assert any(r["kind"] == "run" and r.get("phase") == "end" for r in recs)


# ------------------------------------------------------------------ 3. 결과 모름·미확인 → 잔고 대사


def test_resolve_unknown_은_약_10초_동안_다시_본다(tmp_path):
    c = clocked(tmp_path, unknown_orders=True, history_hidden=3)
    out = c.gw.place(OverseasOrderRequest("MGCZ26", "buy", 1))
    assert out.status == "unknown"
    t0 = c.state["now"]
    got = c.gw.resolve_unknown(out)
    assert got.status == "accepted" and got.code == "RECONCILED"
    assert c.state["now"] - t0 == timedelta(seconds=3)                  # 1초 간격으로 4번째에 찾음


def test_resolve_unknown_조회가_계속_실패해도_예외없이_unknown(tmp_path):
    c = clocked(tmp_path, unknown_orders=True, history_raises=True)
    out = c.gw.place(OverseasOrderRequest("MGCZ26", "buy", 1))
    t0 = c.state["now"]
    got = c.gw.resolve_unknown(out)
    assert got.status == "unknown" and "조회 실패" in got.message
    assert c.state["now"] - t0 == timedelta(seconds=9)


def test_결과모름_진입은_잔고에_보이면_체결로_본다(tmp_path):
    c = clocked(tmp_path, unknown_orders=True, history_hidden=10 ** 6)
    fill = c.ex.execute(intent("enter", +1, 4000.6), 1)
    assert fill is not None and "잔고 대사" in fill.note and fill.price == 4000.0      # 잔고 평균가
    assert c.ex.net == 1 and not c.ex.halted


def test_결과모름_진입이_잔고에_없으면_미체결_취소하고_멈추지_않는다(tmp_path):
    c = clocked(tmp_path, unknown_orders=True, apply_orders=False, history_hidden=10 ** 6)
    assert c.ex.execute(intent("enter"), 1) is None
    assert not c.ex.halted and c.ex.last_status == "reconciled-not-filled" and c.ex.net == 0
    assert any(r.get("name") == "order_reconciled" for r in records(c.journal))


def test_잔고로도_모호하면_진입을_멈추고_경보(tmp_path):
    c = clocked(tmp_path, fill=False, positions_raise=True)
    assert c.ex.execute(intent("enter"), 1) is None
    assert c.ex.halted and any("order_state" in a for a in c.alerts)
    assert c.ex.execute(intent("enter"), 1) is None and len(c.client.buys()) == 1


def test_체결조회_예외는_체결없음이_아니다_다시_보고_확인한다(tmp_path):
    c = clocked(tmp_path, history_raise_first=2)
    fill = c.ex.execute(intent("enter"), 1)
    assert fill is not None and fill.note == "" and fill.price == 4000.0       # 주문내역으로 확인한 체결
    assert categories(c.journal).count("fill_query") == 2


def test_실행_중_예외가_나도_종료_정리를_한다(tmp_path):
    class Boom:
        halted = False

        def execute(self, it, qty):
            raise RuntimeError("실행기 버그")

    bars = flat_day()
    breakout_up(bars)
    t = runner_with(tmp_path, bars, executor_factory=lambda gw, state, alarm: Boom())
    result = t.runner.run()
    assert result["reason"].startswith("오류: RuntimeError")
    recs = records(t.journal)
    assert [r["step"] for r in recs if r.get("name") == "shutdown_step"] == ["cancel_all", "flatten"]
    assert any(r["kind"] == "run" and r.get("phase") == "end" for r in recs)
    assert any("runner" in a for a in t.alerts)
    assert not [r for r in recs if r["kind"] == "trade"] and t.runner.engine.position is None   # 체결된 적 없는 진입은 거래로 남기지 않음


# ------------------------------------------------------------------ 4. 오래된 데이터


def test_보유중_분봉이_끊기면_현재가로_손절하고_경보(tmp_path):
    bars = flat_day()
    breakout_up(bars)                                          # 17:00 매수, 손절 4000.0
    down = [(kst(*DAY, 18, 0), kst(*DAY, 19, 0), "EGW033")]
    t = runner_with(tmp_path, bars, down=down, price_fn=lambda now: 3999.0 if now >= kst(*DAY, 18, 3) else 4001.0)
    t.runner.run()
    assert any("stale_feed" in a for a in t.alerts)
    trade = [r for r in records(t.journal) if r["kind"] == "trade"][0]
    assert trade["reason"] == "손절" and kst(*DAY, 18, 3).isoformat() <= trade["exit_ts"] < kst(*DAY, 18, 4).isoformat()
    first_price = min(t.feed.price_calls)
    assert kst(*DAY, 18, 1) <= first_price <= kst(*DAY, 18, 2, ) + timedelta(seconds=10)   # 60초 넘게 끊긴 뒤부터


def test_보유가_없으면_분봉이_끊겨도_현재가를_묻지_않는다(tmp_path):
    down = [(kst(*DAY, 18, 0), kst(*DAY, 19, 0), "EGW033")]
    t = runner_with(tmp_path, flat_day(), down=down, price_fn=lambda now: 4000.0)
    t.runner.run()
    assert t.feed.price_calls == [] and "stale_feed" not in categories(t.journal)


# ------------------------------------------------------------------ 5. 거부된 청산 백오프


def test_거부된_청산은_간격을_늘려_다시_내고_3번째에_경보():
    state = {"t": 0.0}
    alerts = []

    class RejectGateway:
        journal = None

        def __init__(self):
            self.placed = []

        def place(self, req):
            from nhapi.orders import OrderOutcome
            self.placed.append((state["t"], req))
            return OrderOutcome("rejected", req, code="E9999")

        def positions(self):                                   # 계좌에는 아직 매수 1계약
            return [SimpleNamespace(sym="MGCZ26", side="buy", qty=Decimal(1), avg_price=Decimal("4000"))]

    gw = RejectGateway()
    ex = OrderExecutor(gw, "MGCZ26", sleep=lambda s: None, clock=lambda: state["t"], alarm=lambda c, m="", **f: alerts.append(c))
    ex.net = 1
    for second in range(0, 200, 5):
        state["t"] = float(second)
        ex.execute(intent("exit", +1), 1)
    times = [t for t, _ in gw.placed]
    assert times[:4] == [0.0, 10.0, 30.0, 70.0]                     # 10 → 20 → 40 → 60초
    assert all(req.reduce_only for _, req in gw.placed)
    assert alerts == ["exit_rejected"] and EXIT_BACKOFF_SECONDS[-1] == 60.0


def test_청산이_3번_실패한_뒤_계좌가_이미_정리됐으면_엔진을_맞춘다():
    class Gw:
        journal = None

        def place(self, req):
            from nhapi.orders import OrderOutcome
            return OrderOutcome("rejected", req, code="E9999")

        def positions(self):
            return []                                          # 계좌는 이미 0 (다른 경로로 정리됨)

    state = {"t": 0.0}
    ex = OrderExecutor(Gw(), "MGCZ26", sleep=lambda s: None, clock=lambda: state["t"], alarm=lambda *a, **k: None)
    ex.net = 1
    fills = []
    for second in (0, 10, 30):
        state["t"] = float(second)
        fills.append(ex.execute(intent("exit", +1, 3990.0), 1))
    assert fills[:2] == [None, None] and fills[2] is not None and "가격 추정" in fills[2].note and ex.net == 0


# ------------------------------------------------------------------ 6. 경보 채널


def test_경보는_파일에_쌓이고_텔레그램은_환경변수가_있을_때만(tmp_path):
    import httpx
    import run as cli

    sent = []
    original = httpx.post
    saved = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    httpx.post = lambda url, **kw: sent.append((url, kw))
    try:
        for k in saved:
            os.environ.pop(k, None)
        alert = cli.make_alert(tmp_path / "logs")
        alert("[MGC] not_flat: 테스트")
        assert sent == []
        os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "test-token", "42"
        alert("[MGC] stale_feed: 테스트")
        assert len(sent) == 1 and sent[0][1]["data"]["chat_id"] == "42" and "stale_feed" in sent[0][1]["data"]["text"]
    finally:
        httpx.post = original
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    files = list((tmp_path / "logs").glob("ALERTS-*.txt"))
    text = files[0].read_text(encoding="utf-8")
    assert len(files) == 1 and "not_flat" in text and "stale_feed" in text


def test_알림이_실패해도_기록은_남고_예외는_없다(tmp_path):
    journal = Journal(tmp_path / "j.jsonl", "t", mode="paper-order", sym="MGCZ26")

    def broken(text):
        raise OSError("디스크 가득")

    make_alarm(journal, broken, "MGCZ26")("not_flat", "테스트")
    assert categories(journal) == ["not_flat"]


# ------------------------------------------------------------------ 7. 호출 제한·조회량


def test_EGW020_이면_10_20_40초_쉰다(tmp_path):
    down = [(kst(*DAY, 17, 0), kst(*DAY, 17, 3), "EGW020")]
    t = runner_with(tmp_path, flat_day(), down=down)
    t.runner.run()
    waits = [r["wait_s"] for r in records(t.journal) if r.get("name") == "rate_limit_cooldown"]
    assert waits[:3] == [10.0, 20.0, 40.0] and set(waits[3:]) <= {40.0}
    in_window = [x for x in t.feed.latest_calls if kst(*DAY, 17, 0) <= x < kst(*DAY, 17, 3)]
    assert len(in_window) <= 8                                   # 쉬지 않으면 5초마다 36번


class ChartClient:
    """해외 차트(분) 가짜: 최근 req_qty 개 봉을 돌려줍니다. 현재가도 답합니다."""

    def __init__(self, clock):
        self.clock, self.calls = clock, []

    def query(self, api_id, payload, **kw):
        self.calls.append((api_id, dict(payload) if isinstance(payload, dict) else payload, kw))
        if api_id == "overseas_price":
            return SimpleNamespace(body={"Occurs1": [{"occurs1_sym": "MGCZ26", "occurs1_last_pric": "4012.3"}]})
        now = self.clock().replace(second=0, microsecond=0)
        n = int(payload["req_qty"])
        rows = []
        for i in range(n):
            t = now - timedelta(minutes=i)
            rows.append({"occurs1_ref_dt": t.strftime("%Y%m%d"), "occurs1_exch_tm": t.strftime("%H%M%S"), "occurs1_open_pric": "4000",
                         "occurs1_high_pric": "4001", "occurs1_low_pric": "3999", "occurs1_close_pric": "4000", "occurs1_cum_trd_qty": "1"})
        return SimpleNamespace(body={"Occurs1": rows})


def test_최신_분봉은_적게_받고_빈_분이_생기면_넓게_다시_받는다():
    state = {"now": kst(*DAY, 17, 0)}
    client = ChartClient(lambda: state["now"])
    feed = LiveFeed(client, "FCME", "MGCZ26", "FCME", "ZTZ26", latest_qty=10)
    feed._seen = {"MGCZ26": kst(*DAY, 16, 59), "ZTZ26": kst(*DAY, 16, 59)}
    mgc, _ = feed.latest(date(*DAY))
    assert [c[1]["req_qty"] for c in client.calls] == ["10", "10"] and all(c[2].get("retries") == 1 for c in client.calls)
    assert len(mgc) == 10
    client.calls.clear()
    state["now"] += timedelta(minutes=30)                         # 30분 공백(조회 장애 뒤)
    mgc, _ = feed.latest(date(*DAY))
    assert [c[1]["req_qty"] for c in client.calls] == ["10", "300", "10", "300"] and len(mgc) == 300
    assert feed.price() == pytest.approx(4012.3)
    assert client.calls[-1][1] == {"Iccurs1": [{"iccurs1_exch_cd": "FCME", "iccurs1_sym": "MGCZ26"}]}


# ------------------------------------------------------------------ 8. 게이트 이력


def test_게이트_이력은_시드와_실시간_파일을_합치고_run_id_별로_쓴다(tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("day,cn_rv\n2026-10-29,1.0\n2026-10-30,2.0\n", encoding="utf-8")
    res = tmp_path / "results"
    res.mkdir()
    (res / "gate_history_live.csv").write_text("day,cn_rv\n2026-11-02,3.0\n", encoding="utf-8")      # 옛 이름
    (res / "gate_history-ord-20261103.csv").write_text("day,cn_rv\n2026-11-03,4.0\n", encoding="utf-8")
    store = GateStore(seed)
    files = store.load_live(res)
    assert [p.name for p in files] == ["gate_history_live.csv", "gate_history-ord-20261103.csv"]
    assert sorted(store.values) == [date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3)]
    assert store.missing_weekdays(date(2026, 11, 5), lookback_days=7) == [date(2026, 11, 4)]
    store.add(date(2026, 11, 5), 5.0)
    store.save(res / "gate_history-ord-20261105.csv")
    again = GateStore(seed)
    again.load_live(res)
    assert again.values[date(2026, 11, 5)] == 5.0


def test_실행_시작에_빠진_날을_경고한다(tmp_path):
    t = runner_with(tmp_path, flat_day(), start=(23, 50))
    t.runner.run()
    gap = [e for e in records(t.journal) if e.get("category") == "gate_history_gap"]
    assert gap and "2026-11-03" in gap[0]["days"] and gap[0]["action"] == "warn"


# ------------------------------------------------------------------ 9. 시작 시 남은 포지션 (--flatten-leftover)


def _prep(tmp_path, net, flatten, open_rows=None):
    state = {"now": kst(*DAY, 15, 0)}
    journal = Journal(tmp_path / "j.jsonl", "t", mode="paper-order", sym="MGCZ26", clock=lambda: state["now"])
    client = FakeClient(journal, open_rows=open_rows)
    client.net = net
    gw = gateway_for(client, state, KillSwitch())
    ok, text = prepare_account(gw, "MGCZ26", flatten_leftover=flatten, journal=journal, alarm=make_alarm(journal, None, "MGCZ26"),
                               now=lambda: state["now"], sleep=lambda s: state.update(now=state["now"] + timedelta(seconds=s)),
                               deadline=kst(*DAY, 23, 58))
    return ok, text, client


def test_시작시_깨끗하면_그대로_시작(tmp_path):
    ok, text, client = _prep(tmp_path, 0, False)
    assert ok and client.orders == []


def test_시작시_잔고가_있으면_기본은_거부(tmp_path):
    ok, text, client = _prep(tmp_path, 1, False)
    assert not ok and "--flatten-leftover" in text and client.orders == []


def test_flatten_leftover_면_정리하고_시작(tmp_path):
    ok, text, client = _prep(tmp_path, -1, True)
    assert ok and client.net == 0 and [o["trd_div_cd"] for o in client.orders] == ["1"]      # 매도 보유 → 매수 청산


def test_flatten_leftover_도_다른_종목은_건드리지_않는다(tmp_path):
    other = [{"occurs1_ord_no": "0000000009", "occurs1_sym": "GCZ26", "occurs1_trd_tp": "1", "occurs1_ord_sts": "3", "occurs1_ord_qty": "1"}]
    ok, text, client = _prep(tmp_path, 0, True, open_rows=other)
    assert not ok and "GCZ26" in text and client.orders == []


# ------------------------------------------------------------------ 10. 자잘한 것


def test_방향을_모르는_미체결은_건너뛰고_나머지는_취소한다():
    logged, cancelled = [], []

    class Gw:
        def open_orders(self):
            rec = lambda no, side: SimpleNamespace(order_no=no, sym="MGCZ26", side=side, remaining_qty=Decimal(1), qty=Decimal(1))
            return [rec("0000000001", ""), rec("0000000002", "buy")]

        def cancel(self, sym, side, qty, no):
            cancelled.append(no)

    assert cancel_open_orders(Gw(), None, lambda c, **f: logged.append((c, f))) == 1
    assert cancelled == ["0000000002"] and logged[0][1]["action"] == "manual_check"


def test_정리_중에는_두번째_Ctrl_C_를_무시한다():
    previous = signal.getsignal(signal.SIGINT)
    try:
        kill = KillSwitch()
        kill.install_sigint()
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert kill.triggered() and kill.reason == "Ctrl+C"
        kill.shielded = True
        handler(signal.SIGINT, None)                           # 예외 없음
        kill.shielded = False
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
    finally:
        signal.signal(signal.SIGINT, previous)


def test_STOP_경로는_스크립트_폴더_기준():
    import run as cli
    a = cli.parse_args(["--env", "paper", "--mode", "paper-sim", "--run-id", "x"])
    assert cli.stop_path(a.stop_file) == cli.HERE / "STOP"
    absolute = str(cli.HERE.parent / "elsewhere" / "STOP")
    assert cli.stop_path(absolute) == cli.HERE.parent / "elsewhere" / "STOP"


def test_순포지션_계산():
    p = lambda sym, side, q: SimpleNamespace(sym=sym, side=side, qty=Decimal(q), avg_price=None)
    assert net_position([p("MGCZ26", "buy", 2), p("MGCZ26", "sell", 1), p("GCZ26", "sell", 5)], "MGCZ26") == 1
    assert net_position([p("MGCZ26", "", 1)], "MGCZ26") is None
