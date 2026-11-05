"""오류 코드 사전과 분류.

출처: 운영진 예시 앱 `backend/nh_api/error_codes.py` 의 코드·문구 표를 옮기고, 분류 함수는
우리 구조에 맞게 새로 썼습니다.

NH선물이 실패를 알리는 길은 셋입니다.
HTTP 상태, 본문의 `errorCode`(200 으로도 옵니다), 주문 계열의 `msg_cd`·`err_cd` 입니다.

**갈래(kind)는 코드로만 정합니다.** 문구를 글자로 맞춰 보지 않습니다.
표에 없는 코드는 `other` 입니다 — 모르는 코드를 아는 척하지 않습니다.
**서버가 보낸 문구가 언제나 먼저**이고 이 사전의 문구는 예비입니다.

아는 어긋남: `EG045` 는 문구가 둘, `50803` 은 문구가 비어 있음, `5039` 는 사유가 둘
(야간 개장 전 / 시장가 호가유형 제한) — 그래서 `5039` 는 주문오류 조회로 상세를 봅니다.
"""
from __future__ import annotations

from typing import Any

# 갈래
OK = "ok"
REISSUE = "reissue"  # 접속 토큰을 다시 받으면 풀림
RETRY = "retry"  # 잠시 뒤 다시 하면 풀릴 수 있음
SETTINGS = "settings"  # 인증 정보를 고쳐야 함
PERMISSION = "permission"  # 권한·차단·환경 문제 — 이 프로그램에서 못 고침
PAPER_UNAVAILABLE = "paper-unavailable"  # 모의투자에서 제공하지 않는 API
QUOTE_SUBSCRIPTION = "quote-subscription"  # 거래소 시세 사용 신청 필요
OTHER = "other"

ERROR_CODE_FIELD = "errorCode"
ERROR_MESSAGE_FIELDS = ("errorMessage", "errorMsg", "message", "msg")

#: 구독을 더 못 받는다는 코드. 한도는 앱키 하나에 스무 건.
SUBSCRIPTION_LIMIT = "EST06"

#: 같은 코드에 사유가 둘이라 주문오류 조회에서 상세를 봐야 하는 코드.
DETAIL_IN_ORDER_ERRORS = frozenset({"5039"})

#: 정상 처리에도 코드가 실려 오는 자리.
OK_CODES = frozenset({"EGW002", "EGW013", "0"})

#: 게이트웨이·실시간 접속 쪽 코드의 앞자리. 숫자로만 된 코드는 업무 쪽입니다.
SYSTEM_PREFIXES = ("EG", "EST")

