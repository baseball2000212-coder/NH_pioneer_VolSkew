"""진입점 — 모드를 명시적으로 받는다. 이 폴더에서 실행한다.

  python run.py --env paper --mode backtest    --run-id bt-001  --data-dir ../data     (연구 파이프라인 전체: 후보 8개·OOS·교차상품)
  python run.py --env paper --mode paper-sim   --run-id sim-001 --exit 23:35           (NH 모의 서버 시세 + 가상 체결, 주문 없음)
  python run.py --env paper --mode paper-order --run-id ord-001 --exit 23:35 --yes     (모의계좌에 실제 주문, 1계약)

전략(strategy.json·model_MESZ26.json): 5분봉 특징 → T² 관리도로 국면 판정 → 선형 SVM 으로 롱/숏/관망 → 같은 쪽 최우선호가 지정가 진입(10초 제한),
손절·시간·세션 청산은 시장가. 실전(real)은 지원하지 않는다. 키 파일은 환경변수 NHFUTURES_CONFIG 가 가리키는 곳(config.example.yaml 참고).
중지: 이 폴더에 `STOP` 파일을 만들거나 Ctrl+C → 미체결 취소 → 청산 → 잔고 0 확인 → 기록 마감.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from nhapi.journal import MODE_LABELS, Journal, read_journal  # noqa: E402
from nhapi.risk import KillSwitch                              # noqa: E402
from nhapi.timeutil import now_kst                             # noqa: E402

FILL_SOURCE = {"paper-sim": "paper-sim", "paper-order": "mock-account"}


class ConsoleJournal(Journal):
    """파일 기록(JSONL)은 그대로 하고, 중요한 사건만 한 줄로 콘솔에도 보여 준다 (장중에 눈으로 확인용)."""

    SHOW_EVENTS = {"startup", "resume", "bar", "skip_entry", "entry_timeout", "recon_ok", "recon_mismatch", "bar_missing", "shutdown_start", "exit_px_estimated"}

    def write(self, kind, sym=None, /, **fields):
        rec = super().write(kind, sym, **fields)
        name = rec.get("name")
        if kind in ("signal", "order", "fill", "trade", "error") or (kind == "event" and name in self.SHOW_EVENTS) or (kind == "run" and rec.get("phase") in ("start", "summary")):
            keep = {k: v for k, v in rec.items() if k not in ("run_id", "env", "mode", "seq", "sym", "ts", "kind", "model", "text")}
            print(rec["ts"][11:19], kind, json.dumps(keep, ensure_ascii=False)[:260], flush=True)
        return rec


def hm(day: datetime, text: str | None) -> datetime | None:
    if not text:
        return None
    parts = [int(x) for x in text.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return day.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)


def realized_today(log_dir: Path, day_iso: str, mode: str, exclude_run: str = "") -> float:
    """오늘 같은 mode 의 다른 실행에서 확정한 순손익(`trade` 줄의 net_usd) 합계. 일일 손실 한도에 쓴다. 기록은 읽기만 한다."""
    total = 0.0
    for path in sorted(Path(log_dir).glob("*.jsonl")):
        try:
            for rec in read_journal(path):
                if (rec.get("kind") == "trade" and rec.get("mode") == mode and str(rec.get("ts", "")).startswith(day_iso)
                        and rec.get("run_id") != exclude_run):
                    total += float(rec.get("net_usd") or 0.0)
        except OSError:
            continue
    return total


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="T² 관리도 + 선형 SVM 5분봉 전략 (MES)")
    p.add_argument("--env", required=True, choices=["paper"], help="모의(paper)만 지원합니다.")
    p.add_argument("--mode", required=True, choices=["backtest", "paper-sim", "paper-order"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--config", default=str(HERE / "strategy.json"))
    p.add_argument("--model", default=str(HERE / "model_MESZ26.json"))
    p.add_argument("--log-dir", default=str(HERE / "logs"), help="실행 기록(JSONL) 폴더 (작업 폴더에서는 ../logs)")
    p.add_argument("--data-dir", default=str(HERE / "data"), help="backtest: 분봉 CSV.gz 폴더")
    p.add_argument("--out-dir", default=str(HERE / "results"))
    p.add_argument("--window", choices=["live", "all"], default="live", help="신규 진입 허용 시각 창 (strategy.json windows)")
    p.add_argument("--exit", help="이 시각(HH:MM)부터 신규 진입 없이 정리·종료. 기본: strategy.json flatten")
    p.add_argument("--resume", action="store_true", help="이미 있는 포지션을 이어받음")
    p.add_argument("--yes", action="store_true", help="paper-order: 모의계좌에 실제 주문을 내는 데 동의")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    if a.mode == "backtest":
        import research
        return research.main(["--config", a.config, "--data-dir", a.data_dir, "--out-dir", a.out_dir, "--run-id", a.run_id])

    import pandas as pd
    from brokers import NHBroker, SimBroker
    from engine import Engine
    from nhapi import Client, OverseasGateway, find_config_path, load_config
    from nhapi.ratelimit import RateLimiter
    from signals import SignalModel

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    sym = cfg["trade"]["symbol"]
    keys = load_config(find_config_path())
    if keys.default_environment != "paper":
        raise SystemExit("모의(paper) 키가 아닙니다. 중단합니다.")
    if a.mode == "paper-order" and not a.yes:
        raise SystemExit("paper-order 는 모의계좌에 실제 주문을 냅니다. 동의하면 --yes 를 붙이세요.")
    model = SignalModel.from_json(a.model)
    h, gate = int(model.meta["h"]), bool(model.meta["gate"])
    clock = lambda: now_kst().replace(tzinfo=None)                # 엔진 시각 = 한국시간(naive)
    today = clock()
    log_dir = Path(a.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    journal = ConsoleJournal(log_dir / f"{a.run_id}.jsonl", a.run_id, env="paper", mode=a.mode, sym=sym)
    client = Client(keys, journal=journal, limiter=RateLimiter(min_gap=0.15))
    warn = lambda where, msg: journal.error("warn", sym, where=where, msg=msg)
    nh = NHBroker(client, OverseasGateway(client, journal=journal), clock=clock, on_error=warn)
    broker = SimBroker(nh.quote, nh.minute_rows) if a.mode == "paper-sim" else nh
    kill = KillSwitch(HERE / "STOP")
    kill.install_sigint()
    done = realized_today(log_dir, today.date().isoformat(), a.mode, exclude_run=a.run_id)
    bars_out: list[dict] = []
    eng = Engine(broker, cfg, model, sym=sym, source=FILL_SOURCE[a.mode], clock=clock, sleep=__import__("time").sleep, log=journal,
                 kill=kill, window=a.window, exit_at=hm(today, a.exit), h=h, gate=gate, realized_before_usd=done, resume=a.resume,
                 bars_out=bars_out)
    print(MODE_LABELS[a.mode], "시작", a.run_id, f"h={h} gate={gate} model={model.meta.get('id')}", flush=True)
    summary = eng.run()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if bars_out:
        pd.DataFrame(bars_out).to_csv(out / f"live_bars_{a.run_id}.csv", index=False)
    print(MODE_LABELS[a.mode], "끝", json.dumps(summary, ensure_ascii=False), "| 기록:", log_dir / f"{a.run_id}.jsonl", flush=True)
    return 0 if summary.get("flat") else 2


if __name__ == "__main__":
    sys.exit(main())
