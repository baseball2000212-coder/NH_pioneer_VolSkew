"""거래 API 를 부르는 공통 자리.

모든 REST 호출은 여기를 지납니다. 지키는 것은 다섯 가지입니다.

1. 시장별 접속 토큰을 붙입니다(국내·해외는 APP Key 가 달라 토큰도 다릅니다).
2. 호출 제한 간격을 지킵니다.
3. **HTTP 200 은 성공이 아닙니다.** 본문의 `errorCode`, `msg_cd`, 해외 주문의 `err_cd` 를 봅니다.
4. 응답 원문은 손대지 않고 그대로 돌려줍니다(`ApiResult.body`). 변환은 `parsers` 가 합니다.
5. **조회만 다시 보냅니다.** 주문은 자동 재시도하지 않습니다 — 시간 초과 뒤 중복 접수를 막기 위해서입니다.
   단 401·토큰 만료 코드는 서버가 요청을 처리하기 전에 거절한 것이므로, 토큰을 다시 받아
   **한 번만** 다시 보냅니다(주문 포함).

호출 기록은 `Journal` 이 있으면 `event`(`name="api_call"`)로 남깁니다.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from . import codes
from .auth import TokenManager, TokenStore
from .catalog import COMMON, ApiSpec, http_spec
from .config import PAPER, Config
from .errors import BusinessError, GatewayError, HttpError, NHApiError, TransportError
from .journal import Journal
from .masking import mask_text
from .ratelimit import RateLimiter

UNAUTHORIZED = 401
MESSAGE_CODE = "msg_cd"
MESSAGE_TEXT = "msg_cntn"
OVERSEAS_ORDER_CODE = "err_cd"
OVERSEAS_ORDER_TEXT = "err_msg"

DEFAULT_TIMEOUT = 10.0
#: 조회 재시도 간격(초). 호출 제한(EGW020/036)·일시 오류에서만 씁니다.
QUERY_BACKOFF = (0.5, 1.0, 2.0)


@dataclass(frozen=True)
class ApiResult:
    """호출 하나의 결과. 본문은 받은 그대로입니다."""

    api_id: str
    market: str
    http_status: int
    body: Any
    latency_ms: float
    attempts: int = 1


def business_failure(body: Any) -> tuple[str, str] | None:
    """업무 처리가 거부되었으면 (코드, 설명), 아니면 None.

    국내는 `msg_cd`(비어 있지 않으면 거부), 해외 주문은 `err_cd`(전부 0 이면 정상)입니다.
    조회 0건(`nrec` 0)은 거부가 아닙니다.
    """
    if not isinstance(body, dict):
        return None
    code = str(body.get(MESSAGE_CODE) or "").strip()
    if code:
        return code, str(body.get(MESSAGE_TEXT) or "").strip()
    err = body.get(OVERSEAS_ORDER_CODE)
    if codes.is_rejection_code(err):
        return str(err).strip(), str(body.get(OVERSEAS_ORDER_TEXT) or "").strip()
    return None


def is_success(body: Any) -> bool:
    """본문만 보고 성공인지. 게이트웨이 코드·업무 거부가 없으면 참."""
    return codes.failure_of(body) is None and business_failure(body) is None


class Client:
    """거래 API 호출."""

    def __init__(
        self,
        config: Config,
        *,
        tokens: TokenManager | None = None,
        limiter: RateLimiter | None = None,
        journal: Journal | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
        use_token_cache: bool = True,
    ) -> None:
        self.config = config
        self._http = http or httpx.Client(timeout=timeout)
        store = TokenStore(config.secrets_dir / "tokens.json") if use_token_cache else None
        self.tokens = tokens or TokenManager(config, self._http, store=store)
        self._limiter = limiter or RateLimiter()
        self.journal = journal
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 호출

    def call(self, api_id: str, payload: Mapping[str, Any] | None = None) -> ApiResult:
        """API 를 한 번 부릅니다(토큰 재발급이 필요하면 한 번 더).

        Raises:
            TransportError / HttpError / GatewayError / BusinessError
        """
        spec = http_spec(api_id)
        if spec.market == COMMON:
            raise ValueError(f"{api_id} 는 인증 API 입니다. TokenManager 를 쓰세요.")
        self.config.market(spec.market, PAPER)  # 키가 비었으면 여기서 걸립니다.

        body_to_send = dict(payload or {})
        attempts = 1
        response, body, latency = self._send(spec, body_to_send, attempts)
        self._record(spec, response.status_code, body, latency, attempts)
        trouble = codes.failure_of(body)

        if response.status_code == UNAUTHORIZED or (trouble is not None and trouble["kind"] == codes.REISSUE):
            # 서버가 요청을 처리하기 전에 거절한 경우라 중복 주문이 되지 않습니다. 한 번만 다시 보냅니다.
            self.tokens.forget(spec.market)
            attempts = 2
            response, body, latency = self._send(spec, body_to_send, attempts)
            self._record(spec, response.status_code, body, latency, attempts)
            trouble = codes.failure_of(body)

        if trouble is not None:
            # 상태 코드보다 본문이 앞섭니다. 200 으로 오면서 본문에만 실패가 적히는 자리가 있습니다.
            if codes.is_system_code(trouble["code"]):
                raise GatewayError(api_id, trouble["code"], trouble["message"], body, trouble["kind"])
            raise BusinessError(api_id, trouble["code"], trouble["message"], body, trouble["kind"])
        if response.status_code >= 400:
            raise HttpError(api_id, response.status_code, body)
        failure = business_failure(body)
        if failure is not None:
            code, text = failure
            raise BusinessError(api_id, code, text or codes.message_of(code), body, codes.kind_of(code))
        return ApiResult(api_id, spec.market, response.status_code, body, latency, attempts)

    def query(
        self,
        api_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        retries: int = len(QUERY_BACKOFF),
        backoff: tuple[float, ...] = QUERY_BACKOFF,
    ) -> ApiResult:
        """조회 API 를 부르고, 일시 오류면 간격을 두고 다시 보냅니다. 주문 API 는 쓸 수 없습니다."""
        spec = http_spec(api_id)
        if spec.is_order:
            raise ValueError(f"{api_id} 는 주문 API 입니다. 자동 재시도하지 않으므로 call() 을 쓰세요.")
        last: NHApiError | None = None
        for attempt in range(retries + 1):
            try:
                return self.call(api_id, payload)
            except (GatewayError, BusinessError) as exc:
                if exc.kind != codes.RETRY:
                    raise
                last = exc
            except TransportError as exc:
                last = exc
            except HttpError as exc:
                if exc.status < 500:
                    raise
                last = exc
            if attempt < retries:
                self._sleep(backoff[min(attempt, len(backoff) - 1)])
        assert last is not None
        raise last

    def paginate(
        self,
        api_id: str,
        payload: Mapping[str, Any],
        *,
        key_field: str = "nxt_key",
        rows_field: str = "Occurs1",
        max_pages: int = 50,
    ) -> Iterator[Any]:
        """연속조회. 직전 응답의 `nxt_key` 를 다음 요청에 넣어 이어 받고, 본문을 한 쪽씩 냅니다.

        키가 비었거나, 이전과 같거나, 줄이 없으면 멈춥니다.
        """
        current = dict(payload)
        for _ in range(max_pages):
            result = self.query(api_id, current)
            yield result.body
            body = result.body if isinstance(result.body, dict) else {}
            next_key = str(body.get(key_field) or "").strip()
            rows = body.get(rows_field)
            if not next_key or next_key == str(current.get(key_field) or "") or not rows:
                return
            current[key_field] = next_key

    # ------------------------------------------------------------------ 내부

    def _send(self, spec: ApiSpec, payload: dict[str, Any], attempt: int) -> tuple[httpx.Response, Any, float]:
        self._limiter.wait(PAPER, spec.market, spec.limit)
        try:
            headers = self.tokens.headers(spec.market)
        except NHApiError as exc:
            raise TransportError(spec.api_id, f"접속 토큰을 준비하지 못했습니다: {mask_text(str(exc))}", sent=False) from exc
        url = f"{self.config.base_url}{spec.path}"
        started = self._clock()
        try:
            if spec.method == "GET":
                response = self._http.get(url, headers=headers)
            else:
                response = self._http.request(spec.method, url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            self._record_failure(spec, "connect_error", started, attempt)
            raise TransportError(spec.api_id, f"{spec.name} 연결에 실패했습니다. 요청은 나가지 않았습니다.", sent=False) from exc
        except httpx.TimeoutException as exc:
            self._record_failure(spec, "timeout", started, attempt)
            raise TransportError(
                spec.api_id,
                f"{spec.name} 요청이 시간 안에 끝나지 않았습니다. 처리됐는지 알 수 없으므로 "
                "다시 보내기 전에 주문내역이나 잔고로 확인해 주세요.",
                sent=None,
            ) from exc
        except httpx.HTTPError as exc:
            self._record_failure(spec, type(exc).__name__, started, attempt)
            raise TransportError(spec.api_id, f"{spec.name} 요청에 실패했습니다: {type(exc).__name__}", sent=None) from exc
        latency = (self._clock() - started) * 1000
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text.strip() if response.status_code >= 400 else None
            if body is None:
                raise TransportError(spec.api_id, f"{spec.api_id} 응답을 해석할 수 없습니다.", sent=None)
        return response, body, latency

    def _record(self, spec: ApiSpec, status: int, body: Any, latency: float, attempt: int) -> None:
        if self.journal is None:
            return
        code = ""
        if isinstance(body, dict):
            code = str(body.get(MESSAGE_CODE) or body.get(OVERSEAS_ORDER_CODE) or body.get(codes.ERROR_CODE_FIELD) or "").strip()
            if set(code) == {"0"}:
                code = ""
        self.journal.api_call(spec.api_id, http_status=status, msg_cd=code, latency_ms=latency, attempt=attempt)

    def _record_failure(self, spec: ApiSpec, reason: str, started: float, attempt: int) -> None:
        if self.journal is not None:
            self.journal.api_call(
                spec.api_id, http_status=None, msg_cd=reason,
                latency_ms=(self._clock() - started) * 1000, attempt=attempt,
            )
