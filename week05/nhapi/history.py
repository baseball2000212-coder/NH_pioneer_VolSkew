"""분봉 이력 나눠 받기 · 이어 붙이기 · 중복 제거 · 공백 점검.

해외 `overseas_chart-minute` 는 쪽당 줄 수가 정해져 있고(`req_qty` 와 무관하게 ~495줄 관측) 응답의
`nxt_key` 를 다음 요청에 넣어 더 오래된 쪽을 받습니다. 줄은 최신 → 과거 순입니다.

- 중복: `(ref_dt, exch_tm)` 가 같은 줄은 한 줄만 남깁니다 (쪽 경계에서 겹칠 수 있음).
- 멈춤: 줄이 없음 / 키가 비었거나 9 로만 이뤄짐 / 직전 키와 같음 / **새 줄이 하나도 없음**(키를 넣어도 같은 쪽 반복, ISSUE-003 유형).
- 조회 API 만 재시도합니다 (`Client.query`). 주문과 무관.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

FIELDS = ("ref_dt", "glb_biz_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty", "cum_trd_amt")


def slim_row(row: dict[str, Any]) -> dict[str, str]:
    """원문 한 줄에서 쓰는 칸만 뽑고 `occurs1_` 접두를 뗍니다."""
    return {name: str(row.get(f"occurs1_{name}", "") or "") for name in FIELDS}


def row_key(row: dict[str, str]) -> tuple[str, str]:
    return (row["ref_dt"], row["exch_tm"].zfill(6))


def dedup_rows(rows: Iterable[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """중복 시각을 제거하고 오래된 봉부터 정렬합니다. (결과, 제거한 줄 수)."""
    seen: dict[tuple[str, str], dict[str, str]] = {}
    removed = 0
    for row in rows:
        key = row_key(row)
        if key in seen:
            removed += 1
            continue
        seen[key] = row
    return [seen[k] for k in sorted(seen)], removed


def _is_end_key(key: str) -> bool:
    return not key or set(key) <= {"9"}


@dataclass
class CollectReport:
    sym: str
    pages: int = 0
    raw_rows: int = 0
    duplicate_rows: int = 0
    rows: int = 0
    min_rows_per_page: int = 0
    max_rows_per_page: int = 0
    oldest: str = ""
    newest: str = ""
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def collect_overseas_minute(
    client: Any,
    exch_cd: str,
    sym: str,
    *,
    start_date: str,
    end_date: str,
    max_pages: int = 400,
    min_gap: float = 0.35,
    sleep: Callable[[float], None] = time.sleep,
    on_page: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, str]], CollectReport]:
    """해외 분봉을 `start_date`(YYYYMMDD)까지 나눠 받아 중복 없이 오래된 순으로 돌려줍니다.

    호출 제한을 넘지 않도록 쪽 사이에 `min_gap` 초를 둡니다. 오류(`ETR029` 등)는 그대로 올립니다.
    """
    payload = {"exch_cd": exch_cd, "sym": sym, "inq_strt_dt": start_date, "strt_tm": "000000", "inq_ed_dt": end_date,
              "ed_tm": "235959", "req_qty": "300", "dcnt": "1", "cidx_yn": "0", "nxt_key": "99999999"}
    report = CollectReport(sym=sym)
    collected: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    sizes: list[int] = []
    for page in range(1, max_pages + 1):
        body = client.query("overseas_chart-minute", payload).body
        raw = body.get("Occurs1") or []
        report.pages = page
        sizes.append(len(raw))
        report.raw_rows += len(raw)
        if on_page:
            on_page(page, len(raw))
        if not raw:
            report.stop_reason = "no_rows"
            break
        fresh = 0
        for item in raw:
            row = slim_row(item)
            key = row_key(row)
            if key in seen:
                report.duplicate_rows += 1
                continue
            seen.add(key)
            collected.append(row)
            fresh += 1
        next_key = str(body.get("nxt_key") or "").strip()
        if fresh == 0:
            report.stop_reason = "repeated_page"
            break
        if _is_end_key(next_key):
            report.stop_reason = "end_key"
            break
        if next_key == payload["nxt_key"]:
            report.stop_reason = "same_key"
            break
        payload["nxt_key"] = next_key
        sleep(min_gap)
    else:
        report.stop_reason = "max_pages"
    rows, removed = dedup_rows(collected)
    report.duplicate_rows += removed
    report.rows = len(rows)
    if sizes:
        report.min_rows_per_page, report.max_rows_per_page = min(sizes), max(sizes)
    if rows:
        report.oldest = "".join(row_key(rows[0]))
        report.newest = "".join(row_key(rows[-1]))
    return rows, report


def row_time(row: dict[str, str]) -> datetime:
    return datetime.strptime("".join(row_key(row)), "%Y%m%d%H%M%S")


def gap_report(rows: list[dict[str, str]], *, break_minutes: int = 5) -> dict[str, Any]:
    """오래된 순 목록의 간격 분포. `break_minutes` 초과는 휴장·주말 등 '큰 공백'으로 센다."""
    times = [row_time(r) for r in rows]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    n = len(gaps)
    return {
        "bars": len(rows),
        "one_minute_ratio": round(sum(1 for g in gaps if g == 1) / n, 4) if n else None,
        "small_gap_ratio": round(sum(1 for g in gaps if 1 < g <= break_minutes) / n, 4) if n else None,
        "big_gaps": sum(1 for g in gaps if g > break_minutes),
        "max_gap_minutes": max(gaps) if gaps else None,
    }
