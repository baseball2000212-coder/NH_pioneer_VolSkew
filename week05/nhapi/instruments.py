"""종목 마스터 내려받기·파싱, 근월물 찾기, 틱 처리.

참고: 운영진 예시 앱 `backend/master/*`, `backend/domestic_futureoption/master/__init__.py`.

- 주소: `https://apidev.futures.co.kr/guide/master/{이름}.mst.gz` (정적 파일, 하루 한 번 바뀝니다).
- 형식: UTF-8 텍스트, 한 줄이 한 종목, 필드는 `;` 로 나뉩니다. 값 앞뒤 공백은 뗍니다.
  한 줄이라도 필드 수가 어긋나면 그 파일은 쓰지 않습니다(배치가 통째로 밀렸을 수 있음).
- 국내 마스터 세 파일(`fut_opt`·`idx_fut_opt`·`stk_fut_opt`)은 컬럼 배치가 같습니다.
  **시장 구분은 파일 이름이 아니라 행의 `mrkt_tp` 로 합니다**(파일 이름과 내용이 어긋납니다).
- 틱 크기는 마스터에 없습니다. `product-detail` 의 값(`ProductRow.tick`)을 씁니다.
- 해외 마스터 컬럼 배치는 3주차에 확인해 추가합니다.

**주의:** 이 코드는 마스터 파일을 실제로 내려받아 확인하기 전에 작성했습니다.
`exp_ym` 의 정확한 형식(앞 6자리가 YYYYMM 이라고 가정)은 첫 내려받기에서 확인합니다.
"""
from __future__ import annotations

import gzip
import os
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

import httpx

from .errors import NHApiError
from .timeutil import now_kst, yyyymmdd

MASTER_URL_BASE = "https://apidev.futures.co.kr/guide/master"
DOMESTIC_MASTER_FILES = ("fut_opt.mst", "idx_fut_opt.mst", "stk_fut_opt.mst")
ENCODING = "utf-8"
DELIMITER = ";"

#: 국내 선물/옵션 마스터의 컬럼 26개. 파일 안 순서 그대로, NH선물 헤더정보 문서의 표기 그대로입니다.
DOMESTIC_FIELDS: tuple[str, ...] = (
    "mrkt_tp",       # 마켓구분 (4.지수선물옵션 5.개별주식선물옵션 6.상품선물옵션)
    "fo_tp",         # 선물/옵션/스프레드 구분 (F:선물 O:옵션 C:스프레드)
    "opt_tp",        # 옵션구분 (1:선물 2:스프레드 3:콜옵션 4:풋옵션)
    "under_name",    # 기초자산명
    "exp_ym",        # 만기년월
    "cme_tp",        # 주야간구분 (day, night)
    "class",         # 분류
    "kospi_key",     # 거래소코드
    "series",        # 종목코드
    "rank_serie",    # 상위종목 (KTB에서만 사용)
    "name_kor",      # 한글종목명
    "name_eng",      # 영문종목명
    "upper_p_lv1",   # 상한가 (1단계)
    "lower_p_lv1",   # 하한가 (1단계)
    "strike_p",      # 행사가격
    "inst_grp_cd",   # 파생상품코드
    "p_dec",         # 소수점정보
    "ctrt_size",     # 계약크기, 거래승수
    "rem_cnt",       # 잔존일수
    "dynmc_yn",      # 실시간상한가적용여부
    "dynmc_up_p",    # 실시간상한가
    "dynmc_dn_p",    # 실시간하한가
    "real_Code",     # 실시간종목코드 (실시간 요청 시 사용하는 코드)
    "kofex_Code",    # 당사코드
    "mrkt_code",     # 시장코드
    "trd_code",      # 거래대상코드 (품목 열쇠)
)

FUTURES = "futures"
OPTION = "option"
SPREAD = "spread"
_KIND_OF_FO = {"F": FUTURES, "O": OPTION, "C": SPREAD}


class MasterError(NHApiError):
    """마스터를 받지 못했거나 읽지 못했습니다."""

    def __init__(self, message: str) -> None:
        super().__init__("master", message)


@dataclass(frozen=True)
class Instrument:
    """종목 하나 (국내)."""

    code: str            # series — 주문·조회에 쓰는 종목코드 (예 A016C000)
    name: str
    kind: str            # futures / option / spread
    product_key: str     # trd_code — 품목 열쇠
    exp_ym: str          # 만기년월 원문
    session: str         # cme_tp: day / night
    multiplier: Decimal | None
    real_code: str       # 실시간 구독에 쓰는 종목코드
    strike: str
    market_type: str     # mrkt_tp

    @property
    def expiry_month(self) -> str:
        """만기년월 앞 6자리(YYYYMM). 형식은 첫 내려받기에서 확인합니다."""
        return self.exp_ym[:6]


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.strip())
    except Exception:  # noqa: BLE001 — 숫자가 아니면 모르는 값으로 둡니다.
        return None


def parse_master_text(text: str, fields: tuple[str, ...] = DOMESTIC_FIELDS, *, name: str = "") -> list[dict[str, str]]:
    """마스터 파일 내용 → 행 사전 목록. 필드 수가 어긋난 줄이 있으면 MasterError."""
    rows: list[dict[str, str]] = []
    # splitlines() 는 유니코드 줄바꿈에서도 나누므로 개행 문자만 기준으로 합니다.
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        values = [value.strip() for value in line.split(DELIMITER)]
        if len(values) != len(fields):
            raise MasterError(
                f"{name or '마스터 파일'} {number}번째 줄의 항목 수가 맞지 않습니다 "
                f"(기대 {len(fields)}개, 실제 {len(values)}개)"
            )
        rows.append(dict(zip(fields, values)))
    return rows


