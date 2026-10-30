"""인증정보·계좌 식별정보를 가립니다.

로그, 예외 메시지, 저장하는 응답 원문은 모두 여기를 지납니다. 두 갈래입니다.

- `mask(obj)`: 사전·목록 안에서 **키 이름**이 민감한 값을 통째로 가립니다.
- `mask_text(text)`: 글자 안의 `Bearer …`, JWT, `access_key=…` 같은 **값 모양**을 가립니다.

가린 자리는 `***` 입니다. 길이나 앞뒤 글자는 남기지 않습니다.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "***"

#: 키 이름에서 영숫자만 남겨 소문자로 만든 뒤 이 조각을 **포함**하면 민감한 값입니다.
SENSITIVE_FRAGMENTS = (
    "authorization",
    "appkey",
    "appsecret",
    "accesstoken",
    "accesskey",
    "sessiontoken",
    "refreshtoken",
    "secret",
    "password",
    "passwd",
    "acno",  # ac_no, occurs1_ac_no
    "acntno",
    "accountno",
    "acnm",  # 계좌명
    "useripaddr",
)

_NON_ALNUM = re.compile(r"[^a-z0-9]")

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 " + MASK),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*"), MASK),
    (
        re.compile(
            r"(?i)\b(access_key|access_token|appkey|appsecret|app_key|app_secret|session_token)"
            r"([\"']?\s*[=:]\s*)([\"']?)[^\s\"'&,}]+"
        ),
        r"\1\2\3" + MASK,
    ),
)


def is_sensitive_key(key: Any) -> bool:
    """이 키 이름의 값을 가려야 하는지."""
    flat = _NON_ALNUM.sub("", str(key).lower())
    return any(fragment in flat for fragment in SENSITIVE_FRAGMENTS)


def mask_text(text: str) -> str:
    """글자 안의 인증정보 모양을 가립니다."""
    for pattern, repl in _TEXT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def mask(obj: Any) -> Any:
    """사전·목록·글자를 깊이 훑으며 가린 **사본**을 돌려줍니다. 원본은 바꾸지 않습니다."""
    if isinstance(obj, dict):
        return {
            key: (MASK if is_sensitive_key(key) and value not in (None, "") else mask(value))
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [mask(item) for item in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj
