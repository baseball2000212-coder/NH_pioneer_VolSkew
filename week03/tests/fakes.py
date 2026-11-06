"""리뷰 수정 테스트용 가짜 NH 클라이언트·피드·실행 묶음 — 네트워크 없음.

리뷰어 재현 테스트(review/week03_findings/code/tests/test_review_findings.py)의 FakeClient·Feed 를 넓힌 것입니다.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

from executors import OrderExecutor
from gate_store import GateStore
from live import LiveRunner, make_alarm
from nhapi.config import PAPER
from nhapi.errors import BusinessError, GatewayError, TransportError
from nhapi.journal import Journal, read_journal
from nhapi.overseas import OverseasGateway
from nhapi.risk import Decision, KillSwitch
from helpers import PARAMS, kst

DAY = (2026, 11, 4)


class FakeClient:
    """overseas_order 는 접수(주문번호), 주문내역은 체결 줄, 잔고는 순포지션(net)대로 답합니다.

    fill            : 주문내역에 체결 줄을 보여 줄지(False 면 접수만 되고 내역에 체결이 안 보임)
    history_raises  : 주문내역 조회가 늘 EGW033 예외 / history_raise_first: 처음 n 번만 예외
    history_hidden  : 처음 n 번의 주문내역 조회는 빈 결과(접수 직후 내역 지연)
    reject_orders   : 모든 주문 거부 / reject_first: 처음 n 건만 거부
    unknown_orders  : 주문은 서버에 닿지만 응답이 없음(TransportError, sent=None)
    apply_orders    : 신규 주문이 계좌 잔고를 바꾸는지(False 면 접수돼도 체결 안 됨)
    positions_raise : 잔고 조회가 늘 예외
    """

    def __init__(self, journal=None, *, fill=True, history_raises=False, history_raise_first=0, history_hidden=0,
                 reject_orders=False, reject_first=0, unknown_orders=False, apply_orders=True, positions_raise=False,
                 open_rows=None):
        self.config = SimpleNamespace(default_environment=PAPER)
        self.journal = journal
        self.orders = []          # 실제로 '나간' 주문 payload (거부 포함)
        self.accepted = []        # (주문번호, payload)
        self.fill, self.history_raises, self.history_raise_first, self.history_hidden = fill, history_raises, history_raise_first, history_hidden
        self.reject_orders, self.reject_first, self.unknown_orders = reject_orders, reject_first, unknown_orders
        self.apply_orders, self.positions_raise = apply_orders, positions_raise
        self.open_rows = open_rows or []
        self.net = 0              # 계좌 순포지션(MGCZ26)
        self.history_calls = 0
        self._no = 100

    def call(self, api_id, payload):
        assert api_id == "overseas_order"
        self.orders.append(dict(payload))
        if self.reject_orders or (self.reject_first > 0 and payload["qt_div_cd"] == "1"):
            self.reject_first = max(0, self.reject_first - 1)
            raise BusinessError(api_id, "E9999", "거부", {}, "fatal")
        self._no += 1
        number = f"{self._no:010d}"
        self.accepted.append((number, dict(payload)))
        if payload["qt_div_cd"] == "1" and self.apply_orders:
            self.net += int(payload["ord_qty"]) * (1 if payload["trd_div_cd"] == "1" else -1)
        if self.unknown_orders:
            raise TransportError(api_id, "응답 시간 초과", sent=None)
        return SimpleNamespace(body={"ord_no": number, "err_cd": "00000"})

    def paginate(self, api_id, payload, **kw):
        if api_id == "overseas_order-history":
            self.history_calls += 1
            if self.history_raises or self.history_calls <= self.history_raise_first:
                raise GatewayError(api_id, "EGW033", "백엔드 서버와의 연결에 실패했습니다.", {}, "retry")
            if payload.get("inq_div") == "3":
                yield {"Occurs1": list(self.open_rows), "nxt_key": ""}
                return
            rows = []
            if self.history_calls > self.history_hidden:
                for number, p in self.accepted:
                    if p["qt_div_cd"] != "1":
                        continue
                    rows.append({"occurs1_ord_no": number, "occurs1_sym": p["sym"], "occurs1_trd_tp": p["trd_div_cd"],
                                 "occurs1_ord_sts": "6" if self.fill else "3", "occurs1_ord_qty": p["ord_qty"],
                                 "occurs1_exec_qty": p["ord_qty"] if self.fill else "0",
                                 "occurs1_exec_pric": "4000.0" if self.fill else "", "occurs1_ord_rcv_tm": "20261104230000"})
            yield {"Occurs1": rows, "nxt_key": ""}
        elif api_id == "overseas_open-interest":
            if self.positions_raise:
                raise GatewayError(api_id, "EGW033", "백엔드 서버와의 연결에 실패했습니다.", {}, "retry")
            rows = [] if self.net == 0 else [{"occurs1_sym": "MGCZ26", "occurs1_trd_tp": "1" if self.net > 0 else "2",
                                              "occurs1_opint_qty": str(abs(self.net)), "occurs1_avg_pric": "4000.0"}]
            yield {"Occurs1": rows, "nxt_key": ""}
        else:
            raise AssertionError(api_id)

    def sells(self):
        return [o for o in self.orders if o["qt_div_cd"] == "1" and o["trd_div_cd"] == "2"]

    def buys(self):
        return [o for o in self.orders if o["qt_div_cd"] == "1" and o["trd_div_cd"] == "1"]


class Feed:
    """시계에 맞춰 분봉을 내주는 가짜 피드. down=[(시작, 끝, 코드)] 동안 latest 가 예외, price 는 price_fn(now)."""

    def __init__(self, bars, clock, down=(), price_fn=None):
        self.bars, self.clock, self.down, self.price_fn = bars, clock, down, price_fn
        self.latest_calls = []
        self.price_calls = []
        zt0 = kst(*DAY, 6, 0)
        self.zt = [SimpleNamespace(ts=zt0 + timedelta(minutes=i), c=100 + i * 0.001) for i in range(18 * 60)]

    def backfill(self, today, since):
        now = self.clock()
        return [b for t, b in sorted(self.bars.items()) if since <= t <= now], self.zt

    def latest(self, today):
        now = self.clock()
        self.latest_calls.append(now)
        for item in self.down:
            a, b = item[0], item[1]
            code = item[2] if len(item) > 2 else "EGW020"
            if a <= now < b:
                raise GatewayError("overseas_chart-minute", code, "조회 실패", {}, "retry")
        return [b for t, b in sorted(self.bars.items()) if now - timedelta(minutes=300) <= t <= now], self.zt

    def price(self):
        self.price_calls.append(self.clock())
        return None if self.price_fn is None else self.price_fn(self.clock())


def gateway_for(client, state, kill):
    """run.py 와 같은 pre_check(킬 스위치)를 단 게이트웨이. 시계·sleep 은 가짜."""
    return OverseasGateway(client, journal=client.journal,
                           pre_check=lambda r: Decision(not kill.triggered(), "킬 스위치" if kill.triggered() else ""),
                           clock=lambda: state["now"].timestamp(),
                           sleep=lambda s: state.update(now=state["now"] + timedelta(seconds=s)))


def order_exec(gw, state, alarm=None):
    return OrderExecutor(gw, "MGCZ26", fill_wait_seconds=20, poll_seconds=1,
                         sleep=lambda s: state.update(now=state["now"] + timedelta(seconds=s)),
                         clock=lambda: state["now"].timestamp(), alarm=alarm)


def runner_with(tmp_path, bars, *, start=(15, 59), kill=None, client_kw=None, down=(), price_fn=None, executor_factory=None):
    """가짜 시계·피드·클라이언트로 [모의계좌 주문] 실행 루프를 만듭니다. 경보는 alerts 목록에 쌓입니다."""
    state = {"now": kst(*DAY, *start)}
    cfg = json.loads(json.dumps(PARAMS))
    cfg["live"]["poll_seconds"] = 5
    journal = Journal(tmp_path / "j.jsonl", "rev", mode="paper-order", sym="MGCZ26", clock=lambda: state["now"])
    kill = kill or KillSwitch()
    client = FakeClient(journal, **(client_kw or {}))
    gw = gateway_for(client, state, kill)
    alerts = []
    alarm = make_alarm(journal, alerts.append, "MGCZ26")
    store = GateStore()
    for i in range(130):
        store.values[date(2025, 1, 1) + timedelta(days=i)] = 1e-12
    ex = (executor_factory or order_exec)(gw, state, alarm)
    feed = Feed(bars, lambda: state["now"], down, price_fn)
    r = LiveRunner(cfg, feed, ex, store, journal, kill, gateway=gw, mode="paper-order",
                   now=lambda: state["now"], sleep=lambda s: state.update(now=state["now"] + timedelta(seconds=s)), alarm=alarm)
    return SimpleNamespace(runner=r, client=client, journal=journal, state=state, alerts=alerts, feed=feed, gw=gw, executor=ex, kill=kill)


def records(journal):
    return list(read_journal(journal.path))


def errors(journal):
    return [r for r in read_journal(journal.path) if r["kind"] == "error"]


def categories(journal):
    return [e.get("category") for e in errors(journal)]
