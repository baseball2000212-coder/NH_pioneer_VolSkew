# 출처: 사용자 제공 초안 nh_w04_engine_20261111.zip 의 nh_w04/bs.py (2026-11-11). 4주차에 이식·수정했다.
"""Black-76 (선물옵션). 금리 0 으로 둔다 — 만기 며칠짜리라 할인 효과는 호가 반 틱보다 작다.

T 는 연 단위(달력 시간). cp 는 'C' 또는 'P'. 그릭스 단위: delta(선물 1계약 기준), gamma(/pt), theta(/년), vega(/변동성 1.00).
"""
from __future__ import annotations

from math import erf, exp, log, pi, sqrt

MIN_T = 1e-6
MIN_SIGMA, MAX_SIGMA = 0.005, 5.0


def _N(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _phi(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


def _d1(F: float, K: float, T: float, sigma: float) -> float:
    v = sigma * sqrt(max(T, MIN_T))
    return log(F / K) / v + 0.5 * v


def price(F: float, K: float, T: float, sigma: float, cp: str) -> float:
    d1 = _d1(F, K, T, sigma)
    d2 = d1 - sigma * sqrt(max(T, MIN_T))
    call = F * _N(d1) - K * _N(d2)
    return call if cp == "C" else call - (F - K)


def greeks(F: float, K: float, T: float, sigma: float, cp: str) -> dict[str, float]:
    T = max(T, MIN_T)
    d1 = _d1(F, K, T, sigma)
    sq = sqrt(T)
    ph = _phi(d1)
    return {
        "delta": _N(d1) - (0.0 if cp == "C" else 1.0),
        "gamma": ph / (F * sigma * sq),
        "theta": -F * ph * sigma / (2.0 * sq),
        "vega": F * ph * sq,
    }


def implied_vol(px: float, F: float, K: float, T: float, cp: str) -> float | None:
    """가격 → 내재변동성(이분법). 내재가치 이하이거나 범위를 벗어나면 None."""
    if px <= 0 or F <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(F - K, 0.0) if cp == "C" else max(K - F, 0.0)
    if px <= intrinsic + 1e-9:
        return None
    lo, hi = MIN_SIGMA, MAX_SIGMA
    if price(F, K, T, hi, cp) < px or price(F, K, T, lo, cp) > px:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if price(F, K, T, mid, cp) > px:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
