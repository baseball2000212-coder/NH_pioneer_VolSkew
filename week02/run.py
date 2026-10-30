"""실행 진입점 — 장 막판 모멘텀(3% 규칙).

    python run.py --env paper --mode backtest    --run-id bt1            # [백테스트] (주문 없음, 네트워크 없음)
    python run.py --env paper --mode paper-sim   --run-id sim-1028       # [자체 시뮬레이션(종이 체결)] 시세만 쓰고 주문은 내지 않음
    python run.py --env paper --mode paper-order --run-id ord-1028 --confirm-orders   # [모의계좌 주문] 실제 모의 주문

- 환경은 모의(paper)뿐이다. 실전 주문 코드는 없다.
- 장중 모드는 15:00 부터 기다리다가 15:19:50 에 판단한다. 15:46:30 이후 종료. 멈추려면 Ctrl+C 또는 이 폴더에 `STOP` 파일을 만든다.
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
    return backtest.main([
        "run", "--input", str(HERE / "results" / "backtest_input_daily.csv"), "--strategy", str(args.strategy),
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
        trader = Trader(cfg, feed, broker, journal, risk)
        result = trader.run()
        feed.ref_bars()
        if gateway is not None:
            _deposit(client, journal, "end", parse_deposit, yyyymmdd)
    finally:
        feed.stop()
        client.close()
    print(f"결과: {result}  (틱 {feed.tick_count}건) → {logs / (args.run_id + '.jsonl')}")
    return 0 if result.get("outcome") in ("traded", "skip") else 1


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