SYSTEM_MESSAGES: dict[str, str] = {
    "EG041": "지원하지 않는 JWT 형식 입니다.",
    "EG042": "토큰의 생성 시점이 잘못되었습니다.",
    "EG043": "토큰의 서명이 올바르지 않거나 변조되었습니다.",
    "EG044": "토큰의 필수 클레임이 누락 되었습니다.",
    "EG045": "토큰의 클레임의 값이 일치 하지 않습니다.",
    "EG046": "현재 토큰은 Block 되었습니다.",
    "EG047": "잘못된 JWT 형식입니다.",
    "EG048": "현재 토큰은 Block 되었습니다.",
    "EG049": "현재 토큰은 폐기 되었습니다. 토큰을 다시 발급해주세요.",
    "EGW001": "appKey 또는 secKey가 올바르지 않습니다.",
    "EGW002": "accessToken이 성공적으로 발급되었습니다.",
    "EGW003": "IP 보안 정책 위반: 토큰은 발급된 IP에서만 사용 가능합니다.",
    "EGW005": "필수값이 입력되지 않았습니다.",
    "EGW008": "ping-pong이 수신되지 않아 연결이 종료됩니다.",
    "EGW009": "계좌번호가 잘못되었습니다.",
    "EGW010": "주문할 수 없는 종목입니다 또는 종목코드가 없습니다.",
    "EGW012": "파라미터 값이 허용된 범위를 벗어났습니다.",
    "EGW013": "해당 날짜 범위 내 배치 목록이 조회되었습니다.",
    "EGW014": "호출 권한이 없는 API입니다.",
    "EGW016": "호출 권한이 없는 API입니다.",
    "EGW017": "서버와의 연결이 끊어졌습니다. 다시 시도해주세요.",
    "EGW018": "Access Token이 만료되었습니다. 재인증이 필요합니다.",
    "EGW019": "백엔드 시스템이 응답하지 않습니다.",
    "EGW020": "요청이 허용된 양을 초과하여 차단되었습니다. 일정 시간 후 다시 시도해주세요.",
    "EGW021": "Authorization 헤더가 누락되었습니다.",
    "EGW022": "Authorization 형식이 올바르지 않습니다.",
    "EGW023": "유효하지 않은 토큰입니다.",
    "EGW024": "토큰이 만료되었습니다. 재인증이 필요합니다.",
    "EGW025": "해당 API에 접근 권한이 없습니다.",
    "EGW026": "등록되지 않은 IP에서의 접근이 차단되었습니다.",
    "EGW028": "파라미터 형식이 올바르지 않습니다.",
    "EGW029": "지원하지 않는 HTTP Method입니다.",
    "EGW030": "Content-Type이 올바르지 않습니다. (application/json만 허용)",
    "EGW031": "요청한 API 경로가 존재하지 않습니다.",
    "EGW032": "백엔드 응답 시간이 초과되었습니다.",
    "EGW033": "백엔드 서버와의 연결에 실패했습니다.",
    "EGW034": "게이트웨이 내부 오류가 발생했습니다.",
    "EGW036": "허용된 요청 속도를 초과했습니다. 잠시 후 다시 시도해주세요.",
    "EGW037": "할당량을 초과하여 요청이 제한되었습니다.",
    "EGW038": "차단된 사용자 또는 앱키입니다.",
    "EGW039": "더 이상 지원되지 않는 API입니다.",
    "EGW051": "Authorization 인증 정보를 확인할 수 없습니다.",
    "EGW052": "토큰 재발급이 필요합니다.",
    "EGW053": "해당 API는 모의투자에서 호출할 수 없습니다.",
    "EST01": "토큰을 찾을 수 없습니다. 소켓 접근에 거부되었습니다.",
    "EST02": "이미 소비된 토큰입니다. 소켓 접근에 거부되었습니다.",
    "EST03": "IP 주소가 일치하지 않습니다. 소켓 접근에 거부되었습니다.",
    "EST04": "유효하지 않은 토큰입니다. 소켓 접근에 거부되었습니다.",
    "EST05": "소켓 접근에 거부되었습니다.",
    "EST06": "현재 appkey로 20개 이상의 메시지를 등록할 수 없습니다.",
    "EST07": "예외 발생 허용된 msg_code가 아닙니다.",
    "EST08": "예외 발생 잘못된 action 입니다. A: 구독, D: 해지만 사용 가능합니다.",
}

#: 주문오류 — 자주 만나는 것만 담은 예비 문구. 서버 문구가 먼저입니다.
ORDER_MESSAGES: dict[str, str] = {
    "5016": "종목 및 품목 정보가 없습니다.",
    "5033": "주문할 수 없는 계좌입니다. 계좌 상태를 확인해 주세요.",
    "5039": "거래소 (야간)개장전입니다.",
    "7096": "원주문내역이 존재하지 않습니다.",
    "0": "정상처리 되었습니다.",
    "40": "주문가능금액이 기본예탁금보다 부족함",
    "52": "주문증거금총액 부족",
    "53": "주문증거금현금 부족",
    "901": "주문 수량 오류",
    "976": "최종거래일이 지난 종목 주문 불가",
    "3998": "원주문의 잔량 오류",
    "50013": "주문가격을 확인하십시요.",
    "50015": "매수매도구분을 확인하십시요.",
    "50021": "만기또는 폐쇄된 종목입니다.",
    "50022": "1회주문 한도 수량을 초과. 오류",
    "50029": "Insufficient margin to process your order.",
    "50036": "거래소접수전이거나 정정/취소중 혹은 취소/체결완료주문입니다.",
    "50037": "정정할 원주문 수량이 없습니다.",
    "50409": "가격유형조건이 유효하지 않은 값입니다.",
    "50410": "체결유형조건이 유효하지 않은 값입니다.",
    "50802": "호가단위 오류",
    "50803": "",
    "50808": "(시장가)호가유형별 호가입력제한오류",
    "55001": "주문권한이 없습니다.",
    "90043": "해당종목이 존재하지 않습니다.",
    "90060": "해당종목의 거래일을 확인하십시오.",
    "90207": "거래일이 아닙니다.",
    "97996": "원호가수량을 초과한 취소 주문입니다.",
    "98010": "원호가수량을 초과한 정정 주문입니다.",
}

