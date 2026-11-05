"""[백테스트] — 과거 분봉으로 `DayEngine` 을 하루씩 돌립니다. 실제 주문·API 호출은 없습니다.

체결 모델: 엔진이 낸 기준가(돌파 수준·봉 시가·손절선)에 설정한 슬리피지(틱)만 불리하게 더합니다. 호가·체결 큐는 없습니다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from executors import Fill, SimExecutor
from feeds import ZtSeries
from gate_store import GateStore
from nhapi.journal import Journal
from nhapi.risk import KillSwitch, RiskLimits, RiskManager
from sessions import kst_day
from strategy_core import Bar, DayEngine, Intent


def make_engine(day: date, cfg: dict[str, Any], threshold: float | None, zt: ZtSeries, risk: RiskManager | None) -> DayEngine:
    s = cfg["strategy"]
    return DayEngine(
        day, cfg["params"], cfg["costs"], threshold=threshold, zt_move=zt.move, risk=risk,
        rule3_emergency_stop_usd=float(cfg["risk"].get("rule3_emergency_stop_usd", 0)),
        tick=float(s["tick"]), multiplier=float(s["multiplier"]), qty=int(s["order_qty"]),
    )


def handle(engine: DayEngine, intents: list[Intent], executor, journal: Journal | None, qty: int, sym: str) -> list[Intent]:
    """의도를 실행기에 넘겨 체결을 엔진에 반영하고 기록합니다. 처리하지 못한 의도(체결 확인 실패)는 돌려줍니다."""
    failed: list[Intent] = []
    for it in intents:
        if journal is not None:
            journal.signal("buy" if (it.side > 0) == (it.kind == "enter") else "sell", it.reason, sym,
                           action=it.kind, rule=it.rule, ref_price=it.ref_price, bar_ts=it.ts.isoformat())
        fill = executor.execute(it, qty)
        if fill is None:
            failed.append(it)
            if it.kind == "enter":
                engine.cancel_entry()
            continue
        if journal is not None:
            journal.fill(fill.source, sym, action=it.kind, rule=it.rule, price=fill.price, qty=fill.qty, order_no=fill.order_no, bar_ts=it.ts.isoformat())
        if it.kind == "enter":
            engine.apply_entry(fill.price)
        else:
            rec = engine.apply_exit(it, fill.price, it.ts)
            if journal is not None:
                journal.trade(sym, rule=rec.rule, side=rec.side, entry=rec.entry, exit=rec.exit, usd=round(rec.usd, 2),
                              entry_ts=rec.entry_ts.isoformat(), exit_ts=rec.exit_ts.isoformat(), reason=rec.reason, source=fill.source)
    return failed


def run_backtest(bars: list[Bar], zt: ZtSeries, cfg: dict[str, Any], store: GateStore, *, journal: Journal | None = None,
                 start: date | None = None, end: date | None = None) -> tuple[list[dict], list[dict]]:
    """반환: (거래 목록, 일별 목록)."""
    sym, qty = cfg["strategy"]["sym"], int(cfg["strategy"]["order_qty"])
    g = cfg["params"]["gate"]
    executor = SimExecutor("backtest-model", float(cfg["strategy"]["tick"]), float(cfg["costs"].get("slippage_ticks", 0)))
    limits = RiskLimits.from_dict(cfg["risk"])
    by_day: dict[date, list[Bar]] = defaultdict(list)
    for b in bars:
        by_day[kst_day(b.ts)].append(b)
    trades, daily = [], []
    for day in sorted(by_day):
        if (start and day < start) or (end and day > end) or day.weekday() >= 5:
            continue
        thr = store.threshold(day, float(g["quantile"]), int(g["min_history_days"]))
        engine = make_engine(day, cfg, thr, zt, RiskManager(limits, KillSwitch()))
        day_bars = sorted(by_day[day], key=lambda b: b.ts)
        for b in day_bars:
            handle(engine, engine.on_bar(b, True), executor, journal, qty, sym)
        if engine.position is not None:        # 마감까지 남은 포지션은 마지막 봉 종가로 강제 정리
            last = day_bars[-1]
            handle(engine, [engine.flatten_intent(last, "당일 마감 강제 청산")], executor, journal, qty, sym)
        if engine.gate_pass is None and day_bars and engine.rv > 0:
            engine._decide_gate()
        store.add(day, engine.rv)
        if journal is not None:
            journal.event("day", f"{day} 게이트 {engine.gate_pass}", sym, day=day.isoformat(), rv=engine.rv, threshold=thr,
                          gate_pass=engine.gate_pass, gate_note=engine.gate_note, notes=engine.notes, pnl=round(engine.realized, 2),
                          bars=len(day_bars))
        for t in engine.trades:
            trades.append(dict(day=day.isoformat(), rule=t.rule, side=t.side, entry=t.entry, exit=t.exit, entry_ts=t.entry_ts.isoformat(),
                               exit_ts=t.exit_ts.isoformat(), usd=round(t.usd, 2), reason=t.reason))
        daily.append(dict(day=day.isoformat(), bars=len(day_bars), rv=engine.rv, threshold=thr, gate_pass=engine.gate_pass,
                          n_trades=len(engine.trades), pnl=round(engine.realized, 2), notes=" | ".join(engine.notes)))
    return trades, daily


def summarize(trades: list[dict], daily: list[dict]) -> dict[str, Any]:
    pnl_by_day = {d["day"]: d["pnl"] for d in daily}
    traded = [d for d in daily if d["n_trades"] > 0]
    vals = [d["pnl"] for d in traded]
    n = len(vals)
    cum, peak, mdd = 0.0, 0.0, 0.0
    for d in daily:
        cum += d["pnl"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    mean = sum(vals) / n if n else float("nan")
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else float("nan")
    by_rule: dict[str, dict] = {}
    for rule in (2, 3):
        x = [t["usd"] for t in trades if t["rule"] == rule]
        by_rule[str(rule)] = dict(trades=len(x), mean_usd=round(sum(x) / len(x), 2) if x else None, win_rate=round(sum(1 for v in x if v > 0) / len(x), 3) if x else None)
    return dict(
        days=len(daily), gate_pass_days=sum(1 for d in daily if d["gate_pass"]), gate_unknown_days=sum(1 for d in daily if d["gate_pass"] is None),
        trading_days=n, trades=len(trades), total_usd=round(sum(pnl_by_day.values()), 2),
        mean_per_trading_day=round(mean, 2) if n else None, t_stat=round(mean / (sd / math.sqrt(n)), 2) if n > 1 and sd > 0 else None,
        win_rate_days=round(sum(1 for v in vals if v > 0) / n, 3) if n else None, max_drawdown_usd=round(mdd, 2),
        worst_day_usd=round(min(vals), 2) if vals else None, by_rule=by_rule,
    )


def write_outputs(out_dir: Path, run_id: str, trades: list[dict], daily: list[dict], summary: dict, assumptions: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("trades", trades), ("daily", daily)):
        path = out_dir / f"{run_id}_{name}.csv"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            if rows:
                w = csv.DictWriter(handle, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    (out_dir / f"{run_id}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"{run_id}_assumptions.json").write_text(json.dumps(assumptions, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
