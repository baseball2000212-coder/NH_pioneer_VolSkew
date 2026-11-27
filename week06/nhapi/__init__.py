"""NH선물 Open API 직접 호출 클라이언트 (팀 VolSkew).

전략 코드는 이 패키지만 거쳐 API 를 부릅니다. 이 폴더는 주차 제출 때 `submission/nhapi/` 로
**복사**되어 혼자 돌 수 있도록 상대 import 와 `httpx`·`websockets`·`pyyaml` 만 씁니다.

모의(paper) 환경 전용입니다.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .auth import Token, TokenManager, TokenStore
from .client import ApiResult, Client, business_failure, is_success
from .config import Config, fill_status, find_config_path, load_config
from .errors import (
    AuthError,
    BusinessError,
    ConfigError,
    GatewayError,
    HttpError,
    NHApiError,
    OrderGuardError,
    RiskError,
    TransportError,
)
from .journal import Journal, read_journal
from .masking import mask, mask_text
from .orders import OrderGateway, OrderOutcome, OrderRequest
from .ratelimit import RateLimiter
from .risk import Decision, KillSwitch, RiskLimits, RiskManager, shutdown_sequence

__all__ = [
    "ApiResult", "AuthError", "BusinessError", "Client", "Config", "ConfigError", "Decision",
    "GatewayError", "HttpError", "Journal", "KillSwitch", "NHApiError", "OrderGateway",
    "OrderGuardError", "OrderOutcome", "OrderRequest", "RateLimiter", "RiskError", "RiskLimits",
    "RiskManager", "Token", "TokenManager", "TokenStore", "TransportError", "business_failure",
    "fill_status", "find_config_path", "is_success", "load_config", "mask", "mask_text",
    "read_journal", "shutdown_sequence",
]
