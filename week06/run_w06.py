"""6주차 실행: 2주차 국내전략(장 막판 모멘텀)을 20계약으로 — 호가 따라 분할 진입 + 조각별 감시. 모의(paper) 전용.

  python run_w06.py --mode paper-sim                       # 실시간 시세 + 종이 체결(주문 없음)
  python run_w06.py --mode paper-order --confirm-orders    # 모의계좌 주문
  python run_w06.py --mode paper-order --confirm-orders --drill now --drill-stop 0.3
        # 훈련: 지금 시각 기준으로 판단·진입·청산 시각을 옮겨(연속매매 청산) 손절·분할 청산이 실제로 도는지 본다.
        #       판단 임계는 100% 로 넓혀 그날 방향 그대로 들어간다. 09:05~14:50 에만.

하루 흐름(본 실행):
  15:00 시작 → 주문가능수량·잔고·미체결 확인 → 15:18:55 판단(2주차와 같음) → 15:19:00~ 호가 따라 분할 진입(조각마다 감시)
  → 15:34:55 까지 10pt 손절 감시(걸리면 호가 따라 1초 간격 분할 청산) → 15:35:30 종가 단일가 청산 20계약 한 주문
  → 15:46 15:45 종가 기준 손익 → results/w06_runs.csv 한 줄.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time as _time
from dataclasses import replace
from datetime import datetime, timedelta, time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")
sys.path.insert(0, str(HERE))

from nhapi.timeutil import now_kst  # noqa: E402

import split_exec as sx  # noqa: E402
import w06_record  # noqa: E402
from runner_w06 import SplitPlan  # noqa: E402


def _t(dt: datetime) -> dtime:
    return dtime(dt.hour, dt.minute, dt.second)


def drill_config(cfg, at: datetime, stop_pt: Decimal | None):
    """훈련용 시각: at 판단, +5초 진입, +3분 50초 연속매매 청산(손절 감시는 +4분까지). 임계 100%."""
    return replace(
        cfg, threshold_pct=Decimal("100"), exit_mode="continuous",
        start_not_before=_t(at - timedelta(seconds=60)), decision_time=_t(at), entry_time=_t(at + timedelta(seconds=5)),
        stop_until=_t(at + timedelta(minutes=4)), exit_submit_continuous=_t(at + timedelta(minutes=3, seconds=50)),
        exit_submit_auction=_t(at + timedelta(minutes=5)), exit_retry_time=_t(at + timedelta(minutes=5, seconds=10)),
        auction_fill_time=_t(at + timedelta(minutes=5, seconds=20)), end_time=_t(at + timedelta(minutes=6)),
        stop_pt=stop_pt if stop_pt is not None else cfg.stop_pt,
    )


def orderable_qty(client, sym: str) -> int | None:
    """주문가능수량(시장가 신규). 실패하면 None(그땐 설정 상한만)."""
    try:
        b = client.query("domestic_orderable-quantity", {"sym": sym, "trd_div": "1", "ord_sts": "2", "ord_tp_cd": "2", "ord_pric": "0"}).body
        return int(Decimal(str(b.get("ord_psb_qty") or "0")))
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="6주차: 2주차 국내전략 20계약 (호가 따라 분할 진입 + 조각별 감시)")
    ap.add_argument("--mode", choices=["paper-sim", "paper-order"], required=True)
    ap.add_argument("--confirm-orders", action="store_true")
    ap.add_argument("--strategy", default=str(HERE / "strategy_w06.json"))
    ap.add_argument("--logs", default=str(HERE / "logs"))
    ap.add_argument("--stop-file", default=str(HERE / "STOP"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--drill", default=None, help="훈련 판단 시각 HH:MM:SS (오늘) 또는 now(90초 뒤)")
    ap.add_argument("--drill-stop", default=None, help="훈련 손절폭 pt (작게 줘서 손절·분할 청산을 일부러 부른다)")
    ap.add_argument("--drill-qty", type=int, default=None)
    args = ap.parse_args(argv)
    if args.mode == "paper-order" and not args.confirm_orders:
        print("paper-order 는 모의계좌에 실제 주문을 냅니다. --confirm-orders 를 붙이세요.", file=sys.stderr)
        return 2

    from nhapi import Client, Journal, KillSwitch, RiskLimits, RiskManager, load_config
    from nhapi.instruments import MasterStore
    from nhapi.orders import OrderGateway
    from nhapi.parsers import parse_deposit
    from nhapi.timeutil import yyyymmdd

    from contract import plan_day
    from live import NhOrderBroker
    from live_w06 import LiveFeedW06
    from paper_fill import PaperSimBroker
    from run import _deposit, load_strategy, make_alert
    from runner_w06 import SplitTrader

    raw = json.loads(Path(args.strategy).read_text(encoding="utf-8"))
    w = raw["w06"]
    cfg = load_strategy(Path(args.strategy))
    store = MasterStore(HERE / ".nhapi_cache")
    store.download()
    plan = plan_day(store.load(), now_kst().date())
    print(f"오늘 계획: 매매={plan.trade} ({plan.reason}) 근월물={plan.front}")
    if not plan.trade:
        return 0
    cfg = replace(cfg, sym=plan.front)
    drill = None
    if args.drill:
        if args.drill == "now":             # cron 용: 지금부터 90초 뒤 정각 초
            drill = (now_kst() + timedelta(seconds=90)).replace(microsecond=0)
        else:
            h, m, s = map(int, args.drill.split(":"))
            drill = now_kst().replace(hour=h, minute=m, second=s, microsecond=0)
        if drill.time() >= dtime(14, 50) or drill.time() < dtime(9, 5):
            print("훈련은 09:05~14:50 사이에만 (A 본 실행·장 시작과 겹치지 않게).", file=sys.stderr)
            return 2
        cfg = drill_config(cfg, drill, Decimal(args.drill_stop) if args.drill_stop else None)
    tag = "drill" if drill else "a20"
    run_id = args.run_id or f"w06-{tag}-{args.mode}-{now_kst():%Y%m%d-%H%M%S}"
    logs = Path(args.logs)
    logs.mkdir(parents=True, exist_ok=True)
    journal = Journal(logs / f"{run_id}.jsonl", run_id, env="paper", mode=args.mode, sym=cfg.sym)
    client = Client(load_config(), journal=journal)
    feed = LiveFeedW06(client, cfg, journal, tick_path=logs / f"ticks-{run_id}.jsonl")
    kill = KillSwitch(stop_file=Path(args.stop_file))
    kill.install_sigint()
    risk = RiskManager(RiskLimits.from_dict(cfg.risk), kill)
    gateway = OrderGateway(client, journal=journal) if args.mode == "paper-order" else None
    broker = NhOrderBroker(gateway, cfg, journal) if gateway else PaperSimBroker(feed, tick=cfg.tick, slippage_ticks=cfg.sim_slippage_ticks)

    cap = int(w["max_contracts"])
    if args.drill_qty:
        cap = min(cap, args.drill_qty)
    prep: dict[str, Any] = {}
    guard = sx.GuardConfig.from_dict(w["guard"])

    def planner(values: dict[str, Any], side: str) -> SplitPlan:
        """고정 cap 계약. 손절은 2주차 그대로 10pt(닿으면 전부, 호가 따라 1초 간격 분할 청산). 주문가능수량으로 한 번 더 자름."""
        n, stop_pt, info = cap, cfg.stop_pt, {"cap": cap}
        if drill is not None and args.drill_stop:
            stop_pt = Decimal(args.drill_stop)
        oq = prep.get("orderable")
        if oq is not None and n > oq:
            info["capped_by_orderable"] = oq
            n = oq
        return SplitPlan(n, sx.scale_schedule(n, w["entry_weights"], w["entry_offsets_s"]),
                         [sx.Child(float(o), int(q)) for o, q in zip(w["exit_stop_offsets_s"], w["exit_stop_weights"])],
                         stop_pt, info)

    label = "[모의계좌 주문]" if gateway else "[종이 체결]"
    print(f"{label} {run_id} 종목={cfg.sym} 상한 {cap}계약 {'훈련 ' + args.drill if drill else ''}")
    result: dict[str, Any] = {}
    trader = None
    try:
        feed.start()
        if gateway is not None:
            report = gateway.startup_reconcile()
            print(f"시작 대사: 보유 {len(report.positions)}종목, 미체결 {len(report.open_orders)}건")
            _deposit(client, journal, "start", parse_deposit, yyyymmdd)
            prep["orderable"] = orderable_qty(client, cfg.sym)
            journal.event("orderable", "주문가능수량", qty=prep["orderable"])
        trader = SplitTrader(cfg, feed, broker, journal, risk, alert=make_alert(logs), planner=planner, guard=guard, book=feed.book)
        result = trader.run()
        close = ppc = None
        if drill is None and result.get("outcome") == "traded":
            # 모의계좌는 15:35:30 청산을 바로 체결한다 → 15:46 에 15:45 종가 봉으로 손익을 다시 계산
            target = now_kst().replace(hour=15, minute=46, second=5, microsecond=0)
            while now_kst() < target:
                _time.sleep(min(10.0, max(0.5, (target - now_kst()).total_seconds())))
            bars = feed.ref_bars()
            close = (bars.get("1545") or {}).get("close")
            close = Decimal(str(close)) if close is not None else None
            ppc, xp = w06_record.pnl_per_contract(trader, close)
            journal.event("w06_pnl", "15:45 종가 기준 계약당 손익(수수료 뒤)", close_1545=close, exit_px=xp,
                          pnl_pt_per_contract=ppc, pnl_won=(ppc * trader.qty_entered * 10000) if ppc is not None else None)
        else:
            feed.ref_bars()
        if gateway is not None:
            _deposit(client, journal, "end", parse_deposit, yyyymmdd)
        row = w06_record.result_row(run_id, args.mode, drill is not None, trader, result, close, ppc)
        out = HERE / "results" / "w06_runs.csv"
        new = not out.exists()
        with out.open("a", encoding="utf-8", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(row))
            if new:
                wr.writeheader()
            wr.writerow(row)
    finally:
        feed.stop()
        client.close()
    print(f"결과: {result} (알림 {feed.notice_count}건) → {logs / (run_id + '.jsonl')}")
    return 0 if result.get("outcome") in ("traded", "skip") else 1


if __name__ == "__main__":
    sys.exit(main())
