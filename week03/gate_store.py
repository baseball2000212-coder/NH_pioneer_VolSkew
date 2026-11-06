"""게이트 이력 — 날짜별 아시아 시간대 금 RV(10:00~15:55 KST 금 1분 로그수익률 제곱합. 중국 지수·A50 데이터는 쓰지 않음).

과거 이력은 `data/gate_history_seed.csv`(열: day, cn_rv)에서 읽습니다. 이 시드는 팀원이 준 GC 1분봉(2020~2026-10)에서
같은 정의로 계산한 값입니다. 오늘 이전의 모든 날(시드 + 이번 실행에서 쌓은 날)로 67 백분위를 구합니다.

실시간 실행(paper-sim·paper-order)은 시작할 때 시드에 `results/gate_history-*.csv`(그리고 옛 `gate_history_live.csv`)를
합쳐 읽고, 끝날 때 `results/gate_history-<run_id>.csv` 로 따로 씁니다. 최근 평일 중 빠진 날이 있으면 시작 때 경고합니다.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from strategy_core import gate_threshold


#: 실시간 실행이 남긴 게이트 이력 파일. run_id 마다 따로 씁니다(sim·ord 가 같은 파일을 덮어쓰지 않게).
LIVE_PATTERN = "gate_history-*.csv"
LEGACY_LIVE = "gate_history_live.csv"     # 리뷰 전 이름. 읽기만 합니다.


class GateStore:
    def __init__(self, seed_path: Path | str | None = None) -> None:
        self.values: dict[date, float] = {}
        self.load(seed_path)

    def load(self, path: Path | str | None) -> int:
        """이력 파일(열: day, cn_rv)을 합칩니다. 같은 날이 있으면 나중에 읽은 값이 씁니다. 읽은 줄 수를 돌려줍니다."""
        if not path or not Path(path).exists():
            return 0
        n = 0
        with Path(path).open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                self.values[datetime.strptime(row["day"], "%Y-%m-%d").date()] = float(row["cn_rv"])
                n += 1
        return n

    def load_live(self, folder: Path | str) -> list[Path]:
        """`folder` 의 실시간 이력 파일(옛 이름 포함)을 이름 순서로 합칩니다. 읽은 파일 목록을 돌려줍니다."""
        folder = Path(folder)
        files = ([folder / LEGACY_LIVE] if (folder / LEGACY_LIVE).exists() else []) + sorted(folder.glob(LIVE_PATTERN))
        for path in files:
            self.load(path)
        return files

    def missing_weekdays(self, today: date, lookback_days: int = 14) -> list[date]:
        """오늘 전 `lookback_days` 일 안에서 이력이 없는 평일(휴장일도 나오므로 경고용)."""
        out = []
        for i in range(lookback_days, 0, -1):
            d = date.fromordinal(today.toordinal() - i)
            if d.weekday() < 5 and d not in self.values:
                out.append(d)
        return out

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