def to_instrument(row: dict[str, str]) -> Instrument:
    return Instrument(
        code=row["series"],
        name=row["name_kor"],
        kind=_KIND_OF_FO.get(row["fo_tp"], row["fo_tp"]),
        product_key=row["trd_code"],
        exp_ym=row["exp_ym"],
        session=row["cme_tp"],
        multiplier=_decimal(row["ctrt_size"]),
        real_code=row["real_Code"],
        strike=row["strike_p"],
        market_type=row["mrkt_tp"],
    )


class InstrumentIndex:
    """종목 목록. 코드로 찾거나 품목의 근월물을 고릅니다."""

    def __init__(self, instruments: list[Instrument]) -> None:
        self.instruments = instruments
        self._by_code = {item.code: item for item in instruments}

    def __len__(self) -> int:
        return len(self.instruments)

    def by_code(self, code: str) -> Instrument | None:
        return self._by_code.get(code)

    def futures(self, product_key: str, *, session: str | None = None) -> list[Instrument]:
        """품목의 선물 종목을 만기 순으로."""
        found = [
            item for item in self.instruments
            if item.kind == FUTURES and item.product_key == product_key
            and (session is None or item.session == session)
        ]
        return sorted(found, key=lambda item: item.exp_ym)

    def front_month(
        self,
        product_key: str,
        *,
        today: date | None = None,
        session: str | None = None,
        nearest_expiry: date | None = None,
    ) -> Instrument | None:
        """아직 만기가 지나지 않은 가장 가까운 월물.

        `nearest_expiry` 는 `product-detail` 의 최근월물 만기일입니다. 주면 그 날짜가 오늘 이전인
        월물(= 만기 당일이 지난 것)은 건너뜁니다. 마스터에는 만기 지난 종목이 남아 있을 수 있습니다.
        """
        today = today or now_kst().date()
        this_month = f"{today.year:04d}{today.month:02d}"
        for item in self.futures(product_key, session=session):
            if item.expiry_month < this_month:
                continue
            if item.expiry_month == this_month and nearest_expiry is not None and nearest_expiry < today:
                continue
            return item
        return None


def load_instruments(directory: Path) -> InstrumentIndex:
    """폴더 안의 국내 마스터 파일을 모두 읽어 종목 목록을 만듭니다."""
    instruments: list[Instrument] = []
    for file_name in DOMESTIC_MASTER_FILES:
        path = Path(directory) / file_name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding=ENCODING)
        except UnicodeDecodeError as exc:
            raise MasterError(f"{file_name} 을 {ENCODING} 로 읽지 못했습니다.") from exc
        instruments.extend(to_instrument(row) for row in parse_master_text(text, name=file_name))
    return InstrumentIndex(instruments)


class MasterStore:
    """마스터 파일을 날짜 폴더에 보관합니다. 기본 위치는 `./.nhapi_cache/master` 입니다."""

    ENV_VAR = "NHAPI_CACHE_DIR"

    def __init__(self, cache_dir: Path | str | None = None) -> None:
        base = cache_dir or os.environ.get(self.ENV_VAR) or ".nhapi_cache"
        self.root = Path(base) / "master"

    def folder(self, stamp: str | None = None) -> Path:
        return self.root / (stamp or yyyymmdd())

    def has_all(self, stamp: str | None = None) -> bool:
        folder = self.folder(stamp)
        return all((folder / name).is_file() for name in DOMESTIC_MASTER_FILES)

    def download(
        self, *, force: bool = False, stamp: str | None = None, http: httpx.Client | None = None
    ) -> list[str]:
        """국내 마스터 세 개를 순차로 받아 풀어 저장합니다. 받은 파일 이름을 돌려줍니다.

        오늘 폴더에 이미 다 있으면 받지 않습니다. 하나라도 실패하면 MasterError 입니다.
        """
        if not force and self.has_all(stamp):
            return []
        folder = self.folder(stamp)
        folder.mkdir(parents=True, exist_ok=True)
        owned = http is None
        http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True)
        saved: list[str] = []
        try:
            for name in DOMESTIC_MASTER_FILES:
                try:
                    response = http.get(f"{MASTER_URL_BASE}/{name}.gz")
                    response.raise_for_status()
                    content = gzip.decompress(response.content)
                except (httpx.HTTPError, OSError, EOFError) as exc:
                    raise MasterError(f"{name} 을 받지 못했습니다: {type(exc).__name__}") from exc
                tmp = folder / f"{name}.tmp"
                tmp.write_bytes(content)
                os.replace(tmp, folder / name)
                saved.append(name)
        finally:
            if owned:
                http.close()
        return saved

    def load(self, stamp: str | None = None) -> InstrumentIndex:
        return load_instruments(self.folder(stamp))


def round_to_tick(
    price: Decimal | str | float, tick: Decimal | str,
    mode: Literal["nearest", "floor", "ceil"] = "nearest",
) -> Decimal:
    """가격을 틱 단위로 맞춥니다. 주문 가격은 틱의 배수여야 합니다."""
    price_d = Decimal(str(price))
    tick_d = Decimal(str(tick))
    if tick_d <= 0:
        raise ValueError("틱 크기는 0 보다 커야 합니다.")
    rounding = {"nearest": ROUND_HALF_UP, "floor": ROUND_FLOOR, "ceil": ROUND_CEILING}[mode]
    return (price_d / tick_d).to_integral_value(rounding=rounding) * tick_d


def is_on_tick(price: Decimal | str, tick: Decimal | str) -> bool:
    """가격이 틱의 배수인지."""
    return Decimal(str(price)) % Decimal(str(tick)) == 0
