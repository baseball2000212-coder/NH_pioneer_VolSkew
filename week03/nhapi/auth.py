"""접속 토큰 발급·폐기, 웹소켓 접속키 발급.

참고: 운영진 예시 앱 `backend/auth/auth_functions.py` 의 확인된 사실(경로·형식·함정)을 따라 새로 썼습니다.

- 토큰 발급 `POST /auth-service/v1/token` 은 **form 형식**입니다(JSON 은 "필수 파라미터 누락").
- 폐기 `POST /auth-service/v1/revoke` 는 JSON 이고, 폐기할 토큰을 `Authorization` 헤더로,
  **토큰을 받을 때 쓴 키**로 보냅니다.
- APP Key 는 국내·해외가 따로라 토큰도 **시장별**로 받습니다.
- 인증 호출은 초당 3건입니다. 같은 시장의 토큰은 한 번만 받아 재사용하고 만료 1분 전에 갱신합니다.
- 토큰은 발급받은 IP 에서만 쓸 수 있습니다(`EGW003`).

토큰 캐시 파일(`_secrets/tokens.json`)에는 토큰과 **키 지문**(sha256 앞 16자)을 담고 키 자체는 담지 않습니다.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import PAPER, Config, MarketConfig
from .errors import AuthError
from .masking import mask_text
from .timeutil import KST, now_kst

TOKEN_PATH = "/auth-service/v1/token"
REVOKE_PATH = "/auth-service/v1/revoke"
WEBSOCKET_KEY_PATH = "/permission/v1/web-socket-key"
GRANT_TYPE = "client_credentials"

#: 만료 시각 형식이 명시돼 있지 않아 흔한 꼴을 차례로 시도합니다.
EXPIRES_AT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y%m%d%H%M%S",
)
REFRESH_MARGIN = timedelta(minutes=1)
#: 만료 정보를 해석할 수 없을 때의 유효 기간. 짧게 두어 다음 호출에서 다시 받게 합니다.
FALLBACK_LIFETIME = timedelta(minutes=10)
DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True)
class Token:
    """발급받은 접속 토큰. `repr` 에 토큰 글자가 나오지 않습니다."""

    access_token: str
    token_type: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"Token(type={self.token_type!r}, expires_at={self.expires_at.isoformat()})"

    @property
    def authorization(self) -> str:
        return f"{self.token_type} {self.access_token}".strip()

    def is_valid(self, now: datetime, margin: timedelta = REFRESH_MARGIN) -> bool:
        return now + margin < self.expires_at


def fingerprint(app_key: str) -> str:
    """APP Key 의 지문. 키 자체는 어디에도 남기지 않습니다."""
    return hashlib.sha256(str(app_key or "").encode("utf-8")).hexdigest()[:16]


def _parse_expires_at(payload: dict[str, Any], now: datetime) -> datetime:
    raw = str(payload.get("expires_at") or "").strip()
    if raw:
        for fmt in EXPIRES_AT_FORMATS:
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=KST)
            except ValueError:
                continue
    expires_in = str(payload.get("expires_in") or "").strip()
    if expires_in:
        try:
            return now + timedelta(seconds=int(float(expires_in)))
        except ValueError:
            pass
    return now + FALLBACK_LIFETIME


def _post(
    http: httpx.Client, url: str, what: str, *, form: dict[str, str] | None = None,
    json_body: dict[str, str] | None = None, headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        if form is not None:
            response = http.post(url, data=form, headers=headers)
        else:
            response = http.post(url, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        raise AuthError("token", mask_text(f"{what} 요청에 실패했습니다: {type(exc).__name__}")) from exc
    if response.status_code >= 400:
        raise AuthError("token", mask_text(f"{what} 요청이 거부되었습니다. (HTTP {response.status_code}) {response.text.strip()[:300]}"))
    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError("token", f"{what} 응답을 해석할 수 없습니다.") from exc
    if not isinstance(body, dict):
        raise AuthError("token", f"{what} 응답 형식이 올바르지 않습니다.")
    return body


def issue_token(market: MarketConfig, base_url: str, http: httpx.Client, *, now: datetime | None = None) -> Token:
    """접속 토큰을 발급받습니다."""
    now = now or now_kst()
    body = _post(
        http,
        f"{base_url}{TOKEN_PATH}",
        f"{market.label} 접속 토큰 발급",
        form={"grant_type": GRANT_TYPE, "appkey": market.app_key, "appsecret": market.app_secret},
    )
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        code = str(body.get("errorCode") or body.get("msg_cd") or "").strip()
        raise AuthError("token", f"{market.label} 접속 토큰 발급 응답에 access_token 이 없습니다. {code}".strip())
    return Token(access_token, str(body.get("token_type") or "Bearer").strip(), _parse_expires_at(body, now))


def revoke_token(market: MarketConfig, base_url: str, token: Token, http: httpx.Client) -> None:
    """토큰을 폐기합니다. 토큰을 받을 때 쓴 키로 보내야 합니다."""
    _post(
        http,
        f"{base_url}{REVOKE_PATH}",
        f"{market.label} 접속 토큰 폐기",
        json_body={"appkey": market.app_key, "appsecret": market.app_secret},
        headers={"Authorization": token.authorization},
    )


def issue_websocket_key(token: Token, base_url: str, http: httpx.Client) -> str:
    """웹소켓 접속키를 발급받습니다. **1회용**이라 연결할 때마다 새로 받습니다."""
    try:
        response = http.get(f"{base_url}{WEBSOCKET_KEY_PATH}", headers={"Authorization": token.authorization})
    except httpx.HTTPError as exc:
        raise AuthError("web-socket-key", f"웹소켓 접속키 발급 요청에 실패했습니다: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise AuthError("web-socket-key", mask_text(f"웹소켓 접속키 발급이 거부되었습니다. (HTTP {response.status_code}) {response.text.strip()[:300]}"))
    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError("web-socket-key", "웹소켓 접속키 응답을 해석할 수 없습니다.") from exc
    key = str((body or {}).get("access_key") or "").strip() if isinstance(body, dict) else ""
    if not key:
        raise AuthError("web-socket-key", "웹소켓 접속키 응답에 access_key 가 없습니다.")
    return key


class TokenStore:
    """토큰 캐시 파일. 쓰기에 실패해도 이번 실행에는 손해가 없습니다."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def put(self, key: str, token: Token, key_fingerprint: str) -> None:
        data = self.read()
        data[key] = {
            "access_token": token.access_token,
            "token_type": token.token_type,
            "expires_at": token.expires_at.isoformat(),
            "fingerprint": key_fingerprint,
        }
        self._write(data)

    def drop(self, key: str) -> None:
        data = self.read()
        if data.pop(key, None) is not None:
            self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