KINDS: dict[str, str] = {
    **{code: REISSUE for code in (
        "EG041", "EG042", "EG043", "EG044", "EG045", "EG047", "EG049",
        "EGW018", "EGW021", "EGW022", "EGW023", "EGW024", "EGW051", "EGW052",
        "EST01", "EST02", "EST04",
    )},
    **{code: RETRY for code in (
        "EGW008", "EGW017", "EGW019", "EGW020", "EGW032", "EGW033", "EGW034",
        "EGW036", "EGW037",
    )},
    **{code: SETTINGS for code in ("EGW001", "EGW009")},
    **{code: PERMISSION for code in (
        "EG046", "EG048", "EGW003", "EGW014", "EGW016", "EGW025", "EGW026", "EGW038",
        "EST03", "EST05",
    )},
    "EGW053": PAPER_UNAVAILABLE,
    "ETR010": QUOTE_SUBSCRIPTION,
    "EGW002": OK,
    "EGW013": OK,
    "0": OK,
}


def message_of(code: str) -> str:
    """그 코드의 예비 문구. 없으면 빈 글자."""
    body = str(code or "").strip()
    return SYSTEM_MESSAGES.get(body) or ORDER_MESSAGES.get(body) or ""


def kind_of(code: str) -> str:
    """그 코드로 무엇을 해야 하는지. 모르는 코드는 `other`."""
    body = str(code or "").strip()
    if not body:
        return OTHER
    if body in KINDS:
        return KINDS[body]
    if body in OK_CODES:
        return OK
    return OTHER


def is_system_code(code: str) -> bool:
    """게이트웨이 쪽 코드인지. 앞자리로만 가립니다."""
    return str(code or "").strip().upper().startswith(SYSTEM_PREFIXES)


def describe(code: Any, message: Any = "") -> dict[str, str]:
    """코드·문구·갈래. 문구 차례는 서버가 보낸 것 → 예비 문구 → 빈 글자."""
    body = str(code or "").strip()
    said = str(message or "").strip()
    return {"code": body, "message": said or message_of(body), "kind": kind_of(body)}


def failure_of(body: Any) -> dict[str, str] | None:
    """본문의 `errorCode` 가 실패를 담고 있으면 그 모양, 아니면 None. HTTP 상태와 무관합니다."""
    if not isinstance(body, dict):
        return None
    code = str(body.get(ERROR_CODE_FIELD) or "").strip()
    if not code or code in OK_CODES:
        return None
    said = ""
    for field in ERROR_MESSAGE_FIELDS:
        said = str(body.get(field) or "").strip()
        if said:
            break
    return describe(code, said)


def is_rejection_code(code: Any) -> bool:
    """주문 응답의 코드가 거절인지. 비었거나 **전부 0 이면 거절이 아닙니다.**

    해외 주문은 정상 접수에도 `err_cd` 로 `00000` 을 보냅니다.
    """
    body = str(code or "").strip()
    return bool(body) and set(body) != {"0"}


def needs_detail(code: Any) -> bool:
    """주문오류 조회에서 상세를 봐야 하는 코드인지."""
    return str(code or "").strip() in DETAIL_IN_ORDER_ERRORS
