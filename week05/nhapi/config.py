"""설정 파일 로딩 (모의 전용).

설정 파일은 작업 루트의 `_secrets/config.yaml` 입니다. 이 프로젝트는 **모의(paper) 환경만** 씁니다.
파일에 `real` 칸이 있어도 읽지 않습니다.

찾는 차례:
    1. 환경변수 `NHFUTURES_CONFIG`
    2. 현재 폴더에서 위로 올라가며 처음 만나는 `_secrets/config.yaml`
    3. 이 패키지 위치에서 위로 올라가며 처음 만나는 `_secrets/config.yaml`

APP Key·Secret 은 국내와 해외가 따로 발급되므로 시장별로 보관합니다.
키 값은 `repr` 이나 예외 메시지에 나오지 않습니다. 비었으면 어느 칸이 빈지만 알려 줍니다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

from .errors import ConfigError

CONFIG_ENV_VAR = "NHFUTURES_CONFIG"
SECRETS_DIR_NAME = "_secrets"
CONFIG_FILE_NAME = "config.yaml"

PAPER = "paper"
MARKETS = ("domestic", "overseas")
DEFAULT_BASE_URL = "https://apidemo.futures.co.kr"

MARKET_LABELS = {"domestic": "국내", "overseas": "해외"}


@dataclass(frozen=True)
class MarketConfig:
    """한 시장의 인증 정보."""

    market: str
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)

    @property
    def label(self) -> str:
        return f"모의 {MARKET_LABELS.get(self.market, self.market)}"

    @property
    def is_configured(self) -> bool:
        return bool(self.app_key and self.app_secret)


@dataclass(frozen=True)
class Config:
    """전체 설정. 환경은 paper 하나뿐입니다."""

    base_url: str
    domestic: MarketConfig
    overseas: MarketConfig
    path: Path
    default_environment: str = PAPER

    @property
    def secrets_dir(self) -> Path:
        """설정 파일이 있는 폴더. 토큰 캐시도 여기에 둡니다."""
        return self.path.parent

    def market(self, market: str, environment: str = PAPER) -> MarketConfig:
        """시장별 인증 정보. 비어 있으면 ConfigError."""
        if environment != PAPER:
            raise ConfigError(f"이 프로젝트는 모의(paper) 전용입니다: {environment!r}")
        if market not in MARKETS:
            raise ValueError(f"알 수 없는 시장입니다: {market!r} ({' 또는 '.join(MARKETS)})")
        found: MarketConfig = getattr(self, market)
        if not found.is_configured:
            raise ConfigError(
                f"{found.label} 인증 정보가 비어 있습니다. "
                f"{self.path} 의 paper > {market} 의 app_key 와 app_secret 을 채워 주세요."
            )
        return found


def find_config_path(start: Path | None = None) -> Path | None:
    """설정 파일을 찾습니다. 없으면 None."""
    override = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if override:
        return Path(override)
    bases = [Path(start) if start else Path.cwd(), Path(__file__).resolve().parent]
    for base in bases:
        for folder in (base, *base.parents):
            candidate = folder / SECRETS_DIR_NAME / CONFIG_FILE_NAME
            if candidate.is_file():
                return candidate
    return None


def _read_yaml(path: Path) -> Mapping:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파일을 해석할 수 없습니다: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {path}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"설정 파일의 내용이 올바르지 않습니다: {path}")
    return raw


def _market(section: Mapping, name: str) -> MarketConfig:
    raw = section.get(name)
    if not isinstance(raw, dict):
        raw = {}
    return MarketConfig(
        market=name,
        app_key=str(raw.get("app_key") or "").strip(),
        app_secret=str(raw.get("app_secret") or "").strip(),
    )


def load_config(path: Path | None = None) -> Config:
    """설정 파일을 읽습니다.

    Raises:
        ConfigError: 파일이 없거나 형식이 틀렸거나, 두 시장 모두 인증 정보가 비어 있는 경우.
    """
    path = path or find_config_path()
    if path is None or not Path(path).is_file():
        raise ConfigError(
            f"설정 파일을 찾을 수 없습니다. {SECRETS_DIR_NAME}/{CONFIG_FILE_NAME} 를 만들거나 "
            f"환경변수 {CONFIG_ENV_VAR} 로 경로를 알려 주세요."
        )
    path = Path(path)
    raw = _read_yaml(path)

    default_env = str(raw.get("default_environment") or PAPER).strip()
    if default_env != PAPER:
        raise ConfigError(f"default_environment 는 paper 만 쓸 수 있습니다: {path}")

    section = raw.get(PAPER)
    if not isinstance(section, dict):
        section = {}
    base_url = str(section.get("base_url") or "").strip() or DEFAULT_BASE_URL

    config = Config(
        base_url=base_url.rstrip("/"),
        domestic=_market(section, "domestic"),
        overseas=_market(section, "overseas"),
        path=path,
    )
    if not (config.domestic.is_configured or config.overseas.is_configured):
        raise ConfigError(
            f"사용할 수 있는 인증 정보가 없습니다. {path} 의 paper 아래 "
            "domestic 또는 overseas 의 app_key 와 app_secret 을 채워 주세요."
        )
    return config


def fill_status(path: Path | None = None) -> dict[tuple[str, str, str], bool]:
    """(환경, 시장, 칸) → 채워졌는지. 값은 담지 않습니다."""
    path = path or find_config_path()
    raw: Mapping = _read_yaml(Path(path)) if path is not None and Path(path).is_file() else {}
    section = raw.get(PAPER)
    if not isinstance(section, dict):
        section = {}
    status: dict[tuple[str, str, str], bool] = {}
    for name in MARKETS:
        market = _market(section, name)
        status[(PAPER, name, "app_key")] = bool(market.app_key)
        status[(PAPER, name, "app_secret")] = bool(market.app_secret)
    return status
