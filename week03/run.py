"""진입점 — 모드를 명시적으로 받습니다.

  python run.py --env paper --mode backtest    --run-id bt-001 --bars data/MGCZ26_1m.csv.gz --zt data/ZTZ26_1m.csv.gz
  python run.py --env paper --mode paper-sim   --run-id sim-001     (실시간 분봉 + 종이 체결, 주문 없음)
  python run.py --env paper --mode paper-order --run-id ord-001     (모의계좌에 실제 주문)

실전(real) 환경은 지원하지 않습니다. 모의계좌 주문 모드는 시작 전에 확인을 받습니다(`--yes` 로 생략).
STOP 파일(이 스크립트 폴더에 `STOP` 이름으로 만들기, `--stop-file` 로 바꿈) 또는 Ctrl+C 로 안전하게 멈춥니다.
시작 때 계좌에 남은 보유·미체결이 있으면 시작하지 않습니다. `--flatten-leftover` 를 붙이면 이 종목 것을 정리하고 시작합니다.
경보는 `<log-dir>/ALERTS-YYYYMMDD.txt` 에 쌓이고, 환경변수 TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID 가 있으면 텔레그램으로도 갑니다.

백테스트 체결 모델: `--fill-model level|close|next_open`(기본은 strategy.json 의 backtest.fill_model = level).
슬리피지 기본은 1틱입니다. README 6-1 수치는 `--slippage-ticks 0`(또는 1)로 재현합니다(level 일 때).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from backtest import FILL_MODELS, fill_model_of, run_backtest, sha256, summarize, write_outputs   # noqa: E402
from feeds import ZtSeries, load_csv, load_nh_json                    # noqa: E402
from gate_store import GateStore                                       # noqa: E402
from nhapi.journal import MODE_LABELS, Journal                         # noqa: E402

FILL_MODEL_TEXT = {
    "level": "엔진 기준가(돌파 수준·봉 시가·손절선)에 슬리피지(틱)만 불리하게 더함.",
    "close": "돌파 진입·손절(봉 안에서 닿은 수준가)은 감지 봉 종가로, 봉 시가 기준 진입·청산은 그대로. 그 위에 슬리피지(틱)를 불리하게 더함.",
    "next_open": "돌파 진입·손절(봉 안에서 닿은 수준가)은 다음 봉 시가로(마지막 봉이면 종가), 봉 시가 기준 진입·청산은 그대로. 그 위에 슬리피지(틱)를 불리하게 더함.",
}


def load_bars(path: Path):
    return load_nh_json(path) if path.suffix == ".json" else load_csv(path)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MGC 아시아 시간대 금 변동성 게이트 + 필터형 규칙 2·3")
    p.add_argument("--env", required=True, choices=["paper"], help="모의(paper)만 지원합니다.")
    p.add_argument("--mode", required=True, choices=["backtest", "paper-sim", "paper-order"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--config", default=str(HERE / "strategy.json"))
    p.add_argument("--out-dir", default=str(HERE / "results"))
    p.add_argument("--log-dir", default=None, help="실행 기록(JSONL)·경보 파일 폴더. 기본은 --out-dir")
    p.add_argument("--seed", default=str(HERE / "data" / "gate_history_seed.csv"))
    p.add_argument("--bars")
    p.add_argument("--zt")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--slippage-ticks", type=float)
    p.add_argument("--fill-model", choices=FILL_MODELS, help="백테스트 체결 모델. level 은 원래 방식(README 6-1 재현용)")
    p.add_argument("--round-trip-usd", type=float, help="계약당 왕복 비용(달러). 기본은 strategy.json (모의 수수료 관찰값 4.0)")
    p.add_argument("--yes", action="store_true", help="모의계좌 주문 확인 질문 생략")
    p.add_argument("--flatten-leftover", action="store_true", help="시작 때 남은 이 종목 보유·미체결을 정리하고 시작(크래시 뒤 재시작용)")
    p.add_argument("--stop-file", default="STOP", help="멈춤 파일. 상대 경로는 이 스크립트 폴더 기준")
    return p.parse_args(argv)


def stop_path(text: str) -> Path:
    """STOP 파일 경로. 상대 경로는 작업 폴더(cwd)가 아니라 이 스크립트 폴더 기준입니다."""
    path = Path(text)
    return path if path.is_absolute() else HERE / path


def make_alert(log_dir: Path):
    """경보: <log_dir>/ALERTS-<날짜>.txt 에 한 줄씩(바로 기록) + 텔레그램(환경변수 TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID 가 있으면).

    kq150/live/run.py 의 make_alert 와 같습니다. 알림 실패는 매매를 멈추지 않습니다.
    """
    import os

    from nhapi.timeutil import now_kst

    def alert(text: str) -> None:
        line = f"{now_kst():%Y-%m-%d %H:%M:%S} {text}"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"ALERTS-{now_kst():%Y%m%d}.txt").open("a", encoding="utf-8") as f:
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


def main(argv=None) -> int:
    a = parse_args(argv)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    if a.round_trip_usd is not None:
        cfg["costs"]["round_trip_usd"] = a.round_trip_usd
    if a.slippage_ticks is not None:
        cfg["costs"]["slippage_ticks"] = a.slippage_ticks
    if a.fill_model is not None:
        cfg.setdefault("backtest", {})["fill_model"] = a.fill_model
    out_dir = Path(a.out_dir)
    log_dir = Path(a.log_dir) if a.log_dir else out_dir
    sym = cfg["strategy"]["sym"]
    journal = Journal(log_dir / f"{a.run_id}.jsonl", a.run_id, env=a.env, mode=a.mode, sym=sym)
    store = GateStore(a.seed)
    if a.mode == "backtest":
        print(f"{MODE_LABELS[a.mode]} run_id={a.run_id} 종목={sym} 게이트 이력 {len(store.values)}일")
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
        model = fill_model_of(cfg)
        assumptions = dict(
            label=MODE_LABELS[a.mode], run_id=a.run_id, config=cfg,
            fill_model=f"[{model}] {FILL_MODEL_TEXT[model]} 호가·체결 큐 없음. 모의계좌 체결 아님.",
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
    live_files = store.load_live(out_dir)        # 시드 + 지난 실시간 실행의 이력(run_id 별 파일)
    print(f"{MODE_LABELS[a.mode]} run_id={a.run_id} 종목={sym} 게이트 이력 {len(store.values)}일 (시드 + 실시간 파일 {len(live_files)}개)")
    journal.event("gate_history", "게이트 이력 읽음", sym, days=len(store.values), files=[p.name for p in live_files])
    return run_paper(a, cfg, store, journal, log_dir)


def run_paper(a, cfg, store, journal, log_dir: Path) -> int:
    from executors import EchoExecutor, OrderExecutor, SimExecutor
    from live import LiveFeed, LiveRunner, make_alarm, prepare_account
    from nhapi import Client, Decision, OverseasGateway, find_config_path, load_config
    from nhapi.risk import KillSwitch
    from nhapi.timeutil import now_kst
    from sessions import KST, hm

    s = cfg["strategy"]
    alarm = make_alarm(journal, make_alert(log_dir), s["sym"])
    config = load_config(find_config_path())
    client = Client(config, journal=journal)
    kill = KillSwitch(stop_path(a.stop_file))
    kill.install_sigint()
    lv = cfg["live"]
    feed = LiveFeed(client, s["exchange"], s["sym"], s["zt_exchange"], s["zt_sym"], int(lv["backfill_pages"]), int(lv.get("latest_req_qty", 10)))
    gateway = None
    if a.mode == "paper-order":
        if not a.yes and input(f"모의계좌에 {s['sym']} 시장가 주문을 냅니다. 계속할까요? [y/N] ").strip().lower() != "y":
            print("취소했습니다.")
            return 1
        # 진입 점검(킬 스위치). 청산(reduce_only) 주문은 게이트웨이가 이 점검을 건너뜁니다.
        gateway = OverseasGateway(client, journal=journal, pre_check=lambda r: Decision(not kill.triggered(), "킬 스위치" if kill.triggered() else ""))
        deadline = datetime.combine(now_kst().date(), datetime.min.time(), KST) + timedelta(minutes=hm(lv.get("flatten_deadline", "23:58")))
        ok, text = prepare_account(gateway, s["sym"], flatten_leftover=a.flatten_leftover, journal=journal, alarm=alarm,
                                   now=now_kst, sleep=time.sleep, deadline=deadline)
        print(f"시작 대사: {text}")
        if not ok:
            print(text, file=sys.stderr)
            return 3
        executor = OrderExecutor(gateway, s["sym"], fill_wait_seconds=float(lv["fill_wait_seconds"]), alarm=alarm)
    else:
        executor = SimExecutor("paper-sim", float(s["tick"]), float(cfg["costs"].get("slippage_ticks", 0)))
    try:
        result = LiveRunner(cfg, feed, EchoExecutor(executor), store, journal, kill, gateway=gateway, mode=a.mode,
                            bars_path=Path(a.out_dir) / f"{a.run_id}_bars.csv.gz", alarm=alarm).run()
    finally:
        store.save(Path(a.out_dir) / f"gate_history-{a.run_id}.csv")
    print("종료:", json.dumps(result, ensure_ascii=False))
    return 4 if gateway is not None and result.get("final_position") != 0 else 0   # 4 = 마감 잔고가 0 이 아니거나 확인 못 함


if __name__ == "__main__":
    raise SystemExit(main())
