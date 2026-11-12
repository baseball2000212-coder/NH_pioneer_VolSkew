"""엔진의 사건 기록을 CLAUDE.md §6 형식(한 줄 = 한 사건 JSONL, 갈래 run·event·signal·order·fill·trade·error·api_issue)으로 바꿔 적는다.

모든 줄에 run_id·ts·env·mode·sym 이 들어간다(nhapi.journal.Journal). 체결(fill)에는 `source` 를 적어
backtest-model / paper-sim / mock-account 가 섞이지 않게 한다. 키·계좌 식별정보는 Journal 이 가린다.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from nhapi.journal import Journal, read_journal

FILL_SOURCE = {"backtest": "backtest-model", "paper-sim": "paper-sim", "paper-order": "mock-account"}
#: 화면(콘솔)에 찍는 사건
CONSOLE_KINDS = {"start", "startup", "wait_entry", "waiting", "resume", "entry", "order", "fill", "hedge", "stop", "exit_start", "summary", "finish", "end", "warn", "recon_fix"}
#: 오류 제보 후보로 따로 표시하는 거부 코드 (예: 옵션 신규매도 7093, 주문가능수량 조회는 가능이라 답함)
API_ISSUE_CODES = {"7093": "해외 옵션 신규매도 거부(주문가능수량 조회와 불일치 후보)"}


class EngineJournal:
    """`Engine(log=...)` 에 넘기는 호출 가능 객체."""

    def __init__(self, journal: Journal, *, series: str, mes_sym: str, console: bool = True, console_state_every: float = 60.0) -> None:
        self.j, self.series, self.mes_sym = journal, series, mes_sym
        self.console, self._every, self._last_state = console, console_state_every, None
        self.summary: dict[str, Any] | None = None
        self.fills: list[dict[str, Any]] = []
        self.source = FILL_SOURCE[journal.mode]

    def __call__(self, rec: dict[str, Any]) -> None:
        rec = dict(rec)
        kind = rec.pop("kind")
        ts = rec.pop("ts", None)
        eng_state = rec.pop("state", None)
        sym = rec.pop("sym", None)
        fields = {"engine_state": eng_state, **rec}
        j = self.j
        if kind == "start":
            j.run("start", **fields)
        elif kind == "end":
            j.run("end", **fields)
        elif kind == "summary":
            self.summary = {k: v for k, v in rec.items()}
            j.run("summary", **fields)
        elif kind == "order":
            stage = fields.pop("why", "") or "order"
            j.order(stage, sym, **fields)
            code = str(rec.get("code", ""))
            if rec.get("status") == "rejected" and code in API_ISSUE_CODES:
                j.api_issue(f"CAND-{code}", "overseas_order", known_discrepancy=False, sym=sym, code=code,
                            note=API_ISSUE_CODES[code], msg=rec.get("msg", ""))
        elif kind == "fill":
            self.fills.append({"ts": ts, "sym": sym, **{k: rec.get(k) for k in ("why", "signed", "px", "px_estimated")}})
            j.fill(self.source, sym, **fields)
        elif kind == "hedge":
            side = "buy" if rec.get("order", 0) > 0 else "sell"
            j.signal(side, "delta_mismatch", self.mes_sym, **fields)
        elif kind == "entry":
            j.signal("buy", "enter_straddle", self.series, **fields)
        elif kind == "warn":
            what = fields.pop("what", "warn")
            j.error(what, sym, severity="warn", **fields)
        else:
            j.event(kind, "", sym, **fields)
        if self.console and kind in CONSOLE_KINDS:
            print(json.dumps({"ts": ts, "kind": kind, **rec}, ensure_ascii=False, default=str), flush=True)
        elif self.console and kind == "state" and ts:
            if self._last_state is None or (datetime.fromisoformat(ts) - datetime.fromisoformat(self._last_state)).total_seconds() >= self._every:
                self._last_state = ts
                keep = {k: rec.get(k) for k in ("F", "delta", "mes", "mismatch", "pnl_net", "attr", "theta_usd_per_hour")}
                print(json.dumps({"ts": ts, "kind": "state", **keep}, ensure_ascii=False, default=str), flush=True)


def realized_today(log_dir: Path, day_iso: str, mode: str, exclude_run: str = "") -> float:
    """오늘(`YYYY-MM-DD`) 같은 mode 로 끝난 실행들의 순손익 합계(요약 줄). 일일 손실 한도 점검에 쓴다. 기록은 읽기만 한다."""
    total = 0.0
    for path in sorted(Path(log_dir).glob("*.jsonl")):
        try:
            for rec in read_journal(path):
                if (rec.get("kind") == "run" and rec.get("phase") == "summary" and rec.get("mode") == mode
                        and str(rec.get("ts", "")).startswith(day_iso) and rec.get("run_id") != exclude_run):
                    total += float(rec.get("pnl_net") or 0.0)
        except OSError:
            continue
    return total