class TokenManager:
    """시장별로 접속 토큰을 보관하고 필요할 때 다시 발급받습니다."""

    def __init__(
        self,
        config: Config,
        http: httpx.Client | None = None,
        *,
        store: TokenStore | None = None,
        clock: Callable[[], datetime] = now_kst,
    ) -> None:
        self._config = config
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._store = store
        self._clock = clock
        self._tokens: dict[str, Token] = {}
        self._lock = threading.Lock()
        self._slots: dict[str, threading.Lock] = {}

    def _slot(self, market: str) -> threading.Lock:
        with self._lock:
            return self._slots.setdefault(market, threading.Lock())

    def token(self, market: str) -> Token:
        """유효한 접속 토큰. 없거나 만료가 가까우면 다시 발급받습니다. 동시에 불러도 발급은 한 번입니다."""
        with self._lock:
            cached = self._tokens.get(market)
        if cached is not None and cached.is_valid(self._clock()):
            return cached
        with self._slot(market):
            with self._lock:
                cached = self._tokens.get(market)
            if cached is not None and cached.is_valid(self._clock()):
                return cached
            market_config = self._config.market(market, PAPER)
            kept = self._from_store(market, market_config)
            if kept is not None:
                with self._lock:
                    self._tokens[market] = kept
                return kept
            issued = issue_token(market_config, self._config.base_url, self._http, now=self._clock())
            with self._lock:
                self._tokens[market] = issued
            if self._store is not None:
                self._store.put(market, issued, fingerprint(market_config.app_key))
            return issued

    def _from_store(self, market: str, market_config: MarketConfig) -> Token | None:
        if self._store is None:
            return None
        kept = self._store.read().get(market)
        if not isinstance(kept, dict):
            return None
        if kept.get("fingerprint") != fingerprint(market_config.app_key):
            self._store.drop(market)
            return None
        try:
            token = Token(
                str(kept["access_token"]),
                str(kept.get("token_type") or "Bearer"),
                datetime.fromisoformat(str(kept["expires_at"])),
            )
        except (KeyError, ValueError):
            self._store.drop(market)
            return None
        if not token.is_valid(self._clock()):
            self._store.drop(market)
            return None
        return token

    def headers(self, market: str) -> dict[str, str]:
        return {"Authorization": self.token(market).authorization}

    def forget(self, market: str) -> None:
        """그 시장의 토큰을 버립니다. 서버에 폐기를 청하지는 않습니다."""
        with self._slot(market):
            with self._lock:
                self._tokens.pop(market, None)
            if self._store is not None:
                self._store.drop(market)

    def revoke(self, market: str) -> None:
        """토큰을 폐기하고 보관 중인 값을 버립니다."""
        token = self.token(market)
        revoke_token(self._config.market(market, PAPER), self._config.base_url, token, self._http)
        self.forget(market)

    def websocket_key(self, market: str) -> str:
        return issue_websocket_key(self.token(market), self._config.base_url, self._http)
