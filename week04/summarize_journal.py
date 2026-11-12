"""실행 기록(JSONL)에서 한 실행(run_id)의 줄만 걸러 results/ 에 사본과 요약 JSON 을 만든다. 기록 파일은 읽기만 한다(표준 라이브러리만).

  python summarize_journal.py --log ../logs/bt-w04-1110-2229.jsonl --run bt-w04-1110-2229 --out results

만드는 것
  results/<run>.jsonl          그 실행의 줄만 거른 사본 (원본 로그는 week04/logs/ 에 그대로 둔다)
  results/<run>_summary.json   환경 표기·시간·주문/체결 수·헤지 수·손익 분해·패리티 잔차·오류/api_issue 집계

README 의 모든 수치는 이 요약(또는 사본 JSONL)에서 계산한 값이다.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

LABELS = {"backtest": "[백테스트]", "paper-sim": "[자체 시뮬레이션(종이 체결)]", "paper-order": "[모의계좌 주문]", "smoke": "[조회 점검]"}


def read_run(path: Path, run_id: str) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id:
                rows.append(rec)
    return rows


def summarize(rows: list[dict]) -> dict:
    if not rows:
        raise SystemExit("해당 run_id 의 줄이 없습니다.")
    mode = rows[0]["mode"]
    orders = [r for r in rows if r["kind"] == "order"]
    fills = [r for r in rows if r["kind"] == "fill"]
    states = [r for r in rows if r["kind"] == "event" and r.get("name") == "state"]
    hedges = [r for r in rows if r["kind"] == "signal" and r.get("reason") == "delta_mismatch"]
    summary = next((r for r in rows if r["kind"] == "run" and r.get("phase") == "summary"), None)
    api_calls = [r for r in rows if r["kind"] == "event" and r.get("name") == "api_call"]
    out = {
        "run_id": rows[0]["run_id"], "mode": mode, "label": LABELS.get(mode, mode), "env": rows[0]["env"],
        "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"], "lines": len(rows),
        "kinds": dict(Counter(r["kind"] for r in rows)),
        "orders": {"by_stage": dict(Counter(r.get("stage", "") for r in orders)),
                   "by_status": dict(Counter(r.get("status", "") for r in orders))},
        "fills": {"n": len(fills), "sources": sorted({r.get("source", "") for r in fills}),
                  "contracts": sum(abs(int(r.get("signed") or 0)) for r in fills),
                  "price_estimated": sum(1 for r in fills if r.get("px_estimated"))},
        "hedges": {"n": len(hedges), "mes_ordered": sum(abs(int(r.get("order") or 0)) for r in hedges)},
        "errors": dict(Counter(r.get("category", "") for r in rows if r["kind"] == "error")),
        "api_issues": [{"issue_id": r.get("issue_id"), "api_id": r.get("api_id"), "note": r.get("note")} for r in rows if r["kind"] == "api_issue"],
        "api_calls": {"n": len(api_calls), "by_status": dict(Counter(str(r.get("http_status")) for r in api_calls)),
                      "by_msg_cd": dict(Counter(r.get("msg_cd", "") for r in api_calls if r.get("msg_cd"))),
                      "max_latency_ms": max((r.get("latency_ms") or 0 for r in api_calls), default=0)},
        "state_points": len(states),
        "max_abs_mismatch": max((abs(r["mismatch"]) for r in states if r.get("mismatch") is not None), default=None),
        "pnl_net_min": min((r["pnl_net"] for r in states if r.get("pnl_net") is not None), default=None),
        "pnl_net_max": max((r["pnl_net"] for r in states if r.get("pnl_net") is not None), default=None),
        "engine_summary": None if summary is None else {k: v for k, v in summary.items()
                                                           if k not in ("run_id", "ts", "seq", "env", "mode", "sym", "kind", "phase")},
        "finished": summary is not None,
    }
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="실행 기록에서 한 실행의 줄을 거르고 요약한다(읽기 전용)")
    p.add_argument("--log", required=True, type=Path)
    p.add_argument("--run", required=True)
    p.add_argument("--out", default="results", type=Path)
    a = p.parse_args(argv)
    rows = read_run(a.log, a.run)
    a.out.mkdir(parents=True, exist_ok=True)
    with (a.out / f"{a.run}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = summarize(rows)
    (a.out / f"{a.run}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("run_id", "label", "lines", "fills", "hedges", "engine_summary")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
