"""실행 기록(JSONL)을 읽기만 하고 요약합니다. 표준 라이브러리만 씁니다.

  python summarize_journal.py results/bt-nh-slip0.jsonl --run bt-nh-slip0
  python summarize_journal.py logs/ord-001.jsonl --run ord-001 --json results/ord-001_summary_journal.json

요약: 갈래별 줄 수 · API 호출(건수·오류·지연) · 주문 단계 · 체결 출처 · 거래 · 오류 · 실행 시작/끝.
체결은 `fill.source` 로 구분합니다(backtest-model / paper-sim / mock-account). 섞어서 합산하지 않습니다.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def read(path: Path, run_id: str | None):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id is None or rec.get("run_id") == run_id:
                yield rec


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarize(records: list[dict]) -> dict:
    kinds = Counter(r.get("kind") for r in records)
    api: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0, "latency": []})
    for r in records:
        if r.get("kind") == "event" and r.get("name") == "api_call":
            a = api[r.get("api_id", "?")]
            a["calls"] += 1
            if r.get("msg_cd") or (r.get("http_status") or 200) >= 400:
                a["errors"] += 1
            if r.get("latency_ms") is not None:
                a["latency"].append(float(r["latency_ms"]))
    api_out = {k: {"calls": v["calls"], "errors": v["errors"], "latency_p50_ms": percentile(v["latency"], 0.5),
                   "latency_p95_ms": percentile(v["latency"], 0.95)} for k, v in sorted(api.items())}
    orders = Counter(r.get("stage") for r in records if r.get("kind") == "order")
    fills = Counter(r.get("source") for r in records if r.get("kind") == "fill")
    trades = [r for r in records if r.get("kind") == "trade"]
    usd = [float(t["usd"]) for t in trades if t.get("usd") is not None]
    by_source = defaultdict(list)
    for t in trades:
        by_source[t.get("source", "?")].append(float(t["usd"]))
    errors = [{"ts": r.get("ts"), "category": r.get("category"), "msg": r.get("msg", "")} for r in records if r.get("kind") == "error"]
    issues = [r.get("issue_id") for r in records if r.get("kind") == "api_issue"]
    runs = [r for r in records if r.get("kind") == "run"]
    return {
        "run_ids": sorted({r.get("run_id") for r in records}),
        "modes": sorted({r.get("mode") for r in records}),
        "first_ts": records[0]["ts"] if records else None, "last_ts": records[-1]["ts"] if records else None,
        "lines_by_kind": dict(kinds), "api_calls": api_out, "order_stages": dict(orders), "fills_by_source": dict(fills),
        "trades": len(trades), "trades_usd_total": round(sum(usd), 2) if usd else None,
        "trades_by_source": {k: {"n": len(v), "usd_total": round(sum(v), 2)} for k, v in by_source.items()},
        "errors": errors, "api_issues": issues,
        "run_events": [{"phase": r.get("phase"), "reason": r.get("reason"), "gate_pass": r.get("gate_pass")} for r in runs],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="실행 기록(JSONL) 요약 — 읽기 전용")
    p.add_argument("journal")
    p.add_argument("--run", help="이 run_id 의 줄만")
    p.add_argument("--json", help="요약을 JSON 파일로도 저장")
    a = p.parse_args(argv)
    records = list(read(Path(a.journal), a.run))
    if not records:
        print("해당 줄이 없습니다.", file=sys.stderr)
        return 1
    s = summarize(records)
    print(f"실행 {', '.join(map(str, s['run_ids']))} / 모드 {', '.join(map(str, s['modes']))} / {s['first_ts']} ~ {s['last_ts']}")
    print("줄 수:", ", ".join(f"{k} {v}" for k, v in s["lines_by_kind"].items()))
    for name, v in s["api_calls"].items():
        print(f"  API {name}: {v['calls']}회, 오류 {v['errors']}, 지연 p50 {v['latency_p50_ms']} ms / p95 {v['latency_p95_ms']} ms")
    if s["order_stages"]:
        print("주문 단계:", s["order_stages"])
    print("체결 출처:", s["fills_by_source"] or "없음")
    print(f"거래 {s['trades']}건, 합계 {s['trades_usd_total']} $ (출처별 {s['trades_by_source']})")
    print(f"오류 {len(s['errors'])}건" + ("" if not s["errors"] else ": " + "; ".join(f"{e['category']}({e['msg'][:60]})" for e in s["errors"][:5])))
    if s["api_issues"]:
        print("API 이상 동작:", s["api_issues"])
    if a.json:
        Path(a.json).write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
