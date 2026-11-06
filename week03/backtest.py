"""[백테스트] — 과거 분봉으로 `DayEngine` 을 하루씩 돌립니다. 실제 주문·API 호출은 없습니다.

체결 모델(`backtest.fill_model`, CLI `--fill-model`):
  level     : 엔진이 낸 기준가(돌파 수준·봉 시가·손절선) 그대로. 원래 방식이고 README 6-1 수치가 이것입니다.
  close     : 봉 안에서 '닿은 수준가'(규칙 2 돌파 진입, 손절)는 감지한 봉의 종가로 체결. 실시간은 5초 조회로 감지한 뒤
              시장가를 내므로 수준가에 정확히 체결되지 않습니다. 봉 시가로 정한 진입·청산(시간 청산, 규칙 3 진입)은 그대로.
  next_open : 위와 같되 다음 봉 시가로(그날 마지막 봉이면 종가).
어느 모델이든 그 위에 설정한 슬리피지(틱)를 불리하게 더합니다. 호가·체결 큐는 없습니다.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from executors import Fill, SimExecutor
from feeds import ZtSeries
from gate_store import GateStore
from nhapi.journal import Journal
from nhapi.risk import KillSwitch, RiskLimits, RiskManager
from sessions import hm, kst_day, ny_minutes
from strategy_core import Bar, DayEngine, Intent

FILL_MODELS = ("level", "close", "next_open")


def fill_model_of(cfg: dict[str, Any]) -> str:
    """설정의 체결 모델. 칸이 없으면(옛 설정) 원래 방식 level."""
    model = str(cfg.get("backtest", {}).get("fill_model", "level"))
    if model not in FILL_MODELS:
        raise ValueError(f"알 수 없는 체결 모델: {model!r} ({', '.join(FILL_MODELS)})")
    return model


class BarFillExecutor(SimExecutor):
    """[백테스트] 체결 모델(모듈 설명 참고). 그날 분봉을 `set_day` 로 받아 감지 봉 종가·다음 봉 시가를 찾습니다."""

    def __init__(self, tick: float, slippage_ticks: float = 0.0, fill_model: str = "level") -> None:
        super().__init__("backtest-model", tick, slippage_ticks)
        if fill_model not in FILL_MODELS:
            raise ValueError(f"알 수 없는 체결 모델: {fill_model!r}")
        self.model = fill_model
        self._bars: dict = {}
        self._next: dict = {}

    def set_day(self, day_bars: list[Bar]) -> None:
        self._bars = {b.ts: b for b in day_bars}
        self._next = {a.ts: b for a, b in zip(day_bars, day_bars[1:])}

    def execute(self, intent: Intent, qty: int) -> Fill | None:
        price = intent.ref_price
        bar = self._bars.get(intent.ts)
        if intent.at_level and self.model != "level" and bar is not None:
            nxt = self._next.get(intent.ts) if self.model == "next_open" else None
            price = nxt.o if nxt is not None else bar.c
        adverse = intent.side if intent.kind == "enter" else -intent.side
        return Fill(price + adverse * self.slip, qty, self.source)


def make_engine(day: date, cfg: dict[str, Any], threshold: float | None, zt: ZtSeries, risk: RiskManager | None) -> DayEngine:
    s = cfg["strategy"]
    return DayEngine(
        day, cfg["params"], cfg["costs"], threshold=threshold, zt_move=zt.move, risk=risk,
        rule3_emergency_stop_usd=float(cfg["risk"].get("rule3_emergency_stop_usd", 0)),
        tick=float(s["tick"]), multiplier=float(s["multiplier"]), qty=int(s["order_qty"]),
        stop_gap_buffer_usd=float(cfg["risk"].get("stop_gap_buffer_usd", 0)),
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
            journal.fill(fill.source, sym, action=it.kind, rule=it.rule, price=fill.price, qty=fill.qty, order_no=fill.order_no, bar_ts=it.ts.isoformat(),
                         **({"note": fill.note} if fill.note else {}))
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
    executor = BarFillExecutor(float(cfg["strategy"]["tick"]), float(cfg["costs"].get("slippage_ticks", 0)), fill_model_of(cfg))
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
        executor.set_day(day_bars)
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


def diagnostics(trades: list[dict], daily: list[dict], entry_ny: str = "08:30") -> dict[str, Any]:
    """규칙 2 를 건너뛴 날의 사유별 일수와, 규칙 3 이 뉴욕 08:30 보다 늦게 진입한 건수(entry_tolerance_minutes 안).

    규칙 2 는 하루 한 번만 판단하므로 하루에 사유는 하나입니다(엔진 notes 문구로 가립니다).
    """
    skips = dict(range_bars_short=0, zt_missing=0, zt_mismatch=0, both_sides=0)
    for d in daily:
        notes = d["notes"]
        if "규칙 2: 범위 봉" in notes:
            skips["range_bars_short"] += 1
        elif "확인 불일치" in notes:
            skips["zt_missing" if "(변화 None)" in notes else "zt_mismatch"] += 1
        elif "양방향" in notes:
            skips["both_sides"] += 1
    late = [t for t in trades if t["rule"] == 3 and ny_minutes(datetime.fromisoformat(t["entry_ts"])) > hm(entry_ny)]
    return dict(rule2_skip_days=skips, rule3_late_entries=len(late), rule3_trades=sum(1 for t in trades if t["rule"] == 3))


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
        diagnostics=diagnostics(trades, daily),
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
