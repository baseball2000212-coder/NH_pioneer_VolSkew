"""1분봉 → 5분봉 합성과 데이터 품질 점검.

입력: `collect_chart.py` 가 만든 `minute_<SYM>.csv.gz` (시각은 KST, `ref_dt`+`exch_tm`).
- 체결이 없는 분은 봉이 없다(0봉으로 채우지 않음). 5분 구간에 1분봉이 `min_sub` 개 미만이면 그 5분봉은 버린다.
- 거래량: 분봉 응답의 `cum_trd_qty` 는 이름과 달리 **봉별 거래량**으로 보인다 (일 합계가 실제 일 거래량 규모, 단조증가 아님;
  `cum_trd_amt` 가 오히려 누적). 그래서 합산해서 5분 거래량으로 쓴다. → `_notes/api_issues.md` 후보.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

KST_NAIVE = "Asia/Seoul"


def load_minute(path: str | Path) -> pd.DataFrame:
    """CSV.gz → 오래된 순 1분봉 (index=ts, KST naive). 중복 시각은 제거한다."""
    df = pd.read_csv(path, dtype=str)
    df["ts"] = pd.to_datetime(df["ref_dt"] + df["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S")
    for col in ("open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"):
        df[col] = pd.to_numeric(df[col])
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    return df.rename(columns={"open_pric": "open", "high_pric": "high", "low_pric": "low", "close_pric": "close", "cum_trd_qty": "volume"})[
        ["open", "high", "low", "close", "volume"]]


def minute_from_rows(rows: list[dict[str, str]]) -> pd.DataFrame:
    """`nhapi.history.slim_row` 형식의 줄 목록 → `load_minute` 와 같은 모양의 1분봉 (라이브 엔진이 쓴다)."""
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ref_dt"] + df["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S")
    for col in ("open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"):
        df[col] = pd.to_numeric(df[col])
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    return df.rename(columns={"open_pric": "open", "high_pric": "high", "low_pric": "low", "close_pric": "close", "cum_trd_qty": "volume"})[
        ["open", "high", "low", "close", "volume"]]


def to_bars(minute: pd.DataFrame, minutes: int = 5, min_sub: int = 3) -> pd.DataFrame:
    """1분봉을 `minutes` 분봉으로 합성한다. index = 구간 시작 시각. 열: open high low close volume n_sub."""
    grouper = minute.groupby(minute.index.floor(f"{minutes}min"))
    bars = grouper.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                       volume=("volume", "sum"), n_sub=("close", "size"))
    bars = bars[bars.n_sub >= min_sub]
    bars.index.name = "ts"
    return bars


def quality_report(minute: pd.DataFrame, bars: pd.DataFrame, minutes: int = 5) -> dict:
    """README 의 데이터 점검 표용 요약."""
    gaps = minute.index.to_series().diff().dt.total_seconds().div(60).dropna()
    full = minute.groupby(minute.index.floor(f"{minutes}min")).size()
    return {
        "minute_bars": int(len(minute)),
        "first": str(minute.index.min()), "last": str(minute.index.max()),
        "trading_days": int(minute.index.normalize().nunique()),
        "one_minute_gap_ratio": round(float((gaps == 1).mean()), 4),
        "gaps_2_to_5_min_ratio": round(float(((gaps > 1) & (gaps <= 5)).mean()), 4),
        "gaps_over_5_min": int((gaps > 5).sum()),
        "bars_5m": int(len(bars)),
        "bars_5m_dropped_lt_min_sub": int((full < 3).sum()),
        "volume_field_monotonic_within_day": bool(
            minute.groupby(minute.index.normalize()).volume.apply(lambda s: s.is_monotonic_increasing).mean() > 0.5),
        "daily_volume_median": float(minute.groupby(minute.index.normalize()).volume.sum().median()),
    }
