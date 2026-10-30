"""쉬는 날 사후 점검(읽기 전용, 주문 없음) — 규칙이 '쉼'이라 거래하지 않은 날, 규칙을 무시하고 거래했다면 어땠는지 NH 모의 서버 분봉으로 계산한다.

거래가 아니라 **참고용 사후 계산**이다. 체결가는 15:20 봉 시가·15:34 봉 종가, 비용은 strategy.json 의 cost_pt_roundtrip.
    python skipday_review.py --dates 20261028 20261029 20261030 --out results/skipday_review.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

import strategy_core as core  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--strategy", default=str(HERE / "strategy.json"))
    ap.add_argument("--out", type=Path, default=HERE / "results" / "skipday_review.json")
    args = ap.parse_args(argv)

    from nhapi import Client, Journal, load_config
    from nhapi.parsers import parse_bars
    from nhapi.timeutil import now_kst, yyyymmdd

    cfg = core.StrategyConfig.from_dict(json.loads(Path(args.strategy).read_text(encoding="utf-8")))
    run_id = f"skipreview-{now_kst():%Y%m%d-%H%M%S}"
    journal = Journal(HERE / "logs" / f"{run_id}.jsonl", run_id, mode="smoke", sym=cfg.sym)
    client = Client(load_config(), journal=journal)
    last = datetime.strptime(max(args.dates), "%Y%m%d")
    first = datetime.strptime(min(args.dates), "%Y%m%d") - timedelta(days=7)
    body = client.query("domestic_chart-minute", {
        "sym": cfg.sym, "inq_strt_dt": yyyymmdd(first), "inq_ed_dt": yyyymmdd(last), "strt_tm": "000000", "ed_tm": "235959", "dcnt": "1",
        "nght_tp": "1", "cidx_yn": "0", "fut_cidx_exp_mod": "0", "nxt_key": "99999999", "req_qty": "300"}).body
    bars = {(b.ts.strftime("%Y%m%d"), b.ts.strftime("%H%M")): b for b in parse_bars(body) if b.ts}
    days = sorted({d for d, _ in bars})
    out = {"label": "[참고용 사후 계산] 거래 아님", "source": "NH 모의 서버 분봉(domestic_chart-minute)", "cost_pt": str(cfg.cost_pt), "days": []}
    for date in sorted(args.dates):
        prev = days[days.index(date) - 1] if date in days and days.index(date) > 0 else None
        need = [(date, k) for k in ("1519", "1520", "1534")]
        if prev is None or (prev, "1545") not in bars or any(k not in bars for k in need):
            out["days"].append({"date": date, "error": "필요한 봉이 없음"})
            continue
        prev_close, p1519 = bars[(prev, "1545")].close, bars[(date, "1519")].close
        entry, exit_ = bars[(date, "1520")].open, bars[(date, "1534")].close
        ret = core.day_return_pct(prev_close, p1519)
        side = "buy" if ret > 0 else "sell"
        follow = core.pnl_pt(side, entry, exit_, cfg.cost_pt)
        reverse = core.pnl_pt(core.opposite(side), entry, exit_, cfg.cost_pt)
        rule = core.decide(ret, cfg.threshold_pct)
        out["days"].append({
            "date": date, "prev_close_date": prev, "prev_close": str(prev_close), "close_1519": str(p1519), "ret_pct": f"{ret:.3f}",
            "rule_decision": rule.reason, "entry_1520_open": str(entry), "exit_1534_close": str(exit_),
            "if_follow_net_pt": str(follow), "if_reverse_net_pt": str(reverse),
        })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for d in out["days"]:
        print(d)
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
