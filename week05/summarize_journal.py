"""실행 기록(JSONL) 요약. 기록은 읽기만 하고 `--run` 으로 그 실행의 줄만 거른다.

  python summarize_journal.py --journal ../logs/sim-1118.jsonl --run sim-1118 --out-dir results

만드는 것: results/<run_id>.jsonl (그 실행의 줄만 거른 사본), results/<run_id>_summary.json, 콘솔 표.
수치는 전부 기록에서 계산한다. 체결 출처(`source`)별로 따로 센다 (paper-sim 과 mock-account 를 섞지 않음).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def read_run(path: Path, run_id: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("run_id") == run_id:
                rows.append(rec)
    return rows


def summarize(rows: list[dict]) -> dict:
    kinds = Counter(r["kind"] for r in rows)
    trades = [r for r in rows if r["kind"] == "trade"]
    fills = [r for r in rows if r["kind"] == "fill"]
    orders = [r for r in rows if r["kind"] == "order"]
    errors = [r for r in rows if r["kind"] == "error"]
    net = [float(t.get("net_usd", 0)) for t in trades]
    gross = [float(t.get("gross_usd", 0)) for t in trades]
    reasons = Counter(t.get("reason") for t in trades)
    events = Counter(r.get("name") for r in rows if r["kind"] == "event")
    api_calls = [r for r in rows if r["kind"] == "event" and r.get("name") == "api_call"]
    summary_rec = next((r for r in rows if r["kind"] == "run" and r.get("phase") == "summary"), {})
    return {
        "run_id": rows[0]["run_id"] if rows else None, "mode": rows[0]["mode"] if rows else None,
        "first_ts": rows[0]["ts"] if rows else None, "last_ts": rows[-1]["ts"] if rows else None,
        "lines_by_kind": dict(kinds), "bars_evaluated": events.get("bar", 0),
        "signals": kinds.get("signal", 0),
        "orders_by_stage": dict(Counter((o.get("stage"), o.get("status")) .__repr__() for o in orders)),
        "fills_by_source": dict(Counter(f.get("source") for f in fills)),
        "trades": len(trades), "net_usd": round(sum(net), 2), "gross_usd": round(sum(gross), 2),
        "win_rate": round(sum(1 for x in net if x > 0) / len(net), 3) if net else None,
        "exit_reasons": dict(reasons), "skipped_entries": dict(Counter(r.get("why") for r in rows if r.get("name") == "skip_entry")),
        "errors": [{"category": e.get("category"), "msg": str(e.get("msg") or e.get("note") or "")[:100]} for e in errors][:20],
        "engine_summary": {k: summary_rec.get(k) for k in ("reason", "flat", "trades", "signals", "net_usd")},
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="실행 기록 요약")
    p.add_argument("--journal", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--out-dir", default=str(HERE / "results"))
    a = p.parse_args(argv)
    rows = read_run(Path(a.journal), a.run)
    if not rows:
        print(f"run_id {a.run} 줄이 없습니다.")
        return 1
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.run}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    res = summarize(rows)
    (out / f"{a.run}_summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
