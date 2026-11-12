"""NH선물 Open API 목록 — 경로, HTTP 방식, 호출 제한 등급, 주문 여부.

출처: 운영진 예시 앱 `backend/nh_api/catalog.py` (각 API 명세의 「기본 정보」 표).
호출 제한은 시세·계좌 초당 10건, 주문 5건, 인증 3건이고 실시간은 동시 구독 20건입니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

DOMESTIC = "domestic"
OVERSEAS = "overseas"
#: 시장을 가리지 않는 인증 API. 어느 시장의 키로 받을지는 부르는 쪽이 정합니다.
COMMON = "common"
MARKETS = (DOMESTIC, OVERSEAS)


class Limit(str, Enum):
    """호출 제한 등급."""

    QUOTE = "quote"
    ACCOUNT = "account"
    ORDER = "order"
    AUTH = "auth"


LIMIT_PER_SECOND: dict[Limit, int] = {
    Limit.QUOTE: 10,
    Limit.ACCOUNT: 10,
    Limit.ORDER: 5,
    Limit.AUTH: 3,
}

#: 실시간은 초당 건수가 아니라 동시에 유지할 수 있는 구독 수로 제한됩니다.
STREAM_SUBSCRIPTION_LIMIT = 20

#: 웹소켓 접속 경로. 발급받은 접속키를 `?access_key=` 로 붙입니다.
STREAM_PATH = "/trade/ws-stream"


@dataclass(frozen=True)
class ApiSpec:
    """HTTP 로 부르는 API 하나."""

    api_id: str
    name: str
    market: str
    method: str
    path: str
    limit: Limit
    #: 주문·정정·취소. 자동 재시도하지 않습니다.
    is_order: bool = False


@dataclass(frozen=True)
class StreamSpec:
    """실시간 API 하나. `action_code` 는 구독 본문에 넣는 값입니다(국내 주문/체결은 `O1`)."""

    api_id: str
    name: str
    market: str
    action_code: str


def _q(api_id: str, name: str, market: str, path_tail: str, limit: Limit = Limit.QUOTE) -> ApiSpec:
    return ApiSpec(api_id, name, market, "POST", f"/trade/v1/{path_tail}", limit)


def _o(api_id: str, name: str, market: str, path_tail: str) -> ApiSpec:
    return ApiSpec(api_id, name, market, "POST", f"/trade/v1/{path_tail}", Limit.ORDER, is_order=True)


_ACC = Limit.ACCOUNT

HTTP_APIS: dict[str, ApiSpec] = {
    spec.api_id: spec
    for spec in (
        ApiSpec("token", "접근토큰 발급", COMMON, "POST", "/auth-service/v1/token", Limit.AUTH),
        ApiSpec("revoke", "접근토큰 폐기", COMMON, "POST", "/auth-service/v1/revoke", Limit.AUTH),
        ApiSpec("web-socket-key", "웹소켓 키 발급", COMMON, "GET", "/permission/v1/web-socket-key", Limit.AUTH),
        _q("domestic_price", "국내_현재가", DOMESTIC, "domestic/price"),
        _q("domestic_price-bid", "국내_호가", DOMESTIC, "domestic/price-bid"),
        _q("domestic_chart-daily", "국내_차트(일)", DOMESTIC, "domestic/chart-daily"),
        _q("domestic_chart-minute", "국내_차트(분)", DOMESTIC, "domestic/chart-minute"),
        _q("domestic_price-all-futures", "국내_선물전체시세", DOMESTIC, "domestic/price-all-futures"),
        _q("domestic_price-all-option", "국내_옵션전체시세", DOMESTIC, "domestic/price-all-option"),
        _q("domestic_trend-investor", "국내_투자자직전매매동향", DOMESTIC, "domestic/trend-investor"),
        _q("domestic_price-expect-fill", "국내_종목예상체결가", DOMESTIC, "domestic/price-expect-fill"),
        _q("domestic_product-detail", "국내_품목별세부정보", DOMESTIC, "domestic/product-detail", _ACC),
        _q("domestic_price-night", "국내_현재가(야간)", DOMESTIC, "domestic/price-night"),
        _q("domestic_price-all-option-night", "국내_옵션전체시세(야간)", DOMESTIC, "domestic/price-all-option-night"),
        _o("domestic_order", "국내_신규주문", DOMESTIC, "domestic/order"),
        _o("domestic_modify", "국내_정정주문", DOMESTIC, "domestic/modify"),
        _o("domestic_cancel", "국내_취소주문", DOMESTIC, "domestic/cancel"),
        _q("domestic_orderable-quantity", "국내_주문가능수량", DOMESTIC, "domestic/orderable-quantity", _ACC),
        _q("domestic_open-interest", "국내_잔고내역", DOMESTIC, "domestic/open-interest", _ACC),
        _q("domestic_order-error", "국내_주문오류내역", DOMESTIC, "domestic/order-error", _ACC),
        _q("domestic_order-history", "국내_주문내역", DOMESTIC, "domestic/order-history", _ACC),
        _q("domestic_deposit", "국내_예수금조회", DOMESTIC, "domestic/deposit", _ACC),
        _o("domestic_order-night", "국내_신규주문(야간)", DOMESTIC, "domestic/order-night"),
        _o("domestic_modify-night", "국내_정정주문(야간)", DOMESTIC, "domestic/modify-night"),
        _o("domestic_cancel-night", "국내_취소주문(야간)", DOMESTIC, "domestic/cancel-night"),
        _q("overseas_price", "해외_현재가", OVERSEAS, "overseas/price"),
        _q("overseas_bid-price", "해외_호가", OVERSEAS, "overseas/bid-price"),
        _q("overseas_chart-daily", "해외_차트(일)", OVERSEAS, "overseas/chart-daily"),
        _q("overseas_chart-minute", "해외_차트(분)", OVERSEAS, "overseas/chart-minute"),
        _q("overseas_price-all-option", "해외_옵션전체시세", OVERSEAS, "overseas/price-all-option"),
        _q("overseas_product-detail", "해외_품목별세부정보", OVERSEAS, "overseas/product-detail", _ACC),
        _q("overseas_market-operations", "해외_장운영정보", OVERSEAS, "overseas/market-operations", _ACC),
        _o("overseas_order", "해외_주문", OVERSEAS, "overseas/order"),
        _q("overseas_orderable-quantity", "해외_주문가능수량", OVERSEAS, "overseas/orderable-quantity", _ACC),
        _q("overseas_order-error", "해외_주문오류내역", OVERSEAS, "overseas/order-error", _ACC),
        _q("overseas_order-history", "해외_주문내역", OVERSEAS, "overseas/order-history", _ACC),
        _q("overseas_open-interest", "해외_잔고내역", OVERSEAS, "overseas/open-interest", _ACC),
        _q("overseas_deposit", "해외_예수금조회", OVERSEAS, "overseas/deposit", _ACC),
        _q("overseas_product-expiry", "해외_만기일조회", OVERSEAS, "overseas/product-expiry", _ACC),
        _q("overseas_open-interest-expiry", "해외_보유포지션만기조회", OVERSEAS, "overseas/open-interest-expiry", _ACC),
        _q("overseas_cumulative-pl", "해외_누적손익현황", OVERSEAS, "overseas/cumulative-pl", _ACC),
    )
}

STREAM_APIS: dict[str, StreamSpec] = {
    spec.api_id: spec
    for spec in (
        StreamSpec("KA", "국내_시세체결", DOMESTIC, "KA"),
        StreamSpec("KB", "국내_시세호가", DOMESTIC, "KB"),
        StreamSpec("KC", "국내_시세체결(야간)", DOMESTIC, "KC"),
        StreamSpec("KD", "국내_시세호가(야간)", DOMESTIC, "KD"),
        StreamSpec("O1_K", "국내_주문/체결", DOMESTIC, "O1"),
        StreamSpec("FA", "해외_시세체결", OVERSEAS, "FA"),
        StreamSpec("FB", "해외_시세호가", OVERSEAS, "FB"),
        StreamSpec("O1", "해외_주문/체결", OVERSEAS, "O1"),
    )
}


def http_spec(api_id: str) -> ApiSpec:
    """HTTP API 하나의 명세. 모르는 이름이면 ValueError."""
    try:
        return HTTP_APIS[api_id]
    except KeyError:
        raise ValueError(f"모르는 API 입니다: {api_id!r}") from None


def stream_spec(api_id: str) -> StreamSpec:
    """실시간 API 하나의 명세. 모르는 이름이면 ValueError."""
    try:
        return STREAM_APIS[api_id]
    except KeyError:
        raise ValueError(f"모르는 실시간 API 입니다: {api_id!r}") from None
