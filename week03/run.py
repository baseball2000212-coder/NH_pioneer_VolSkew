"""진입점 — 모드를 명시적으로 받습니다.

  python run.py --env paper --mode backtest    --run-id bt-001 --bars data/MGCZ26_1m.csv.gz --zt data/ZTZ26_1m.csv.gz
  python run.py --env paper --mode paper-sim   --run-id sim-001     (실시간 분봉 + 종이 체결, 주문 없음)
  python run.py --env paper --mode paper-order --run-id ord-001     (모의계좌에 실제 주문)

실전(real) 환경은 지원하지 않습니다. 모의계좌 주문 모드는 시작 전에 확인을 받습니다(`--yes` 로 생략).
STOP 파일(작업 폴더에 `STOP` 이름으로 만들기) 또는 Ctrl+C 로 안전하게 멈춥니다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest import run_backtest, sha256, summarize, write_outputs   # noqa: E402
from feeds import ZtSeries, load_csv, load_nh_json                    # noqa: E402
from gate_store import GateStore                                       # noqa: E402
from nhapi.journal import MODE_LABELS, Journal                         # noqa: E402


def load_bars(path: Path):
    return load_nh_json(path) if path.suffix == ".json" else load_csv(path)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MGC 중국장 변동성 게이트 + 필터형 규칙 2·3")
    p.add_argument("--env", required=True, choices=["paper"], help="모의(paper)만 지원합니다.")
    p.add_argument("--mode", required=True, choices=["backtest", "paper-sim", "paper-order"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--config", default=str(HERE / "strategy.json"))
    p.add_argument("--out-dir", default=str(HERE / "results"))
    p.add_argument("--log-dir", default=None, help="실행 기록(JSONL) 폴더. 기본은 --out-dir")
    p.add_argument("--seed", default=str(HERE / "data" / "gate_history_seed.csv"))
    p.add_argument("--bars")
    p.add_argument("--zt")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--slippage-ticks", type=float)
    p.add_argument("--round-trip-usd", type=float, help="계약당 왕복 비용(달러). 기본은 strategy.json (모의 수수료 관찰값 4.0)")
    p.add_argument("--yes", action="store_true", help="모의계좌 주문 확인 질문 생략")
    p.add_argument("--stop-file", default="STOP")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    if a.round_trip_usd is not None:
        cfg["costs"]["round_trip_usd"] = a.round_trip_usd
    if a.slippage_ticks is not None:
        cfg["costs"]["slippage_ticks"] = a.slippage_ticks
    out_dir = Path(a.out_dir)
    log_dir = Path(a.log_dir) if a.log_dir else out_dir
    sym = cfg["strategy"]["sym"]
    journal = Journal(log_dir / f"{a.run_id}.jsonl", a.run_id, env=a.env, mode=a.mode, sym=sym)
    store = GateStore(a.seed)
    print(f"{MODE_LABELS[a.mode]} run_id={a.run_id} 종목={sym} 게이트 이력 {len(store.values)}일")
    if a.mode == "backtest":
        if not a.bars or not a.zt:
            print("백테스트에는 --bars 와 --zt 가 필요합니다.", file=sys.stderr)
            return 2
        bars, zt_bars = load_bars(Path(a.bars)), load_bars(Path(a.zt))
        journal.run("start", mode=a.mode, bars=len(bars), first=bars[0].ts.isoformat(), last=bars[-1].ts.isoformat())
        start = datetime.strptime(a.start, "%Y-%m-%d").date() if a.start else None
        end = datetime.strptime(a.end, "%Y-%m-%d").date() if a.end else None
        trades, daily = run_backtest(bars, ZtSeries(zt_bars), cfg, store, journal=journal, start=start, end=end)
        summary = summarize(trades, daily)
        journal.run("end", summary=summary)
        assumptions = dict(
            label=MODE_LABELS[a.mode], run_id=a.run_id, config=cfg,
            fill_model="엔진 기준가(돌파 수준·봉 시가·손절선)에 슬리피지(틱)만 불리하게 더함. 호가·체결 큐 없음. 모의계좌 체결 아님.",
            data={"bars": {"path": Path(a.bars).name, "sha256": sha256(Path(a.bars)), "n": len(bars)},
                  "zt": {"path": Path(a.zt).name, "sha256": sha256(Path(a.zt)), "n": len(zt_bars)},
                  "gate_seed": {"path": Path(a.seed).name, "days": len(GateStore(a.seed).values)}},
            period={"start": a.start, "end": a.end},
            notes=["게이트 이력의 과거 값은 팀원 GC 1분봉에서 계산한 시드이고, 백테스트 기간의 값은 이 실행의 분봉으로 다시 계산해 덮어쓴다.",
                   "표본은 전략을 고르는 데 쓴 기간과 겹친다(표본 내). 진짜 표본외는 모의 운영 기간뿐이다."],
        )
        write_outputs(out_dir, a.run_id, trades, daily, summary, assumptions)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    return run_paper(a, cfg, store, journal)


def run_paper(a, cfg, store, journal) -> int:
    from executors import EchoExecutor, OrderExecutor, SimExecutor
    from live import LiveFeed, LiveRunner
    from nhapi import Client, Decision, OverseasGateway, find_config_path, load_config
    from nhapi.risk import KillSwitch

    config = load_config(find_config_path())
    client = Client(config, journal=journal)
    s = cfg["strategy"]
    kill = KillSwitch(a.stop_file)
    kill.install_sigint()
    feed = LiveFeed(client, s["exchange"], s["sym"], s["zt_exchange"], s["zt_sym"], int(cfg["live"]["backfill_pages"]))
    gateway = None
    if a.mode == "paper-order":
        gateway = OverseasGateway(client, journal=journal, pre_check=lambda r: Decision(not kill.triggered(), "킬 스위치" if kill.triggered() else ""))
        report = gateway.startup_reconcile()
        print(f"시작 대사: 보유 {len(report.positions)}건, 미체결 {len(report.open_orders)}건")
        if report.positions or report.open_orders:
            print("기존 포지션·미체결이 있어 시작하지 않습니다. 정리한 뒤 다시 실행하세요.", file=sys.stderr)
            journal.error("startup_not_clean", s["sym"], positions=len(report.positions), open_orders=len(report.open_orders))
            return 3
        if not a.yes and input(f"모의계좌에 {s['sym']} 시장가 주문을 냅니다. 계속할까요? [y/N] ").strip().lower() != "y":
            print("취소했습니다.")
            return 1
        executor = OrderExecutor(gateway, s["sym"], fill_wait_seconds=float(cfg["live"]["fill_wait_seconds"]))
    else:
        executor = SimExecutor("paper-sim", float(s["tick"]), float(cfg["costs"].get("slippage_ticks", 0)))
    result = LiveRunner(cfg, feed, EchoExecutor(executor), store, journal, kill, gateway=gateway, mode=a.mode,
                        bars_path=Path(a.out_dir) / f"{a.run_id}_bars.csv.gz").run()
    store.save(Path(a.out_dir) / "gate_history_live.csv")
    print("종료:", json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
