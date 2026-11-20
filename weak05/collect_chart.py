"""분봉 이력 수집기: 차트 API 를 나눠 받아 이어 붙이고 중복을 제거해 CSV.gz 와 요약 JSON 으로 남긴다.

  python collect_chart.py --syms MESZ26 MNQZ26 MGCZ26 6EZ26 --start 20260801 --out-dir ../data --run-id collect-001

- 모의 서버(해외 키)만 쓴다. 조회 API 만 부르며 주문은 없다.
- 쪽당 줄 수·중복·공백·멈춘 이유를 `collect_summary_<run_id>.json` 에 저장한다 (README 의 데이터 근거).
- 5분봉 등 가공은 `bars.py`. 원시 1분봉(수십 MB)은 `--out-dir` 에 두고 제출하지 않는다.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nhapi import Client, Journal, NHApiError, load_config  # noqa: E402
from nhapi.history import FIELDS, collect_overseas_minute, gap_report  # noqa: E402
from nhapi.timeutil import now_kst  # noqa: E402

EXCHANGE = {"MES": "FCME", "MNQ": "FCME", "MGC": "FCME", "6E": "FCME"}


def exch_of(sym: str) -> str:
    return "FCME"  # 이번 주 대상은 모두 CME 선물


def write_csv_gz(path: Path, rows: list[dict[str, str]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="분봉 이력 수집기")
    p.add_argument("--syms", nargs="+", required=True)
    p.add_argument("--start", required=True, help="YYYYMMDD (이 날짜까지 거슬러 받음)")
    p.add_argument("--end", default=None, help="YYYYMMDD (기본: 오늘)")
    p.add_argument("--out-dir", default=str(HERE / "data"))
    p.add_argument("--log-dir", default=str(HERE / "logs"))
    p.add_argument("--run-id", required=True)
    p.add_argument("--min-gap", type=float, default=0.35, help="쪽 사이 대기(초). 시세 제한 초당 10건")
    args = p.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)
    end = args.end or now_kst().strftime("%Y%m%d")
    client = Client(load_config(), journal=Journal(Path(args.log_dir) / f"{args.run_id}.jsonl", args.run_id, mode="smoke"))  # 조회 전용 실행은 smoke 로 기록
    summary = {"run_id": args.run_id, "env": "paper", "collected_at": now_kst().isoformat(timespec="seconds"),
               "start": args.start, "end": end, "api": "overseas_chart-minute", "symbols": {}}
    for sym in args.syms:
        try:
            rows, rep = collect_overseas_minute(
                client, exch_of(sym), sym, start_date=args.start, end_date=end, min_gap=args.min_gap,
                on_page=lambda n, k, s=sym: print(f"\r{s}: {n}쪽 ({k}줄)", end="", flush=True))
        except NHApiError as exc:
            print(f"\n{sym}: 실패 {type(exc).__name__} {getattr(exc, 'code', '')} {str(exc)[:80]}")
            summary["symbols"][sym] = {"error": f"{type(exc).__name__} {getattr(exc, 'code', '')}"}
            continue
        write_csv_gz(out / f"minute_{sym}.csv.gz", rows)
        info = rep.as_dict()
        info["gaps"] = gap_report(rows)
        summary["symbols"][sym] = info
        print(f"\n{sym}: {rep.rows}봉 {rep.pages}쪽 중복 {rep.duplicate_rows} 멈춤={rep.stop_reason} {rep.oldest} ~ {rep.newest}")
    (out / f"collect_summary_{args.run_id}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
