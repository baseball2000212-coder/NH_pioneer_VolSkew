"""실행 진입점 — 장 막판 모멘텀(3% 규칙).

    python run.py --env paper --mode backtest    --run-id bt1            # [백테스트] (주문 없음, 네트워크 없음)
    python run.py --env paper --mode paper-sim   --run-id sim-1028       # [자체 시뮬레이션(종이 체결)] 시세만 쓰고 주문은 내지 않음
    python run.py --env paper --mode paper-order --run-id ord-1028 --confirm-orders   # [모의계좌 주문] 실제 모의 주문

- 환경은 모의(paper)뿐이다. 실전 주문 코드는 없다.
- 장중 모드는 15:00 부터 기다리다가 strategy.json 의 decision_time(2026-11-11 부터 15:18:55)에 판단하고 entry_time(15:19:00)에 진입한다. 15:46:30 이후 종료.
- 모의계좌는 종가 단일가를 흉내 내지 않고 15:35:30 청산 주문을 바로 체결한다(11/6·11/9 확인). 그래서 청산 뒤 15:46 까지 기다려
  15:45 봉 종가로 다시 계산한 손익(`auction_shadow`)을 따로 남긴다. 규칙의 성과는 이 값으로 본다. 멈추려면 Ctrl+C 또는 이 폴더에 `STOP` 파일을 만든다.
- 모든 사건은 logs/<run-id>.jsonl 에 한 줄씩 남는다(체결 틱은 logs/ticks-<run-id>.jsonl).
- `[자체 시뮬레이션]` 체결은 모의계좌 체결이 아니다. 기록의 source 가 sim / paper-account 로 구분된다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _stream in (sys.stdout, sys.stderr):      # Windows 콘솔에서도 한글이 깨지지 않게
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")
sys.path.insert(0, str(HERE))

from nhapi.timeutil import now_kst  # noqa: E402


def load_strategy(path: Path):
    from strategy_core import StrategyConfig

    return StrategyConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def run_backtest(args: argparse.Namespace) -> int:
    import backtest

    logs = Path(args.logs)
    # 2026-11-11: 15:18 판단 → 15:19 진입은 p1518·o1519 칸이 있는 NH 입력표가 필요하다(팀 KIS 입력표에는 없음)
    nh_input = HERE.parents[1] / "data" / "ext" / "nh_kq150_1m" / "daily_input.csv"
    src = nh_input if nh_input.exists() else HERE / "results" / "backtest_input_daily.csv"
    return backtest.main([
        "run", "--input", str(src), "--strategy", str(args.strategy),
        "--out", str(HERE / "results" / "backtest_result.json"), "--trades", str(HERE / "results" / "backtest_trades.csv"),
        "--journal", str(logs / f"{args.run_id}.jsonl"), "--run-id", args.run_id,
        "--team-ref", str(HERE / "results" / "team_trades_reference.csv"),
    ])


def run_live(args: argparse.Namespace) -> int:
    from nhapi import Client, Journal, KillSwitch, RiskLimits, RiskManager, load_config
    from nhapi.orders import OrderGateway
    from nhapi.parsers import parse_deposit
    from nhapi.timeutil import yyyymmdd

    from live import LiveFeed, NhOrderBroker
    from paper_fill import PaperSimBroker
    from runner import Trader

    cfg = load_strategy(Path(args.strategy))
    # P1: 종목코드를 strategy.json 고정값 대신 종목 마스터의 근월물로 고른다. 최종거래일·달력 skip 날은 매매하지 않는다
    from dataclasses import replace
    from contract import plan_day
    from nhapi.instruments import MasterStore
    store = MasterStore(HERE / ".nhapi_cache")
    store.download()
    plan = plan_day(store.load(), now_kst().date())
    print(f"오늘 계획: 매매={plan.trade} ({plan.reason}) 근월물={plan.front} (strategy.json 의 {cfg.sym} 대신)")
    if not plan.trade:
        return 0
    cfg = replace(cfg, sym=plan.front)
    logs = Path(args.logs)
    logs.mkdir(parents=True, exist_ok=True)
    journal = Journal(logs / f"{args.run_id}.jsonl", args.run_id, env="paper", mode=args.mode, sym=cfg.sym)
    client = Client(load_config(), journal=journal)
    feed = LiveFeed(client, cfg, journal, tick_path=logs / f"ticks-{args.run_id}.jsonl")
    kill = KillSwitch(stop_file=Path(args.stop_file))
    kill.install_sigint()
    risk = RiskManager(RiskLimits.from_dict(cfg.risk), kill)

    gateway = None
    if args.mode == "paper-order":
        gateway = OrderGateway(client, journal=journal)
        broker = NhOrderBroker(gateway, cfg, journal)
    else:
        broker = PaperSimBroker(feed, tick=cfg.tick, slippage_ticks=cfg.sim_slippage_ticks)

    label = {"paper-sim": "[자체 시뮬레이션(종이 체결)]", "paper-order": "[모의계좌 주문]"}[args.mode]
    print(f"{label} run_id={args.run_id} 종목={cfg.sym} 현재 {now_kst():%H:%M:%S} — 멈추려면 Ctrl+C 또는 STOP 파일")
    try:
        feed.start()
        if gateway is not None:
            report = gateway.startup_reconcile()
            print(f"시작 대사: 보유 {len(report.positions)}종목, 미체결 {len(report.open_orders)}건")
            _deposit(client, journal, "start", parse_deposit, yyyymmdd)
        trader = Trader(cfg, feed, broker, journal, risk, alert=make_alert(logs))
        result = trader.run()
        shadow = auction_shadow(trader, feed, journal, cfg) if args.mode == "paper-order" else None
        if shadow is None:
            feed.ref_bars()
        else:
            print(f"15:45 종가 기준 손익(auction_shadow): {shadow}")
        if gateway is not None:
            _deposit(client, journal, "end", parse_deposit, yyyymmdd)
    finally:
        feed.stop()
        client.close()
    print(f"결과: {result}  (틱 {feed.tick_count}건) → {logs / (args.run_id + '.jsonl')}")
    return 0 if result.get("outcome") in ("traded", "skip") else 1


TEAM_FEE_RATE = "0.000098"   # 편도 수수료 0.0098% (kq150/rule.py FEE_RATE)
TEAM_SLIP_PT = "0.1"          # 팀 규약: 진입 1틱


def auction_shadow(trader, feed, journal, cfg, *, clock=now_kst, sleep=None, wait_until_hms=(15, 46, 5)):
    """모의계좌는 15:35:30 청산 주문을 종가 단일가까지 기다리지 않고 바로 체결한다(2026-11-06·11-09 확인).
    정해진 청산(exit_reason=scheduled, auction)으로 끝난 날만 15:46 까지 기다려 15:45 봉 종가로 손익을 다시 계산해 남긴다.
    손절 청산은 연속매매에서 실제로 난 것이라 다시 계산하지 않는다. 실패해도 매매 결과는 바꾸지 않는다(기록만)."""
    import time as _t
    from datetime import datetime, time as dtime
    from decimal import Decimal

    sleep = sleep or _t.sleep
    res = getattr(trader, "result", None) or {}
    if res.get("outcome") != "traded" or res.get("exit_reason") != "scheduled" or cfg.exit_mode != "auction":
        return None
    try:
        now = clock()
        target = datetime.combine(now.date(), dtime(*wait_until_hms), tzinfo=now.tzinfo)
        while clock() < target:
            sleep(min(10.0, max(0.5, (target - clock()).total_seconds())))
        bars = feed.ref_bars()
        close = (bars.get("1545") or {}).get("close")
        entry, side = trader.entry_price, trader.side
        if close is None or entry is None or side not in ("buy", "sell"):
            journal.error("auction_shadow", msg=f"15:45 봉 또는 진입가 없음 (bars={list(bars)})", action="skip")
            return None
        close, entry = Decimal(str(close)), Decimal(str(entry))
        s = 1 if side == "buy" else -1
        gross = s * (close - entry)
        fee = Decimal(TEAM_FEE_RATE) * (entry + close)
        out = {"side": side, "entry_fill": str(entry), "close_1545": str(close), "gross_pt": str(gross),
               "net_pt_fee": str(gross - fee), "net_pt_team": str(gross - fee - Decimal(TEAM_SLIP_PT)),
               "paper_gross_pt": res.get("gross_pt"), "paper_minus_auction_pt": str(Decimal(str(res.get("gross_pt"))) - gross)
               if res.get("gross_pt") is not None else None}
        journal.event("auction_shadow", "15:45 종가 단일가 기준 다시 계산(모의계좌 즉시 체결 보정)", **out)
        return out
    except Exception as exc:  # noqa: BLE001 — 기록 보조라 실패해도 매매 결과는 그대로
        journal.error("auction_shadow", msg=f"{type(exc).__name__}: {exc}", action="skip")
        return None


def make_alert(logs: Path):
    """경보: logs/ALERTS-<날짜>.txt 에 한 줄씩 (바로 flush) + 텔레그램(환경변수 TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID 가 있으면)."""
    import os

    def alert(text: str) -> None:
        line = f"{now_kst():%Y-%m-%d %H:%M:%S} {text}"
        with (logs / f"ALERTS-{now_kst():%Y%m%d}.txt").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)
        tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if tok and chat:
            try:
                import httpx
                httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": line}, timeout=5)
            except Exception:  # noqa: BLE001 — 알림 실패는 무시
                pass
    return alert


def _deposit(client, journal, when, parse_deposit, yyyymmdd) -> None:
    """예수금 스냅숏. 주간 실행이라 biz_dt 는 오늘이다(야간엔 다음 영업일 — ISSUE-002)."""
    try:
        dep = parse_deposit(client.query("domestic_deposit", {"biz_dt": yyyymmdd(now_kst())}).body)
        journal.event("deposit", when, when=when, total_assets=dep.total_assets, cash=dep.cash, orderable_total=dep.orderable_total, fee=dep.fee)
    except Exception as exc:  # noqa: BLE001 — 보조 기록이라 실패해도 매매는 계속한다.
        journal.error("deposit", msg=f"{type(exc).__name__}: {exc}", action="skip")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="장 막판 모멘텀(3% 규칙) 실행")
    ap.add_argument("--env", choices=["paper"], default="paper", help="모의(paper)만 지원")
    ap.add_argument("--mode", choices=["backtest", "paper-sim", "paper-order"], required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--strategy", default=str(HERE / "strategy.json"))
    ap.add_argument("--logs", default=str(HERE / "logs"))
    ap.add_argument("--stop-file", default=str(HERE / "STOP"))
    ap.add_argument("--confirm-orders", action="store_true", help="paper-order 에서 실제 모의 주문을 내겠다는 확인")
    args = ap.parse_args(argv)
    args.run_id = args.run_id or f"{args.mode}-{now_kst():%Y%m%d-%H%M%S}"

    if args.mode == "backtest":
        return run_backtest(args)
    if args.mode == "paper-order" and not args.confirm_orders:
        print("paper-order 는 모의계좌에 실제 주문을 냅니다. 확인하려면 --confirm-orders 를 붙이세요.", file=sys.stderr)
        return 2
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
