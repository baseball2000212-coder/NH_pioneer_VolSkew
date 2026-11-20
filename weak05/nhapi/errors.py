"""예외 계층.

**HTTP 200 은 성공이 아닙니다.** 실패는 네 갈래로 가립니다.

    TransportError  응답을 받지 못함. `sent` 가 False 면 보내지 못한 것(연결 실패),
                    None 이면 처리됐는지 알 수 없는 것(읽기 시간 초과 등)입니다.
    HttpError       HTTP 4xx/5xx 응답.
    GatewayError    게이트웨이·실시간 접속 쪽 거절(EG·EGW·EST 코드).
                    요청이 처리됐는지 아닌지 모르는 자리일 수 있습니다.
    BusinessError   업무 처리 거부(`msg_cd`·`err_cd`, 숫자 `errorCode`).

`kind` 는 「그래서 무엇을 해야 하나」입니다(`codes` 참조).
"""
from __future__ import annotations

from typing import Any


class NHApiError(Exception):
    """API 호출 실패의 뿌리."""

    def __init__(self, api_id: str, message: str) -> None:
        super().__init__(message)
        self.api_id = api_id
        self.message = message


class ConfigError(Exception):
    """설정 파일을 읽을 수 없거나 비어 있을 때."""


class AuthError(NHApiError):
    """접속 토큰·웹소켓 키 발급 실패."""


class TransportError(NHApiError):
    """응답을 받지 못했습니다."""

    def __init__(self, api_id: str, message: str, *, sent: bool | None = None) -> None:
        super().__init__(api_id, message)
        #: False: 보내지 못함 / None: 알 수 없음.
        self.sent = sent


class HttpError(NHApiError):
    """HTTP 오류 응답. 본문은 `errorTime/errorMessage/errorStatus/errorPath` 로 옵니다."""

    def __init__(self, api_id: str, status: int, body: Any) -> None:
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("errorMessage") or "").strip()
        message = f"{api_id} 호출이 거부되었습니다. (HTTP {status})"
        if detail:
            message = f"{message} {detail}"
        super().__init__(api_id, message)
        self.status = status
        self.body = body


class _CodedError(NHApiError):
    def __init__(self, api_id: str, code: str, detail: str, body: Any, kind: str, label: str) -> None:
        message = f"{api_id} {label} ({code})"
        if detail:
            message = f"{message} {detail}"
        super().__init__(api_id, message)
        self.code = code
        self.detail = detail
        self.body = body
        self.kind = kind


class GatewayError(_CodedError):
    """게이트웨이·실시간 접속 쪽 거절."""

    def __init__(self, api_id: str, code: str, detail: str, body: Any, kind: str = "") -> None:
        super().__init__(api_id, code, detail, body, kind, "요청이 거절되었습니다.")


class BusinessError(_CodedError):
    """업무 처리가 거부되었습니다. HTTP 상태와 무관합니다."""

    def __init__(self, api_id: str, code: str, detail: str, body: Any, kind: str = "") -> None:
        super().__init__(api_id, code, detail, body, kind, "처리가 거부되었습니다.")


class RiskError(Exception):
    """리스크 규칙이 막은 동작."""


class OrderGuardError(Exception):
    """주문을 내면 안 되는 환경·상태라서 막은 경우."""
