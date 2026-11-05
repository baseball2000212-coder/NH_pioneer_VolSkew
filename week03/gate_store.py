"""게이트 이력 — 날짜별 중국장 RV(10:00~15:55 KST 1분 로그수익률 제곱합).

과거 이력은 `data/gate_history_seed.csv`(열: day, cn_rv)에서 읽습니다. 이 시드는 팀원이 준 GC 1분봉(2020~2026-10)에서
같은 정의로 계산한 값입니다. 오늘 이전의 모든 날(시드 + 이번 실행에서 쌓은 날)로 67 백분위를 구합니다.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from strategy_core import gate_threshold


class GateStore:
    def __init__(self, seed_path: Path | str | None = None) -> None:
        self.values: dict[date, float] = {}
        if seed_path and Path(seed_path).exists():
            with Path(seed_path).open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    self.values[datetime.strptime(row["day"], "%Y-%m-%d").date()] = float(row["cn_rv"])

    def add(self, day: date, rv: float) -> None:
        if rv > 0:
            self.values[day] = rv

    def history_before(self, day: date) -> list[float]:
        return [v for d, v in self.values.items() if d < day]

    def threshold(self, day: date, quantile: float, min_days: int) -> float | None:
        return gate_threshold(self.history_before(day), quantile, min_days)

    def save(self, path: Path | str) -> None:
        with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("day,cn_rv\n")
            for d in sorted(self.values):
                handle.write(f"{d.isoformat()},{self.values[d]!r}\n")
