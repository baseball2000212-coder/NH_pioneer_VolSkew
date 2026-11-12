"""[백테스트] 기록된 호가로 시장을 흉내 낸다. 시계는 엔진이 sleep 한 만큼만 간다.

자료(`data/`): `<YYYYMMDD>_ew2_chain.csv`(옵션 체인, 5초 스냅숏) · `<YYYYMMDD>_ew2_fut.csv`(ESZ26·MESZ26 호가).
가정(결과 JSON `assumptions` 에도 저장):
  - 스냅숏 사이에는 직전 호가를 유지한다(5초 간격, 봉 내부 가격 경로 가정 없음).
  - 체결은 맞은편 호가 전량(SimBroker). 체결 지연은 `latency` 초 뒤의 호가로 정한다.
  - MES 기록이 없으면 ES 호가로 대신한다.
표준 라이브러리만 쓴다(csv·bisect).
"""
from __future__ import annotations

import bisect
import csv
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from strategy_core import Q, parse_opt_sym


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ReplayMarket:
    def __init__(self, data_dir: Path, day: str, start: datetime) -> None:
        base = Path(data_dir) / f"{day}_ew2_"
        self.files = [Path(str(base) + "chain.csv"), Path(str(base) + "fut.csv")]
        snaps: dict[datetime, dict[tuple[str, float], Q]] = {}
        with self.files[0].open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                t = datetime.fromisoformat(r["ts"])
                snap = snaps.setdefault(t, {})
                k = float(r["K"])
                snap[("C", k)] = Q(float(r["c_bid"]), float(r["c_ask"]), float(r["c_bq"]), float(r["c_aq"]), t)
                snap[("P", k)] = Q(float(r["p_bid"]), float(r["p_ask"]), float(r["p_bq"]), float(r["p_aq"]), t)
        self.times = sorted(snaps)
        self.chain = [snaps[t] for t in self.times]
        raw: dict[str, list[tuple[datetime, Q]]] = {}
        with self.files[1].open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                t = datetime.fromisoformat(r["ts"])
                raw.setdefault(r["sym"], []).append((t, Q(float(r["bid"]), float(r["ask"]), float(r["bq"]), float(r["aq"]), t)))
        self.fut: dict[str, tuple[list[datetime], list[Q]]] = {}
        for sym, rows in raw.items():
            rows.sort(key=lambda x: x[0])
            self.fut[sym] = ([t for t, _ in rows], [q for _, q in rows])
        self.now = start

    def input_hashes(self) -> dict[str, str]:
        return {p.name: sha256(p) for p in self.files}

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def quote(self, sym: str, ahead: float = 0.0) -> Q | None:
        """`ahead` 초 뒤의 호가(체결 지연 가정). 0 이면 지금 호가."""
        at = self.now + timedelta(seconds=ahead)
        parsed = parse_opt_sym(sym)
        if parsed is not None:
            i = bisect.bisect_right(self.times, at) - 1
            return None if i < 0 else self.chain[i].get((parsed[1], parsed[2]))
        key = sym if sym in self.fut else ("ESZ26" if sym.startswith("MES") else sym)
        if key not in self.fut:
            return None
        ts, qs = self.fut[key]
        i = bisect.bisect_right(ts, at) - 1
        return None if i < 0 else qs[i]
