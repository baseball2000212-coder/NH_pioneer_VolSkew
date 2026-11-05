"""분봉 읽기·쓰기와 ZT 변화 계산. API 호출은 `live.py` 가 하고, 여기는 파일과 계산만 합니다."""
from __future__ import annotations

import csv
import gzip
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from sessions import KST
from strategy_core import Bar


def _open_text(path, mode):
    path = Path(path)
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding="utf-8", newline="")
    return path.open(mode, encoding="utf-8", newline="")


def load_nh_json(path: Path | str) -> list[Bar]:
    """NH 해외 차트(분) 수집 파일(`{"rows": [...]}`, 줄은 occurs1_ 접두 없는 이름)에서 봉을 읽습니다."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))["rows"]
    bars = {}
    for r in rows:
        ts = datetime.strptime(r["ref_dt"] + r["exch_tm"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        bars[ts] = Bar(ts, float(r["open_pric"]), float(r["high_pric"]), float(r["low_pric"]), float(r["close_pric"]), float(r.get("cum_trd_qty") or 0))
    return [bars[t] for t in sorted(bars)]


def load_csv(path: Path | str) -> list[Bar]:
    """열: ts(ISO, 시간대 포함), o, h, l, c, v."""
    out = []
    with _open_text(path, "rt") as handle:
        for row in csv.DictReader(handle):
            out.append(Bar(datetime.fromisoformat(row["ts"]), float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row.get("v") or 0)))
    out.sort(key=lambda b: b.ts)
    return out


def save_csv(path: Path | str, bars: list[Bar]) -> None:
    with _open_text(path, "wt") as handle:
        handle.write("ts,o,h,l,c,v\n")
        for b in bars:
            handle.write(f"{b.ts.astimezone(KST).isoformat()},{b.o},{b.h},{b.l},{b.c},{b.v}\n")


class ZtSeries:
    """ZT 종가의 1분 격자(빈 분은 최대 10분까지 앞 값으로 채움). `move(end, minutes)` = ln 종가(end) − ln 종가(end−minutes)."""

    FILL_LIMIT = 10

    def __init__(self, bars: list[Bar] | None = None) -> None:
        self.close: dict[datetime, float] = {}
        for b in bars or []:
            self.close[b.ts.astimezone(KST).replace(second=0, microsecond=0)] = b.c

    def update(self, bars: list[Bar]) -> None:
        for b in bars:
            self.close[b.ts.astimezone(KST).replace(second=0, microsecond=0)] = b.c

    def _at(self, t: datetime) -> float | None:
        t = t.astimezone(KST).replace(second=0, microsecond=0)
        for back in range(self.FILL_LIMIT + 1):
            v = self.close.get(t - timedelta(minutes=back))
            if v is not None:
                return v
        return None

    def move(self, end: datetime, minutes: int) -> float | None:
        a, b = self._at(end), self._at(end - timedelta(minutes=minutes))
        if a is None or b is None:
            return None
        return math.log(a) - math.log(b)
