"""근월물 자동 선택과 특수일 달력 (P1).

- 종목 마스터(NH 공개 정적 파일, nhapi.instruments.MasterStore)의 `exp_ym` 은 실제로 최종거래일 YYYYMMDD 다
  (2026-11-05 첫 내려받기에서 확인: 코스닥150 12월물 A066C000 → 20261210).
- 최종거래일 당일: 근월물은 15:20 에 거래가 끝난다 → 그날은 매매하지 않는다(백테스트도 제외). 기록은 다음 월물도 같이 받는다.
- 장 시간이 바뀌는 날(수능일 등)은 `calendar.json` 의 skip 목록으로 쉰다. 이 파일은 사람이 고친다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
KOSDAQ150 = "06"
KOSPI200 = "01"


@dataclass(frozen=True)
class DayPlan:
    day: date
    trade: bool
    reason: str
    front: str | None          # 오늘 매매할 종목코드 (trade=False 면 참고용)
    record: tuple[str, ...]    # 오늘 기록할 종목코드들


def expiry(inst) -> date:
    return datetime.strptime(inst.exp_ym[:8], "%Y%m%d").date()


def futures_by_expiry(index, product_key: str, session: str = "day") -> list:
    return sorted(index.futures(product_key, session=session), key=expiry)


def load_calendar(path: Path | None = None) -> dict:
    p = path or HERE / "calendar.json"
    if not p.is_file():
        return {"skip": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def plan_day(index, today: date, *, product_key: str = KOSDAQ150, calendar: dict | None = None) -> DayPlan:
    """오늘 매매 여부와 종목. 주말·달력 skip·최종거래일이면 매매하지 않는다."""
    cal = calendar if calendar is not None else load_calendar()
    live = [i for i in futures_by_expiry(index, product_key) if expiry(i) >= today]
    if not live:
        return DayPlan(today, False, "no_contract", None, ())
    front = live[0]
    record = tuple(i.code for i in live[:2])
    if today.weekday() >= 5:
        return DayPlan(today, False, "weekend", front.code, ())
    skip = cal.get("skip", {}).get(today.isoformat())
    if skip:
        return DayPlan(today, False, f"calendar:{skip}", front.code, record)
    if expiry(front) == today:
        nxt = live[1].code if len(live) > 1 else None
        return DayPlan(today, False, "expiry_day", nxt, record)
    return DayPlan(today, True, "ok", front.code, record)
