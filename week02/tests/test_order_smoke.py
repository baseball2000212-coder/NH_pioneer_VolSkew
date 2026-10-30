import json
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import order_smoke as osm
from nhapi.orders import OrderOutcome
from nhapi.parsers import OrderRecord
from nhapi.timeutil import KST
from strategy_core import StrategyConfig

ROOT = Path(__file__).resolve().parents[1]
CFG = StrategyConfig.from_dict(json.loads((ROOT / "strategy.json").read_text(encoding="utf-8")))


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 28, 9, 5, tzinfo=KST)

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)


def rec(no, org=None, price="997.0", status="접수", **kw):
    return OrderRecord(order_no=no, org_order_no=org or no, sym="A066C000", side="buy", status_code="", status="", status_name=status,
                       qty=D(1), exec_qty=D(0), remaining_qty=D(1), cancelled_qty=D(0), price=D(price), exec_price=D(0),
                       trade_date="20261028", recv_time="090500000", exec_time="", exec_no="", reject_reason=kw.get("reject", ""))


class Gw:
    def __init__(self, modify_status="accepted", cancel_status="accepted"):
        self.calls, self.mod, self.can = [], modify_status, cancel_status
        self.rows = []

    def place(self, req):
        self.calls.append(("place", req.price))
        self.rows.append(rec("100", price=str(req.price)))
        return OrderOutcome("accepted", req, order_no="100")

    def modify(self, req, org):
        self.calls.append(("modify", req.price, org))
        if self.mod != "accepted":
            return OrderOutcome("rejected", req, code="X", message="모의 미지원")
        self.rows.append(rec("101", org="100", price=str(req.price)))
        return OrderOutcome("accepted", req, order_no="101")

    def cancel(self, sym, side, qty, org):
        self.calls.append(("cancel", org))
        return OrderOutcome(self.can, None, order_no="102" if self.can == "accepted" else "", code="" if self.can == "accepted" else "Y")

    def history(self):
        return list(self.rows)

    def open_orders(self):
        return []


def test_floor_tick():
    assert osm.floor_tick(D("1237.77"), D("0.1")) == D("1237.7")


def test_limit_leg_places_far_below_modifies_lower_then_cancels_the_new_order():
    c = Clock()
    steps = osm.limit_leg(Gw(), CFG, None, D("1241.1"), clock=c, sleep=c.sleep)
    assert [s["step"] for s in steps] == ["place_limit", "modify_limit", "cancel_limit"]
    assert steps[0]["price"] == D("1239.1") and steps[1]["price"] == D("1238.6")      # 2.0pt, 2.5pt 아래
    assert all(s["status"] == "accepted" for s in steps)


def test_limit_leg_stops_when_modify_rejected_but_still_cancels_original():
    c = Clock()
    gw = Gw(modify_status="rejected")
    steps = osm.limit_leg(gw, CFG, None, D("1241.1"), clock=c, sleep=c.sleep)
    assert steps[1]["status"] == "rejected" and ("cancel", "100") in gw.calls


def test_probe_cancels_after_accept():
    c = Clock()
    gw = Gw()
    steps = osm.probe_leg(gw, CFG, None, D("1241.1"), clock=c, sleep=c.sleep)
    assert [s["step"] for s in steps] == ["auction_place_limit", "auction_cancel_limit"]


def test_cli_requires_confirmation_and_hours():
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, str(ROOT / "order_smoke.py"), "--stage", "am"], capture_output=True, text=True, encoding="utf-8", env=env)
    assert p.returncode == 2
    p = subprocess.run([sys.executable, str(ROOT / "order_smoke.py"), "--stage", "probe", "--confirm-orders"], capture_output=True, text=True, encoding="utf-8", env=env)
    assert p.returncode == 3 or datetime.now(KST).time() >= osm.WINDOWS["probe"][0]   # 장 시간 밖이면 거부
