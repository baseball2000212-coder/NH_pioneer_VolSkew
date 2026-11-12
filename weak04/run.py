"""진입점 — 모드를 명시적으로 받는다. 이 폴더에서 실행한다.

  python run.py --env paper --mode backtest    --run-id bt-001  --day 20261110 --start 22:29:00 --exit 23:40:00
  python run.py --env paper --mode paper-sim   --run-id sim-001 --exit 23:40      (NH 실시간 시세 + 종이 체결, 주문 없음)
  python run.py --env paper --mode paper-order --run-id ord-001 --enter-at auto --exit 23:40 --yes (개장 20분 전 23:10 에 진입, 모의계좌에 실제 주문)

전략: ES 주간옵션 ATM 콜 n + 풋 n 을 사고, 델타가 MES 몇 계약 이상 어긋나면 MES 로 맞춘다(strategy.json).
실행한 순간의 ES 가격에 가장 가까운 행사가로 진입한다. 청산: --exit 시각, 손절, 마감(--deadline) 2분 전, STOP 파일, Ctrl+C.
실전(real) 환경은 지원하지 않는다. 키 파일은 환경변수 NHFUTURES_CONFIG 가 가리키는 곳(config.example.yaml 참고).
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from brokers import NHBroker, SimBroker                              # noqa: E402
from engine import Engine                                            # noqa: E402
from journal_adapter import EngineJournal, realized_today            # noqa: E402
from nhapi.journal import MODE_LABELS, Journal                       # noqa: E402
from nhapi.timeutil import KST, now_kst                              # noqa: E402
from replay import ReplayMarket                                      # noqa: E402
from strategy_core import auto_enter_time, cfg_from_json                              # noqa: E402


def hm(day: datetime, text: str | None) -> datetime | None:
    if not text:
        return None
    parts = [int(x) for x in text.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return day.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ES 주간옵션 스트래들 + MES 델타헤지")
    p.add_argument("--env", required=True, choices=["paper"], help="모의(paper)만 지원합니다.")
    p.add_argument("--mode", required=True, choices=["backtest", "paper-sim", "paper-order"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--config", default=str(HERE / "strategy.json"))
    p.add_argument("--log-dir", default=str(HERE / "logs"), help="실행 기록(JSONL) 폴더 (작업 폴더에서는 ../logs)")
    p.add_argument("--data-dir", default=str(HERE / "data"), help="backtest: 기록된 호가 폴더")
    p.add_argument("--day", help="backtest: 기록 날짜 YYYYMMDD")
    p.add_argument("--start", help="backtest: 진입 시각 HH:MM:SS")
    p.add_argument("--enter-at", help="진입 시각 HH:MM[:SS] 또는 auto(= 미장 개장 enter_before_open_min 분 전). 그때까지 포지션 없이 기다린다. 없으면 실행한 순간 진입")
    p.add_argument("--exit", help="청산 시작 시각 HH:MM[:SS]")
    p.add_argument("--deadline", help="이 시각 전에는 무조건 잔고 0 (기본: strategy.json risk.deadline)")
    p.add_argument("--pairs", type=int, help="콜 n + 풋 n (기본: strategy.json)")
    p.add_argument("--band", type=float, help="델타가 MES 몇 계약 어긋나면 맞출지")
    p.add_argument("--latency", type=float, help="backtest: 체결 지연(초). 기본 strategy.json backtest.latency_sec")
    p.add_argument("--out-dir", default=str(HERE / "results"), help="backtest: 요약 JSON 저장 폴더")
    p.add_argument("--resume", action="store_true", help="이미 있는 포지션을 이어받아 헤지")
    p.add_argument("--stay", action="store_true", help="청산·실패 뒤에도 마감까지 시세만 기록하며 켜 둠")
    p.add_argument("--yes", action="store_true", help="paper-order: 모의계좌에 실제 주문을 내는 데 동의")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):                       # 콘솔 인코딩이 한글·기호를 못 담아도 엔진이 죽지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    conf = json.loads(Path(a.config).read_text(encoding="utf-8"))
    if a.pairs:
        conf["strategy"]["pairs"] = a.pairs
    if a.band:
        conf["params"]["band"] = a.band
    log_path = Path(a.log_dir) / f"{a.run_id}.jsonl"
    sym = conf["strategy"]["series"]

    if a.mode == "backtest":
        if not (a.day and a.start):
            raise SystemExit("backtest 는 --day 와 --start 가 필요합니다.")
        day = datetime.strptime(a.day, "%Y%m%d")
        cfg = cfg_from_json(conf, today=day, exit_at=hm(day, a.exit), deadline=hm(day, a.deadline) if a.deadline else None)
        if not a.deadline:
            cfg.deadline = None                                   # 재현에서는 청산 시각만 본다
        cfg.kill_file = cfg.stop_file = "__no_such_file__"
        latency = conf["backtest"]["latency_sec"] if a.latency is None else a.latency
        mkt = ReplayMarket(Path(a.data_dir), a.day, hm(day, a.start))
        journal = Journal(log_path, a.run_id, env="paper", mode="backtest", sym=sym, clock=lambda: mkt.now.replace(tzinfo=KST))
        elog = EngineJournal(journal, series=sym, mes_sym=cfg.mes_sym)
        broker = SimBroker(mkt.quote, lambda s: mkt.quote(s, ahead=latency))
        eng = Engine(broker, cfg, clock=mkt.clock, sleep=mkt.sleep, log=elog)
        eng.run()
        write_backtest_result(Path(a.out_dir), a, conf, cfg, latency, mkt, elog, broker)
        print(f"{MODE_LABELS['backtest']} 기록: {log_path} | 가상 체결 {len(broker.trades)}건", flush=True)
        return 0

    # paper-sim / paper-order: NH 모의 서버 (키는 환경변수 NHFUTURES_CONFIG 가 가리키는 파일)
    from nhapi import Client, OverseasGateway, find_config_path, load_config
    from nhapi.ratelimit import RateLimiter

    def _term(*_):                                                # kill·창 닫힘으로 끝나도 청산 순서를 밟게
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _term)
    keys = load_config(find_config_path())
    if keys.default_environment != "paper":
        raise SystemExit("모의(paper) 키가 아닙니다. 중단합니다.")
    if a.mode == "paper-order" and not a.yes:
        raise SystemExit("paper-order 는 모의계좌에 실제 주문을 냅니다. 동의하면 --yes 를 붙이세요.")
    clock = lambda: now_kst().replace(tzinfo=None)                # 엔진 시각 = 한국시간(naive)
    today = clock()
    enter_at = None if not a.enter_at else (auto_enter_time(conf, today) if a.enter_at == "auto" else hm(today, a.enter_at))
    cfg = cfg_from_json(conf, today=today, exit_at=hm(today, a.exit), deadline=hm(today, a.deadline) if a.deadline else None, enter_at=enter_at)
    journal = Journal(log_path, a.run_id, env="paper", mode=a.mode, sym=sym)
    elog = EngineJournal(journal, series=sym, mes_sym=cfg.mes_sym)
    client = Client(keys, journal=journal, limiter=RateLimiter(min_gap=0.15))
    warn = lambda where, msg: elog({"ts": clock().isoformat(timespec="seconds"), "kind": "warn", "what": where, "msg": msg})
    if a.mode == "paper-sim":
        broker = SimBroker(NHBroker(client, None, clock=clock, on_error=warn).quote)
    else:
        broker = NHBroker(client, OverseasGateway(client, journal=journal), clock=clock, on_error=warn)
    done = realized_today(Path(a.log_dir), today.date().isoformat(), a.mode, exclude_run=a.run_id)
    eng = Engine(broker, cfg, clock=clock, log=elog, resume=a.resume, stay=a.stay, realized_today=done)
    eng.run()
    print(f"{MODE_LABELS[a.mode]} 기록: {log_path}", flush=True)
    return 0


def write_backtest_result(out_dir: Path, a, conf, cfg, latency, mkt, elog, broker) -> None:
    """[백테스트] 결과 JSON — 요약과 함께 가정·입력 파일 해시·설정을 저장한다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "run_id": a.run_id, "mode": "backtest", "label": MODE_LABELS["backtest"], "env": "paper",
        "day": a.day, "start": a.start, "exit": a.exit,
        "summary": elog.summary, "fills": elog.fills, "sim_trades": broker.trades,
        "assumptions": {**conf["backtest"], "latency_sec": latency,
                        "fees_usd_per_contract_one_way": {"option": cfg.opt_fee, "mes": cfg.mes_fee},
                        "multipliers": {"option": cfg.opt_mult, "mes": cfg.mes_mult},
                        "rate": "금리 0 (Black-76)"},
        "config": conf, "inputs_sha256": mkt.input_hashes(),
    }
    (out_dir / f"{a.run_id}_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
