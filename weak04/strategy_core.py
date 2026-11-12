# 출처: 사용자 제공 초안 nh_w04_engine_20261111.zip 의 nh_w04/core.py (2026-11-11). 4주차에 수정: strategy.json 설정,
# 위험 한도(MES 상한·일일 손실), 풋-콜 패리티 잔차, 수량 제한 헤지.
"""설정·호가·다리·헤지 판단·손익 분해 — 순수 로직(입출력 없음). 실시간과 재현이 이 코드를 같이 쓴다.

부호 규칙: 수량은 매수 +, 매도 −. 델타는 ES 선물 1계약 = 1.0, MES 1계약 = 0.1.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

import bs

YEAR_SECONDS = 365.0 * 24 * 3600

#: 시리즈 → 만기 시각(한국시간). ES 주간옵션(금요일)은 15:00 CT = 다음 날 06:00 KST(겨울).
EXPIRY_KST = {
    "O_EW2X26": datetime(2026, 11, 14, 6, 0),
    "O_EW3X26": datetime(2026, 11, 21, 6, 0),
    "O_EW4X26": datetime(2026, 11, 28, 6, 0),
}


@dataclass(frozen=True)
class Q:
    """최우선 호가 한 장."""

    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    ts: datetime | None = None      # 호가 수신 시각(없으면 None)

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def ok(self, max_spread: float | None = None) -> bool:
        if not (self.bid > 0 and self.ask > 0 and self.ask >= self.bid):
            return False
        return max_spread is None or self.spread <= max_spread + 1e-9


@dataclass
class Cfg:
    series: str = "O_EW2X26"
    expiry: datetime = EXPIRY_KST["O_EW2X26"]
    es_sym: str = "ESZ26"
    mes_sym: str = "MESZ26"
    pairs: int = 1                   # 콜 n + 풋 n
    strike_step: float = 5.0
    # 헤지
    band: float = 1.0                # 델타가 MES 몇 계약 어긋나면 맞출지
    min_gap: float = 10.0            # 헤지 주문 사이 최소 간격(초)
    max_hedges_per_hour: int = 12    # 넘으면 band 를 2배로
    # 시세
    poll: float = 2.0                # 루프 주기(초)
    iv_max_spread: float = 2.0       # 옵션 호가폭이 이보다 넓으면 IV 갱신 안 함
    iv_window: int = 5               # 최근 유효 IV 몇 개의 중앙값을 쓸지
    stale_sec: float = 60.0          # ES 호가가 이보다 오래되면 헤지 안 함
    # 진입·체결
    entry_max_spread: float = 2.0
    entry_timeout: float = 120.0
    fill_wait: float = 10.0          # 지정가 체결을 기다리는 시간(초)
    # 위험
    stop_per_pair: float = 400.0     # 쌍당 손절($, 수수료 포함 평가손익)
    stop_hold: float = 30.0          # 이만큼 이어지면 손절
    enter_at: datetime | None = None  # 진입 시각(없으면 실행한 순간). 미장 개장 전에 구축하기 위해 기다리는 시각
    exit_at: datetime | None = None  # 청산 시작 시각
    deadline: datetime | None = None  # 이 시각 전에는 무조건 잔고 0 (당일청산)
    kill_file: str = "state/KILL"          # 프로젝트 공용 킬스위치
    stop_file: str = "STOP"                # 작업 폴더에 `STOP` 파일을 만들면 청산하고 끝낸다
    recon_every: float = 30.0        # 잔고 대사 주기(초)
    log_every: float = 5.0           # 상태 기록 주기(초)
    # 위험 한도 (CLAUDE.md §4-2)
    max_mes_per_pair: int = 12       # 쌍당 MES 보유 절댓값 상한(옵션 델타 최대 ±1.0 → MES ±10, 여유 2)
    daily_loss_limit: float = 1200.0  # 하루 누적 손실 한도($). 남은 한도 < 1회 손절 금액이면 신규 진입 금지
    # 상품
    opt_mult: float = 50.0
    mes_mult: float = 5.0
    mes_per_es: int = 10
    opt_fee: float = 8.0             # 편도 $/계약 (사용자 값 2026-11-10)
    mes_fee: float = 2.0


def opt_sym(series: str, cp: str, strike: float) -> str:
    return f"{series}-{cp}{strike:.1f}"


_SYM_RE = re.compile(r"^(?P<series>O_[A-Z0-9]+)-(?P<cp>[CP])(?P<k>\d+(?:\.\d+)?)$")


def parse_opt_sym(sym: str) -> tuple[str, str, float] | None:
    m = _SYM_RE.match(sym)
    return (m["series"], m["cp"], float(m["k"])) if m else None


def atm_strike(F: float, step: float) -> float:
    return math.floor(F / step + 0.5) * step


def year_frac(now: datetime, expiry: datetime) -> float:
    return max((expiry - now).total_seconds(), 1.0) / YEAR_SECONDS


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


@dataclass
class Leg:
    sym: str
    cp: str
    strike: float
    qty: int = 0
    entry_px: float = 0.0
    iv: float | None = None
    ivs: list[float] = field(default_factory=list)
    last_mid: float | None = None

    def update_iv(self, q: Q, F: float, T: float, cfg: Cfg) -> bool:
        """유효한 호가일 때만 IV 를 갱신한다(최근 iv_window 개 중앙값 → 호가 튐에 둔감)."""
        if not q.ok(cfg.iv_max_spread):
            return False
        self.last_mid = q.mid
        v = bs.implied_vol(q.mid, F, self.strike, T, self.cp)
        if v is None:
            return False
        self.ivs.append(v)
        del self.ivs[:-cfg.iv_window]
        self.iv = median(self.ivs)
        return True

    def greeks(self, F: float, T: float) -> dict[str, float] | None:
        """1계약 기준 그릭스. IV 를 아직 모르면 None."""
        return None if self.iv is None else bs.greeks(F, self.strike, T, self.iv, self.cp)

    def mark(self, q: Q | None, F: float, T: float, cfg: Cfg) -> float | None:
        """평가 가격: 호가가 멀쩡하면 가운데 값, 아니면 모형값(마지막 IV·지금 ES), 그것도 없으면 마지막 가운데 값."""
        if q is not None and q.ok(cfg.iv_max_spread):
            return q.mid
        if self.iv is not None:
            return bs.price(F, self.strike, T, self.iv, self.cp)
        return self.last_mid


def portfolio_greeks(legs: list[Leg], F: float, T: float) -> dict[str, float] | None:
    """보유 옵션 전체의 그릭스(수량 반영). 한 다리라도 IV 를 모르면 None."""
    tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        if leg.qty == 0:
            continue
        g = leg.greeks(F, T)
        if g is None:
            return None
        for k in tot:
            tot[k] += leg.qty * g[k]
    return tot


def hedge_order(opt_delta: float, mes_pos: int, band: float, mes_per_es: int = 10) -> int:
    """낼 MES 수량(매수 +, 매도 −, 0 이면 안 냄).

    어긋남 = 옵션 델타(MES 환산) + MES 보유. |어긋남| 이 band 이상일 때만, 가장 가까운 정수로 한 번에 맞춘다.
    band 안에서는 가만히 있으므로 경계에서 사고팔기를 되풀이하지 않는다.
    """
    need = opt_delta * mes_per_es + mes_pos
    if abs(need) < band - 1e-9:
        return 0
    return -round_half_up(opt_delta * mes_per_es) - mes_pos


def mismatch(opt_delta: float, mes_pos: int, mes_per_es: int = 10) -> float:
    return opt_delta * mes_per_es + mes_pos


@dataclass
class HedgeThrottle:
    """헤지 주문 빈도 제한: 최소 간격, 시간당 상한을 넘으면 band 2배."""

    min_gap: float
    max_per_hour: int
    times: list[datetime] = field(default_factory=list)

    def band(self, base: float, now: datetime) -> float:
        cut = now - timedelta(hours=1)
        self.times = [t for t in self.times if t > cut]
        return base * 2 if len(self.times) >= self.max_per_hour else base

    def allowed(self, now: datetime) -> bool:
        return not self.times or (now - self.times[-1]).total_seconds() >= self.min_gap - 1e-9

    def record(self, now: datetime) -> None:
        self.times.append(now)


@dataclass
class Pnl:
    """현금 흐름 방식 손익. 체결마다 현금을 빼고 더하고, 평가 때 보유분 가치를 더한다."""

    cash: float = 0.0
    fees: float = 0.0

    def fill(self, signed_qty: int, px: float, mult: float, fee: float) -> None:
        self.cash -= signed_qty * px * mult
        self.fees += abs(signed_qty) * fee

    def gross(self, value: float) -> float:
        return self.cash + value

    def net(self, value: float) -> float:
        return self.cash + value - self.fees


@dataclass
class Attribution:
    """손익을 감마·세타·베가·남은 델타로 나눈다(구간 시작 그릭스 × 구간 변화, $ 누적)."""

    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    delta: float = 0.0               # 헤지하고 남은 델타 × ES 이동
    _prev: dict | None = None

    def step(self, F: float, T: float, legs: list[Leg], mes_pos: int, cfg: Cfg) -> None:
        snap = {"F": F, "T": T, "mes": mes_pos,
                "legs": {l.sym: (l.qty, l.greeks(F, T), l.iv) for l in legs if l.qty and l.iv is not None}}
        p = self._prev
        self._prev = snap
        if p is None:
            return
        dF, dT = F - p["F"], p["T"] - T
        net_delta = p["mes"] / cfg.mes_per_es
        for sym, (qty, g, iv0) in p["legs"].items():
            if g is None:
                continue
            self.gamma += 0.5 * qty * g["gamma"] * dF * dF * cfg.opt_mult
            self.theta += qty * g["theta"] * dT * cfg.opt_mult
            now = snap["legs"].get(sym)
            if now is not None and now[2] is not None and iv0 is not None:
                self.vega += qty * g["vega"] * (now[2] - iv0) * cfg.opt_mult
            net_delta += qty * g["delta"]
        self.delta += net_delta * dF * cfg.opt_mult

    def as_dict(self) -> dict[str, float]:
        return {"gamma": round(self.gamma, 2), "theta": round(self.theta, 2), "vega": round(self.vega, 2), "delta": round(self.delta, 2)}


@dataclass
class StopWatch:
    """평가손익이 −threshold 이하인 상태가 hold 초 이어지면 True."""

    threshold: float
    hold: float
    since: datetime | None = None

    def update(self, now: datetime, pnl: float) -> bool:
        if pnl > -self.threshold:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return (now - self.since).total_seconds() >= self.hold - 1e-9


# ---------------------------------------------------------------- 4주차 추가: 위험 한도·헤지 수량 제한·패리티 잔차


def cfg_from_json(conf: dict, *, today: datetime, exit_at: datetime | None = None, deadline: datetime | None = None,
                  enter_at: datetime | None = None) -> Cfg:
    """`strategy.json` 한 벌로 Cfg 를 만든다. 코드에 매직 넘버를 두지 않기 위함.

    구조: strategy(series·es_sym·mes_sym·expiry_kst·pairs) / params(헤지·시세·체결·손절) / product(승수·수수료) / risk(한도·마감).
    """
    st, pa, pr, rk = conf["strategy"], conf["params"], conf["product"], conf["risk"]
    expiry = datetime.strptime(st["expiry_kst"], "%Y-%m-%d %H:%M")
    if deadline is None and rk.get("deadline"):
        h, m = (int(x) for x in rk["deadline"].split(":"))
        deadline = today.replace(hour=h, minute=m, second=0, microsecond=0)
    return Cfg(
        series=st["series"], expiry=expiry, es_sym=st["es_sym"], mes_sym=st["mes_sym"], pairs=int(st["pairs"]),
        strike_step=pa["strike_step"], band=pa["band"], min_gap=pa["min_gap"], max_hedges_per_hour=pa["max_hedges_per_hour"],
        poll=pa["poll"], iv_max_spread=pa["iv_max_spread"], iv_window=pa["iv_window"], stale_sec=pa["stale_sec"],
        entry_max_spread=pa["entry_max_spread"], entry_timeout=pa["entry_timeout"], fill_wait=pa["fill_wait"],
        stop_per_pair=pa["stop_per_pair"], stop_hold=pa["stop_hold"], recon_every=pa["recon_every"], log_every=pa["log_every"],
        max_mes_per_pair=rk["max_mes_per_pair"], daily_loss_limit=rk["daily_loss_limit"],
        opt_mult=pr["opt_mult"], mes_mult=pr["mes_mult"], mes_per_es=pr["mes_per_es"], opt_fee=pr["opt_fee"], mes_fee=pr["mes_fee"],
        enter_at=enter_at, exit_at=exit_at, deadline=deadline,
    )


def entry_allowed(daily_loss_limit: float, realized_today: float, stop_amount: float) -> tuple[bool, float]:
    """신규 진입 허용 여부. 남은 한도(= 한도 + 오늘 실현손익, 손실이면 음수)가 1회 손절 금액보다 작으면 금지.

    반환: (허용 여부, 남은 한도).
    """
    remaining = daily_loss_limit + min(realized_today, 0.0)      # 이익은 한도를 늘리지 않는다(보수적)
    return remaining >= stop_amount - 1e-9, remaining


def clip_hedge(order: int, mes_pos: int, max_abs: int) -> int:
    """헤지 주문 뒤 MES 보유가 ±max_abs 를 넘지 않게 수량을 깎는다(0 이면 주문 없음)."""
    if order == 0:
        return 0
    target = max(-max_abs, min(max_abs, mes_pos + order))
    return target - mes_pos


def parity_residual(call_mid: float, put_mid: float, strike: float, fut_mid: float) -> float:
    """풋-콜 패리티 잔차 = (콜 − 풋 + 행사가) − 선물. 같은 행사가 세 다리의 가격 차이(스프레드)이며 0 근처여야 한다.

    금리 0 가정(bs.py)이므로 이론값은 0. 양수면 콜·풋 합성 선물이 MES 보다 비싸다.
    """
    return (call_mid - put_mid + strike) - fut_mid


@dataclass
class ParityStats:
    """패리티 잔차 표본을 모아 요약한다(기록·분석용, 주문에는 쓰지 않는다)."""

    n: int = 0
    total: float = 0.0
    total_sq: float = 0.0
    lo: float = float("inf")
    hi: float = float("-inf")
    beyond_cost: int = 0            # |잔차| > 반호가폭 합 (거래비용을 넘는 괴리) 횟수

    def add(self, resid: float, half_cost: float) -> None:
        self.n += 1
        self.total += resid
        self.total_sq += resid * resid
        self.lo, self.hi = min(self.lo, resid), max(self.hi, resid)
        if abs(resid) > half_cost:
            self.beyond_cost += 1

    def as_dict(self) -> dict[str, float | int | None]:
        if self.n == 0:
            return {"n": 0, "mean": None, "std": None, "min": None, "max": None, "beyond_cost": 0}
        mean = self.total / self.n
        var = max(self.total_sq / self.n - mean * mean, 0.0)
        return {"n": self.n, "mean": round(mean, 4), "std": round(math.sqrt(var), 4), "min": round(self.lo, 4),
                "max": round(self.hi, 4), "beyond_cost": self.beyond_cost}


def auto_enter_time(conf: dict, today: datetime) -> datetime:
    """`--enter-at auto`: 미장 정규장 개장(strategy.json session.us_cash_open_kst) 몇 분 전(enter_before_open_min)."""
    h, m = (int(x) for x in conf["session"]["us_cash_open_kst"].split(":"))
    opening = today.replace(hour=h, minute=m, second=0, microsecond=0)
    return opening - timedelta(minutes=int(conf["session"]["enter_before_open_min"]))
