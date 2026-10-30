"""기록(JSONL) 요약 — 표준 라이브러리만 쓰고 파일은 읽기만 한다.

    python summarize_journal.py logs/ord-1028.jsonl --run ord-1028
    python summarize_journal.py logs/ord-1028.jsonl --run ord-1028 --json results/ord-1028_summary.json

README 의 수치는 이 스크립트가 원본 기록에서 계산한 값만 쓴다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator

MODE_LABEL = {"backtest": "[백테스트]", "paper-sim": "[자체 시뮬레이션(종이 체결)]", "paper-order": "[모의계좌 주문]", "smoke": "[조회 점검]"}


def read_lines(path: Path, run: str | None) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run is None or rec.get("run_id") == run:
                yield rec


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(r.get("kind") for r in records)
    modes = sorted({r.get("mode") for r in records if r.get("mode")})
    out: dict[str, Any] = {
        "run_ids": sorted({r.get("run_id") for r in records}), "modes": modes,
        "labels": [MODE_LABEL.get(m, m) for m in modes], "lines": len(records), "by_kind": dict(kinds),
        "first_ts": records[0]["ts"] if records else None, "last_ts": records[-1]["ts"] if records else None,
    }
    out["signals"] = [
        {k: r.get(k) for k in ("ts", "side", "reason", "prev_close", "price", "price_source", "ret_pct", "trade_date")} for r in records if r["kind"] == "signal"
    ]
    out["trades"] = [
        {k: r.get(k) for k in ("ts", "trade_date", "side", "qty", "entry_price", "exit_price", "exit_reason", "gross_pt", "assumed_cost_pt", "net_pt", "net_won", "source")}
        for r in records if r["kind"] == "trade"
    ]
    nets = [float(t["net_pt"]) for t in out["trades"] if t.get("net_pt") is not None]
    if nets:
        out["trade_stats"] = {"n": len(nets), "sum_net_pt": round(sum(nets), 2), "mean_net_pt": round(statistics.mean(nets), 3),
                              "wins": sum(1 for n in nets if n > 0)}
    out["fills"] = [{k: r.get(k) for k in ("ts", "source", "side", "qty", "price", "purpose", "order_no")} for r in records if r["kind"] == "fill"]
    orders = [r for r in records if r["kind"] == "order"]
    out["order_stages"] = dict(Counter(r.get("stage") for r in orders))
    out["errors"] = [{k: r.get(k) for k in ("ts", "category", "msg", "code", "action")} for r in records if r["kind"] == "error"]
    out["api_issues"] = [{k: r.get(k) for k in ("ts", "issue_id", "api_id")} for r in records if r["kind"] == "api_issue"]

    calls = [r for r in records if r["kind"] == "event" and r.get("name") == "api_call"]
    by_api: dict[str, list[float]] = defaultdict(list)
    codes: dict[str, Counter] = defaultdict(Counter)
    for r in calls:
        by_api[r["api_id"]].append(r.get("latency_ms") or 0.0)
        codes[r["api_id"]][f"{r.get('http_status')}/{r.get('msg_cd') or '-'}"] += 1
    out["api_calls"] = {
        api: {"n": len(lat), "latency_ms_median": round(statistics.median(lat), 1), "latency_ms_max": round(max(lat), 1), "status": dict(codes[api])}
        for api, lat in sorted(by_api.items())
    }
    ws = [r for r in records if r["kind"] == "event" and r.get("name") == "ws_state"]
    out["ws_states"] = [r.get("state") for r in ws]
    out["events"] = dict(Counter(r.get("name") for r in records if r["kind"] == "event" and r.get("name") != "api_call"))
    return out


def print_report(s: dict[str, Any]) -> None:
    print(f"{' '.join(s['labels']) or '(mode 없음)'}  run_id={', '.join(map(str, s['run_ids']))}  {s['first_ts']} ~ {s['last_ts']}")
    print(f"줄 수 {s['lines']}  종류별 {s['by_kind']}")
    for sig in s["signals"]:
        print(f"  신호: {sig}")
    for t in s["trades"]:
        print(f"  거래: {t}")
    if "trade_stats" in s:
        print(f"  거래 통계: {s['trade_stats']}")
    print(f"  주문 단계: {s['order_stages']}  체결 기록 {len(s['fills'])}건")
    for e in s["errors"]:
        print(f"  오류: {e}")
    for api, c in s["api_calls"].items():
        print(f"  API {api:<28} {c['n']:>4}회 지연 중앙 {c['latency_ms_median']}ms 최대 {c['latency_ms_max']}ms 상태 {c['status']}")
    if s["ws_states"]:
        print(f"  WS 상태: {s['ws_states']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="JSONL 기록 요약(읽기 전용)")
    ap.add_argument("journal", type=Path)
    ap.add_argument("--run", default=None, help="이 run_id 줄만")
    ap.add_argument("--json", type=Path, default=None, help="요약을 JSON 으로 저장")
    args = ap.parse_args(argv)
    records = list(read_lines(args.journal, args.run))
    if not records:
        print("해당 run 의 기록이 없습니다.", file=sys.stderr)
        return 1
    summary = summarize(records)
    print_report(summary)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
