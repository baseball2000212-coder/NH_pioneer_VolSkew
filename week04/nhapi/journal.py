"""실행 기록 — 한 줄이 한 사건인 JSONL.

모든 줄에 공통으로 들어가는 칸:
    run_id, ts(ISO-8601 KST, ms), seq(실행 안에서 1씩 증가), env, mode, sym, kind

`kind` 는 `run · event · signal · order · fill · trade · error · api_issue` 여덟 갈래뿐입니다.
API 호출은 `event` 의 `name="api_call"` 로 남깁니다(`api_id`, `http_status`, `msg_cd`, `latency_ms`).

`mode` 는 어떤 실행인지 밝힙니다. 보고서의 표기와 이렇게 대응합니다.
    backtest → [백테스트] / paper-sim → [자체 시뮬레이션(종이 체결)] / paper-order → [모의계좌 주문]
    smoke → 조회 점검(주문 없음)
우리 시뮬레이터의 체결을 모의계좌 체결로 쓰지 않습니다 — `fill.source` 로도 구분합니다.

기록하기 전에 `masking.mask` 를 거쳐 인증정보·계좌 식별정보를 가립니다.
Decimal 은 문자열로 적어 정밀도를 잃지 않습니다.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .masking import mask
from .timeutil import iso_ms, now_kst

KINDS = ("run", "event", "signal", "order", "fill", "trade", "error", "api_issue")
MODES = ("backtest", "paper-sim", "paper-order", "smoke")

MODE_LABELS = {
    "backtest": "[백테스트]",
    "paper-sim": "[자체 시뮬레이션(종이 체결)]",
    "paper-order": "[모의계좌 주문]",
    "smoke": "[조회 점검]",
}


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return iso_ms(value) if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


class Journal:
    """JSONL 기록기. 줄마다 바로 내보내므로 프로세스가 죽어도 앞부분이 남습니다."""

    def __init__(
        self,
        path: Path | str,
        run_id: str,
        *,
        env: str = "paper",
        mode: str = "smoke",
        sym: str = "",
        clock: Callable[[], datetime] = now_kst,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"알 수 없는 mode 입니다: {mode!r} ({', '.join(MODES)})")
        self.path = Path(path)
        self.run_id = run_id
        self.env = env
        self.mode = mode
        self.sym = sym
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, sym: str | None = None, /, **fields: Any) -> dict[str, Any]:
        """한 줄을 적고, 적은 내용(가린 뒤)을 돌려줍니다."""
        if kind not in KINDS:
            raise ValueError(f"알 수 없는 kind 입니다: {kind!r} ({', '.join(KINDS)})")
        with self._lock:
            self._seq += 1
            record: dict[str, Any] = {
                "run_id": self.run_id,
                "ts": iso_ms(self._clock()),
                "seq": self._seq,
                "env": self.env,
                "mode": self.mode,
                "sym": self.sym if sym is None else sym,
                "kind": kind,
            }
            record.update(fields)
            safe = mask(json.loads(json.dumps(record, ensure_ascii=False, default=_default)))
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
        return safe

    # 갈래별 도우미 -----------------------------------------------------

    def run(self, phase: str, **fields: Any) -> dict[str, Any]:
        return self.write("run", phase=phase, **fields)

    def event(self, name: str, text: str = "", sym: str | None = None, **data: Any) -> dict[str, Any]:
        return self.write("event", sym, name=name, text=text, **data)

    def api_call(
        self,
        api_id: str,
        *,
        http_status: int | None,
        msg_cd: str = "",
        latency_ms: float | None = None,
        attempt: int = 1,
        sym: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.write(
            "event",
            sym,
            name="api_call",
            api_id=api_id,
            http_status=http_status,
            msg_cd=msg_cd,
            latency_ms=None if latency_ms is None else round(latency_ms, 1),
            attempt=attempt,
            **extra,
        )

    def signal(self, side: str, reason: str, sym: str | None = None, **fields: Any) -> dict[str, Any]:
        return self.write("signal", sym, side=side, reason=reason, **fields)

    def order(self, stage: str, sym: str | None = None, **fields: Any) -> dict[str, Any]:
        return self.write("order", sym, stage=stage, **fields)

    def fill(self, source: str, sym: str | None = None, **fields: Any) -> dict[str, Any]:
        return self.write("fill", sym, source=source, **fields)

    def trade(self, sym: str | None = None, **fields: Any) -> dict[str, Any]:
        return self.write("trade", sym, **fields)

    def error(self, category: str, sym: str | None = None, **fields: Any) -> dict[str, Any]:
        return self.write("error", sym, category=category, **fields)

    def api_issue(self, issue_id: str, api_id: str, *, known_discrepancy: bool = False, **fields: Any) -> dict[str, Any]:
        return self.write("api_issue", None, issue_id=issue_id, api_id=api_id, known_discrepancy=known_discrepancy, **fields)


def read_journal(path: Path | str, run_id: str | None = None) -> Iterator[dict[str, Any]]:
    """기록 파일을 **읽기만** 합니다. `run_id` 를 주면 그 실행의 줄만 냅니다. 깨진 줄은 건너뜁니다."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id is None or record.get("run_id") == run_id:
                yield record
