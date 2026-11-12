"""4주차에 더한 것의 시험: 설정 로딩, 위험 한도(MES 상한·일일 손실), 패리티 잔차, 체결 지연, 기록 형식, 백테스트 결과 JSON.

NH 호출 없음. 데이터가 필요한 시험은 submission/data 의 11/10 기록을 쓴다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import bs
from brokers import SimBroker
from engine import Engine
from journal_adapter import EngineJournal, realized_today
from nhapi.journal import Journal, read_journal
from strategy_core import (Cfg, ParityStats, Q, cfg_from_json, clip_hedge, entry_allowed, parity_residual, parse_opt_sym,
                           year_frac)

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"
T0 = datetime(2026, 11, 12, 22, 0, 0)
EXP = datetime(2026, 11, 14, 6, 0)


class Market:
    def __init__(self, es_path, iv=0.19, spread=0.5, mes_offset=0.0):
        self.now, self.es_path, self.iv, self.spread, self.mes_offset = T0, es_path, iv, spread, mes_offset

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)

    def quote(self, sym):
        F = self.es_path((self.now - T0).total_seconds())
        p = parse_opt_sym(sym)
        if p is None:
            off = self.mes_offset if sym.startswith("MES") else 0.0
            return Q(F + off - 0.125, F + off + 0.125, 10, 10, ts=self.now)
        mid = bs.price(F, p[2], year_frac(self.now, EXP), self.iv, p[1])
        return Q(mid - self.spread / 2, mid + self.spread / 2, 5, 5, ts=self.now)


def make(es_path, *, mes_offset=0.0, **kw):
    m = Market(es_path, mes_offset=mes_offset)
    b = SimBroker(m.quote)
    cfg = Cfg(exit_at=T0 + timedelta(minutes=10), deadline=T0 + timedelta(minutes=60), kill_file="__none__", stop_file="__none_stop__", **kw)
    logs = []
    e = Engine(b, cfg, clock=m.clock, sleep=m.sleep, log=logs.append)
    return m, b, e, logs


def test_strategy_json_이_Cfg_로_읽힌다():
    conf = json.loads((HERE / "strategy.json").read_text(encoding="utf-8"))
    cfg = cfg_from_json(conf, today=datetime(2026, 11, 13), exit_at=None)
    assert cfg.series == "O_EW2X26" and cfg.mes_sym == "MESZ26" and cfg.pairs == 1
    assert cfg.expiry == datetime(2026, 11, 14, 6, 0) and cfg.deadline == datetime(2026, 11, 13, 23, 45)
    assert (cfg.opt_fee, cfg.mes_fee, cfg.opt_mult, cfg.mes_mult) == (8.0, 2.0, 50.0, 5.0)
    assert cfg.stop_per_pair == 400.0 and cfg.max_mes_per_pair == 12 and cfg.daily_loss_limit == 1200.0


def test_일일_손실_한도_남은_한도가_손절보다_작으면_금지():
    assert entry_allowed(1200, 0.0, 400) == (True, 1200)
    assert entry_allowed(1200, -700.0, 400) == (True, 500.0)
    ok, remaining = entry_allowed(1200, -801.0, 400)
    assert not ok and remaining == pytest.approx(399.0)
    assert entry_allowed(1200, +900.0, 400)[1] == 1200          # 이익은 한도를 늘리지 않는다


def test_MES_보유_상한():
    assert clip_hedge(5, 0, 12) == 5
    assert clip_hedge(5, 10, 12) == 2                           # 12 까지만
    assert clip_hedge(-5, -10, 12) == -2
    assert clip_hedge(3, 12, 12) == 0                           # 이미 상한이면 주문 없음
    assert clip_hedge(0, 4, 12) == 0
    assert clip_hedge(-3, 12, 12) == -3                         # 줄이는 방향은 막지 않는다


def test_패리티_잔차():
    F, K, T, s = 7600.0, 7600.0, 3 / 365, 0.17
    c, p = bs.price(F, K, T, s, "C"), bs.price(F, K, T, s, "P")
    assert parity_residual(c, p, K, F) == pytest.approx(0.0, abs=1e-9)        # 금리 0 → 이론 잔차 0
    assert parity_residual(c + 1.0, p, K, F) == pytest.approx(1.0)
    st = ParityStats()
    for r in (0.0, 1.0, -2.0, 0.5):
        st.add(r, half_cost=0.75)
    d = st.as_dict()
    assert d["n"] == 4 and d["min"] == -2.0 and d["max"] == 1.0 and d["beyond_cost"] == 2     # |1.0|, |−2.0| > 0.75
    assert ParityStats().as_dict()["n"] == 0


def test_엔진이_패리티_잔차를_기록하고_주문에는_쓰지_않는다():
    m, b, e, logs = make(lambda t: 7580.0, mes_offset=0.0)
    e.run()
    states = [r for r in logs if r["kind"] == "state" and r.get("parity")]
    assert states and abs(states[0]["parity"]["resid"]) < 1.0
    s = [r for r in logs if r["kind"] == "summary"][0]
    assert s["parity"]["n"] > 10
    # MES 가 ES 보다 3pt 비싸게 호가되면 잔차는 −3 근처 — 주문(헤지)은 그 영향을 받지 않는다
    m2, b2, e2, logs2 = make(lambda t: 7580.0, mes_offset=3.0)
    e2.run()
    assert [r for r in logs2 if r["kind"] == "summary"][0]["parity"]["mean"] == pytest.approx(-3.0, abs=0.6)
    assert [t["qty"] for t in b.trades if t["sym"] == "MESZ26"] == [t["qty"] for t in b2.trades if t["sym"] == "MESZ26"]


def test_일일_손실_한도에_걸리면_진입하지_않는다():
    m, b, e, logs = make(lambda t: 7580.0)
    e.realized_today = -900.0                                    # 남은 한도 300 < 손절 400
    e.run()
    assert b.trades == [] and "손실 한도" in e.exit_reason


def test_헤지는_MES_상한을_넘지_않는다():
    m, b, e, logs = make(lambda t: 7580.0 - min(t, 120) * 1.0, max_mes_per_pair=2)      # 2분에 120pt 하락 → 델타가 크게 기울어도
    e.cfg.exit_at = T0 + timedelta(minutes=8)
    e.run()
    held = [r["mes"] for r in logs if r["kind"] == "state"]
    assert held and max(abs(x) for x in held) <= 2
    assert any(r.get("what") == "mes_cap" for r in logs if r["kind"] == "warn")
    assert b.positions() == {}


def test_백테스트_체결_지연은_뒤_호가로_체결한다():
    path = lambda t: 7580.0 + t * 0.5                           # 1초에 0.5pt 상승
    m = Market(path)
    plain = SimBroker(m.quote)
    late = SimBroker(m.quote, lambda s: _shifted(m, s, 10.0))
    plain.place("MESZ26", "buy", 1)
    late.place("MESZ26", "buy", 1)
    assert plain.trades[-1]["px"] < late.trades[-1]["px"]       # 10초 뒤 호가(더 비쌈)로 체결 → 불리해짐
    assert late.trades[-1]["px"] - plain.trades[-1]["px"] == pytest.approx(5.0)


def _shifted(m, sym, ahead):
    save = m.now
    m.now = save + timedelta(seconds=ahead)
    try:
        return m.quote(sym)
    finally:
        m.now = save


def test_기록_형식_모든_줄에_공통_칸과_체결_출처(tmp_path):
    j = Journal(tmp_path / "x.jsonl", "r1", env="paper", mode="paper-sim", sym="O_EW2X26")
    log = EngineJournal(j, series="O_EW2X26", mes_sym="MESZ26", console=False)
    m, b, e, _ = make(lambda t: 7580.0 - min(t, 300) * 0.2)
    e._log_fn = log
    e.run()
    rows = list(read_journal(tmp_path / "x.jsonl"))
    assert rows and all({"run_id", "ts", "env", "mode", "sym", "kind"} <= set(r) for r in rows)
    kinds = {r["kind"] for r in rows}
    assert {"run", "event", "signal", "order", "fill"} <= kinds <= {"run", "event", "signal", "order", "fill", "trade", "error", "api_issue"}
    assert {r["source"] for r in rows if r["kind"] == "fill"} == {"paper-sim"}      # 종이 체결을 모의계좌 체결로 쓰지 않는다
    assert any(r["kind"] == "signal" and r["reason"] == "delta_mismatch" and r["sym"] == "MESZ26" for r in rows)
    assert log.summary and log.summary["flat"] is True


def test_옵션_매도_거부_7093_은_api_issue_후보로_남긴다(tmp_path):
    j = Journal(tmp_path / "y.jsonl", "r2", env="paper", mode="paper-order", sym="O_EW2X26")
    log = EngineJournal(j, series="O_EW2X26", mes_sym="MESZ26", console=False)
    log({"ts": "2026-11-12T22:00:00", "kind": "order", "state": "EXIT", "why": "exit", "sym": "O_EW2X26-C7580.0", "side": "sell",
         "qty": 1, "price": None, "status": "rejected", "order_no": "", "code": "7093", "msg": "옵션신규매도 거래가 불가능합니다."})
    rows = list(read_journal(tmp_path / "y.jsonl"))
    assert [r["kind"] for r in rows] == ["order", "api_issue"] and rows[1]["issue_id"] == "CAND-7093"
    assert log.source == "mock-account"


def test_기록에_키나_접속IP가_남지_않는다(tmp_path):
    j = Journal(tmp_path / "z.jsonl", "r3", mode="paper-order")
    log = EngineJournal(j, series="O_EW2X26", mes_sym="MESZ26", console=False)
    log({"ts": "2026-11-12T22:00:00", "kind": "warn", "state": "HEDGE", "what": "x", "app" + "key": "FAKE-VALUE-0001", "occurs1_ip_addr": "203.0.113.9"})
    text = (tmp_path / "z.jsonl").read_text(encoding="utf-8")
    assert "FAKE-VALUE-0001" not in text and "203.0.113.9" not in text


def test_오늘_실현손익_합계는_같은_mode_이고_다른_실행만(tmp_path):
    for run, mode, pnl in (("a", "paper-order", -300.0), ("b", "paper-order", -150.0), ("c", "paper-sim", -999.0), ("d", "paper-order", 50.0)):
        j = Journal(tmp_path / f"{run}.jsonl", run, mode=mode, clock=lambda: datetime(2026, 11, 12, 23, 0).astimezone())
        j.run("summary", pnl_net=pnl)
    day = datetime(2026, 11, 12, 23, 0).astimezone().date().isoformat()
    assert realized_today(tmp_path, day, "paper-order") == pytest.approx(-400.0)
    assert realized_today(tmp_path, day, "paper-order", exclude_run="d") == pytest.approx(-450.0)
    assert realized_today(tmp_path, "2000-01-01", "paper-order") == 0.0


@pytest.mark.skipif(not (DATA / "20261110_ew2_chain.csv").exists(), reason="11/10 기록 없음")
def test_run_backtest_결과_JSON_에_가정과_입력_해시가_들어간다(tmp_path):
    import run
    rc = run.main(["--env", "paper", "--mode", "backtest", "--run-id", "t-bt", "--day", "20261110", "--start", "22:29:00",
                   "--exit", "23:00:00", "--log-dir", str(tmp_path / "logs"), "--out-dir", str(tmp_path / "res"), "--latency", "5"])
    assert rc == 0
    res = json.loads((tmp_path / "res" / "t-bt_result.json").read_text(encoding="utf-8"))
    assert res["label"] == "[백테스트]" and res["assumptions"]["latency_sec"] == 5.0
    assert set(res["inputs_sha256"]) == {"20261110_ew2_chain.csv", "20261110_ew2_fut.csv"}
    assert res["summary"]["flat"] is True
    assert all(f["sym"] for f in res["fills"])
    rows = list(read_journal(tmp_path / "logs" / "t-bt.jsonl"))
    assert {r["mode"] for r in rows} == {"backtest"} and {r["source"] for r in rows if r["kind"] == "fill"} == {"backtest-model"}
    assert rows[0]["ts"].startswith("2026-11-10T22:29")                       # 시뮬레이션 시각으로 기록


def test_run_은_실전_환경을_받지_않는다():
    import run
    with pytest.raises(SystemExit):
        run.parse_args(["--env", "real", "--mode", "backtest", "--run-id", "x"])


def test_진입_시각까지_포지션_없이_기다렸다가_진입한다():
    m, b, e, logs = make(lambda t: 7580.0)
    e.cfg.enter_at = T0 + timedelta(minutes=5)
    e.cfg.exit_at = T0 + timedelta(minutes=15)
    e.run()
    entry = [r for r in logs if r["kind"] == "entry"][0]
    assert datetime.fromisoformat(entry["ts"]) >= e.cfg.enter_at           # 시각 전에는 주문 없음
    assert min(datetime.fromisoformat(r["ts"]) for r in logs if r["kind"] == "order") >= e.cfg.enter_at
    assert any(r["kind"] == "wait_entry" for r in logs) and any(r["kind"] == "waiting" for r in logs)
    assert b.positions() == {} and e.exit_reason == "time"


def test_기다리는_중_STOP_이면_주문_없이_끝난다(tmp_path):
    m, b, e, logs = make(lambda t: 7580.0)
    e.cfg.enter_at = T0 + timedelta(minutes=10)
    e.cfg.exit_at = T0 + timedelta(minutes=20)
    e.cfg.stop_file = str(tmp_path / "STOP")
    orig = m.sleep

    def sleep(s):
        orig(s)
        if m.now >= T0 + timedelta(minutes=2):
            (tmp_path / "STOP").write_text("")
    e.sleep = sleep
    e.run()
    assert b.trades == [] and "중지 신호" in e.exit_reason


def test_진입_시각이_청산_시각_뒤면_거부한다():
    m, b, e, logs = make(lambda t: 7580.0)
    e.cfg.enter_at = T0 + timedelta(minutes=9, seconds=45)
    e.cfg.exit_at = T0 + timedelta(minutes=10)
    e.run()
    assert b.trades == [] and "진입 시각" in e.exit_reason


def test_enter_at_auto_는_개장_20분_전():
    from strategy_core import auto_enter_time
    conf = json.loads((HERE / "strategy.json").read_text(encoding="utf-8"))
    assert auto_enter_time(conf, datetime(2026, 11, 12)) == datetime(2026, 11, 12, 23, 10)
