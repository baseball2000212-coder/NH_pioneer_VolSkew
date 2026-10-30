"""[백테스트] 장 막판 모멘텀(3% 규칙) — 표준 라이브러리만 쓴다.

두 단계:
    extract  팀 제공 1분봉 폴더(YYYYMMDD.csv) → 일별 입력표(results/backtest_input_daily.csv)
    run      일별 입력표 + strategy.json → 결과 JSON·거래표 CSV (가정이 결과 JSON 안에 들어간다)

사용 데이터와 가정 (결과 JSON `assumptions` 에도 그대로 저장):
    - 1분봉: 한국투자증권 API 로 받은 코스닥150 선물 최근월물 (팀 제공). NH 모의 서버 시세가 아니다.
    - 전일 종가 = 전 거래일 15:45 봉(종가 단일가) 종가. 판단 가격 = 15:19 봉 종가. 진입가 = 15:20 봉 시가.
    - 청산가 = 15:45 봉 종가(종가 단일가) [기본] 또는 15:25/15:30/15:34 봉 종가.
    - 비용 = 왕복 `cost_pt` = 0.5pt (모의 수수료 실측 약 0.25pt + 슬리피지 약 0.25pt 가정, 2026-10-28 스모크). 팀 원래 기록은 0.2pt.
    - 재난 손절: 15:20~청산 봉까지 연속매매 1분봉 고저가 손절선에 닿으면 손절선 가격에 체결(낙관적 가정).
      봉 안의 가격 경로는 알 수 없어 같은 봉에서 손절과 청산이 겹쳐도 손절을 먼저로 본다.
    - 제외: 첫날(전일 종가 없음), 만기일(최근월물이 15:20 에 끝남), 월물 교체 직후 첫날(전일 종가가 다른 월물).
    - 하루 최대 1회, 1계약.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import strategy_core as core
from strategy_core import StrategyConfig

TEAM_COST_PT = Decimal("0.2")     # 팀 원래 기록이 쓴 왕복 비용(대조용 고정값)
KST_NOTE = "시각은 모두 한국시간(KST)"
EXIT_BARS = ("1525", "1530", "1534", "1545")
CONT_BARS = ("1525", "1530", "1534")          # 연속매매 구간 누적 고저를 저장하는 봉
INPUT_FIELDS = ["date", "code", "prev_close", "p1519", "o1520", "c1525", "c1530", "c1534", "c1545",
                "hi_1525", "lo_1525", "hi_1530", "lo_1530", "hi_1534", "lo_1534"]


# ---------------------------------------------------------------------- 1) 원본 → 일별 입력

def _read_day(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["stck_cntg_hour"][:4]: row for row in csv.DictReader(handle)}


def extract_daily(raw_dir: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    files = sorted(raw_dir.glob("*.csv"))
    days = [(f.stem, _read_day(f)) for f in files]
    rows, skipped = [], {"first_day": 0, "missing_bars": 0, "expiry_day": 0, "roll_day": 0}
    for i, (date, bars) in enumerate(days):
        if i == 0:
            skipped["first_day"] += 1
            continue
        prev_date, prev = days[i - 1]
        if not {"1519", "1520", "1545"} <= bars.keys() or "1545" not in prev:
            skipped["missing_bars"] += 1
            continue
        code, prev_code = bars["1519"]["code"], prev["1545"]["code"]
        if code != prev_code:
            skipped["roll_day"] += 1
            continue
        if i + 1 < len(days) and days[i + 1][1].get("1519", {}).get("code", code) != code:
            skipped["expiry_day"] += 1
            continue
        row = {
            "date": date, "code": code, "prev_close": prev["1545"]["futs_prpr"], "p1519": bars["1519"]["futs_prpr"],
            "o1520": bars["1520"]["futs_oprc"], "c1545": bars["1545"]["futs_prpr"],
        }
        for key in CONT_BARS:
            row[f"c{key}"] = bars[key]["futs_prpr"] if key in bars else ""
            window = [b for k, b in bars.items() if "1520" <= k <= key]
            row[f"hi_{key}"] = str(max(Decimal(b["futs_hgpr"]) for b in window)) if window else ""
            row[f"lo_{key}"] = str(min(Decimal(b["futs_lwpr"]) for b in window)) if window else ""
        rows.append(row)
    return rows, {"raw_days": len(days), "used_days": len(rows), "skipped": skipped}


def write_input(rows: list[dict[str, str]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_input(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------- 2) 시뮬레이션

@dataclass(frozen=True)
class Trade:
    date: str
    side: str
    ret_pct: Decimal
    entry: Decimal
    exit: Decimal
    exit_reason: str   # scheduled / stop
    gross_pt: Decimal
    net_pt: Decimal


def simulate_day(row: dict[str, str], *, threshold: Decimal, cost: Decimal, exit_bar: str,
                 stop_pt: Decimal | None, stop_slip_pt: Decimal = Decimal(0)) -> Trade | None:
    prev, p1519, entry = Decimal(row["prev_close"]), Decimal(row["p1519"]), Decimal(row["o1520"])
    ret = core.day_return_pct(prev, p1519)
    decision = core.decide(ret, threshold)
    if decision.side is None:
        return None
    side = decision.side
    exit_price, reason = Decimal(row[f"c{exit_bar}"]), "scheduled"
    if stop_pt is not None:
        ext = exit_bar if exit_bar in CONT_BARS else "1534"
        stop = core.stop_price(side, entry, stop_pt)
        lo, hi = Decimal(row[f"lo_{ext}"]), Decimal(row[f"hi_{ext}"])
        if (side == "buy" and lo <= stop) or (side == "sell" and hi >= stop):
            exit_price = stop - stop_slip_pt if side == "buy" else stop + stop_slip_pt
            reason = "stop"
    gross = core.pnl_pt(side, entry, exit_price)
    return Trade(row["date"], side, ret, entry, exit_price, reason, gross, gross - cost)


def run_rule(rows: Iterable[dict[str, str]], **kw: Any) -> list[Trade]:
    return [t for t in (simulate_day(r, **kw) for r in rows) if t is not None]


# ---------------------------------------------------------------------- 3) 통계

def stats(trades: list[Trade], multiplier: Decimal) -> dict[str, Any]:
    pnl = [t.net_pt for t in trades]
    n = len(pnl)
    if n == 0:
        return {"n": 0}
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p <= 0]
    mean = sum(pnl) / n
    var = sum((p - mean) ** 2 for p in pnl) / (n - 1) if n > 1 else Decimal(0)
    std = Decimal(math.sqrt(var)) if var > 0 else Decimal(0)
    equity, peak, mdd = Decimal(0), Decimal(0), Decimal(0)
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    return {
        "n": n, "win_rate": round(len(wins) / n, 4), "mean_pt": float(round(mean, 4)), "mean_won": float(round(mean * multiplier, 0)),
        "sum_pt": float(round(sum(pnl), 2)), "profit_factor": round(float(sum(wins) / sum(losses)), 3) if losses and sum(losses) > 0 else None,
        "std_pt": float(round(std, 4)), "t_stat": round(float(mean / (std / Decimal(math.sqrt(n)))), 2) if std > 0 else None,
        "max_drawdown_pt": float(round(mdd, 2)), "worst_pt": float(min(pnl)), "best_pt": float(max(pnl)),
        "stop_hits": sum(1 for t in trades if t.exit_reason == "stop"),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_backtest(rows: list[dict[str, str]], cfg: StrategyConfig, *, input_path: Path | None = None, extract_info: dict | None = None) -> tuple[dict[str, Any], list[Trade]]:
    bt, mult = cfg.backtest, cfg.multiplier
    train_end = str(bt["train_end"])
    exit_bar = "1545" if cfg.exit_mode == "auction" else "1534"
    main_kw = dict(threshold=cfg.threshold_pct, cost=cfg.cost_pt, exit_bar=exit_bar, stop_pt=cfg.stop_pt)
    main = run_rule(rows, **main_kw)
    result: dict[str, Any] = {
        "label": "[백테스트]",
        "strategy": {"name": cfg.name, "version": cfg.version, "sym": cfg.sym, "contract": cfg.contract},
        "assumptions": {
            "data": "한국투자증권 API 코스닥150 선물 최근월물 1분봉(팀 제공). NH 모의 서버 시세가 아니다.",
            "definitions": {"prev_close": "전 거래일 15:45 봉 종가", "decision_price": "15:19 봉 종가", "entry": "15:20 봉 시가", "exit": "exit_mode=continuous 면 15:34 봉 종가(실행은 15:34:30 시장가), auction 이면 15:45 봉 종가"},
            "threshold_pct": str(cfg.threshold_pct), "cost_pt_roundtrip": str(cfg.cost_pt), "stop_pt": str(cfg.stop_pt),
            "stop_model": "15:20~15:34 연속매매 1분봉 고저가 손절선에 닿으면 손절선 가격에 체결(낙관적). 같은 봉 안의 경로는 모르므로 손절 우선.",
            "stop_slip_pt_main": "0", "qty": cfg.qty, "multiplier": str(mult),
            "exclusions": "첫날, 만기일, 월물 교체 직후 첫날, 필요한 봉이 없는 날",
            "train_end": train_end, "note": "임계값(3%)·손절폭은 같은 자료를 본 뒤 정한 값이라 선택 편향이 있다. 학습/검증 구분은 팀 기록의 기준일을 따랐다.",
            "kst": KST_NOTE,
        },
        "input": {"rows": len(rows), "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
                  "sha256": _sha256(input_path) if input_path else None, "extract": extract_info},
        "main": {"description": f"strategy.json 그대로 (임계 3%, 손절 10pt, 비용 cost_pt, 청산={exit_bar} 봉 종가)", **stats(main, mult),
                 "train": stats([t for t in main if t.date <= train_end], mult), "valid": stats([t for t in main if t.date > train_end], mult)},
    }
    no_stop = run_rule(rows, **{**main_kw, "stop_pt": None})
    result["no_stop_team_rule"] = {"description": "손절 없음(팀 원래 규칙)", **stats(no_stop, mult)}
    result["by_cost_pt"] = {str(c): stats(run_rule(rows, **{**main_kw, "cost": Decimal(c)}), mult) for c in bt["cost_pt_list"]}
    result["by_threshold_pct"] = {str(t): stats(run_rule(rows, **{**main_kw, "threshold": Decimal(t)}), mult) for t in bt["threshold_pct_list"]}
    result["by_exit_bar"] = {b: stats(run_rule(rows, **{**main_kw, "exit_bar": b}), mult) for b in bt["exit_list"]}
    result["by_stop_slip_pt"] = {s: stats(run_rule(rows, **{**main_kw, "stop_slip_pt": Decimal(s)}), mult) for s in ("0", "0.5", "1.0")}
    by_side = {s: stats([t for t in main if t.side == s], mult) for s in ("buy", "sell")}
    result["by_side"] = by_side
    by_month: dict[str, float] = {}
    for t in main:
        by_month[t.date[:6]] = round(by_month.get(t.date[:6], 0.0) + float(t.net_pt), 2)
    result["by_month_sum_pt"] = by_month
    return result, main


def trades_csv(trades: list[Trade], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["date", "side", "ret_pct", "entry", "exit", "exit_reason", "gross_pt", "net_pt"])
        for t in trades:
            w.writerow([t.date, t.side, f"{t.ret_pct:.4f}", t.entry, t.exit, t.exit_reason, t.gross_pt, t.net_pt])


def write_journal(trades: list[Trade], rows: list[dict[str, str]], cfg: StrategyConfig, path: Path, run_id: str) -> None:
    """백테스트도 같은 JSONL 형식으로 남긴다(mode=backtest). 거래일은 `trade_date` 칸."""
    from nhapi import Journal

    journal = Journal(path, run_id, mode="backtest", sym=cfg.sym)
    journal.run("start", strategy=cfg.name, version=cfg.version, rows=len(rows))
    by_date = {t.date: t for t in trades}
    for row in rows:
        ret = core.day_return_pct(Decimal(row["prev_close"]), Decimal(row["p1519"]))
        trade = by_date.get(row["date"])
        if trade is None:
            journal.signal("none", core.decide(ret, cfg.threshold_pct).reason, trade_date=row["date"], ret_pct=ret, price=row["p1519"], prev_close=row["prev_close"])
            continue
        journal.signal(trade.side, "enter", trade_date=row["date"], ret_pct=ret, price=row["p1519"], prev_close=row["prev_close"])
        journal.trade(trade_date=trade.date, side=trade.side, qty=cfg.qty, entry_price=trade.entry, exit_price=trade.exit,
                      exit_reason=trade.exit_reason, gross_pt=trade.gross_pt, assumed_cost_pt=cfg.cost_pt, net_pt=trade.net_pt,
                      net_won=core.pnl_won(trade.net_pt, cfg.qty, cfg.multiplier), source="backtest")
    journal.run("end", trades=len(trades))


def compare_with_team(rows: list[dict[str, str]], team_csv: Path, cfg: StrategyConfig) -> dict[str, Any]:
    """손절 없는 규칙을 팀원 매매 기록과 날짜별로 견준다."""
    with team_csv.open(encoding="utf-8-sig", newline="") as handle:
        team = {r["날짜"].replace("-", ""): r for r in csv.DictReader(handle)}
    mine = {t.date: t for t in run_rule(rows, threshold=cfg.threshold_pct, cost=TEAM_COST_PT, exit_bar="1545", stop_pt=None)}
    entered = {d for d, r in team.items() if r["진입여부"] == "진입"}
    same = [d for d in entered & mine.keys() if abs(Decimal(team[d]["손익(pt,비용차감)"]) - mine[d].net_pt) < Decimal("0.05")]
    return {"team_entries": len(entered), "my_entries": len(mine), "same_days": len(entered & mine.keys()), "same_pnl": len(same),
            "only_team": sorted(entered - mine.keys())[:5], "only_mine": sorted(mine.keys() - entered)[:5]}


# ---------------------------------------------------------------------- 4) CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract", help="1분봉 폴더 → 일별 입력표")
    ex.add_argument("--raw", type=Path, required=True)
    ex.add_argument("--out", type=Path, default=Path("results/backtest_input_daily.csv"))
    rn = sub.add_parser("run", help="일별 입력표 → 결과 JSON")
    rn.add_argument("--input", type=Path, default=Path("results/backtest_input_daily.csv"))
    rn.add_argument("--strategy", type=Path, default=Path("strategy.json"))
    rn.add_argument("--out", type=Path, default=Path("results/backtest_result.json"))
    rn.add_argument("--trades", type=Path, default=Path("results/backtest_trades.csv"))
    rn.add_argument("--journal", type=Path, default=None)
    rn.add_argument("--run-id", default=None)
    rn.add_argument("--team-ref", type=Path, default=None, help="팀원 매매 기록 CSV(있으면 손절 없는 규칙과 견줌)")
    args = ap.parse_args(argv)

    if args.cmd == "extract":
        rows, info = extract_daily(args.raw)
        write_input(rows, args.out)
        print(f"일별 입력표 {len(rows)}행 → {args.out}  (원본 {info['raw_days']}일, 제외 {info['skipped']})")
        return 0

    cfg = StrategyConfig.from_dict(json.loads(args.strategy.read_text(encoding="utf-8")))
    rows = read_input(args.input)
    result, trades = run_backtest(rows, cfg, input_path=args.input)
    if args.team_ref:
        result["team_reference_check"] = compare_with_team(rows, args.team_ref, cfg)
    run_id = args.run_id or datetime.now().strftime("backtest-%Y%m%d-%H%M%S")
    result["run_id"] = run_id
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    trades_csv(trades, args.trades)
    if args.journal:
        write_journal(trades, rows, cfg, args.journal, run_id)
    m = result["main"]
    print(f"[백테스트] {run_id}: 거래 {m['n']}건, 승률 {m['win_rate']:.0%}, 평균 {m['mean_pt']}pt, 합 {m['sum_pt']}pt, PF {m['profit_factor']}, "
          f"손절 {m['stop_hits']}건 → {args.out}")
    if args.team_ref:
        print("팀 기록 대조:", result["team_reference_check"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
