"""(출처: 사용자 제공 초안 nh_w04_engine_20261111.zip 의 tests/test_nh_w04.py, 4주차에 import 만 바꿔 이식)
ES 주간옵션 스트래들 + MES 델타헤지 엔진 시험 (NH 호출 없음, 가상 시장·가상 체결)."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

import bs
from brokers import SimBroker
from strategy_core import Cfg, HedgeThrottle, Q, StopWatch, atm_strike, hedge_order, opt_sym, parse_opt_sym, year_frac
from engine import Engine

T0 = datetime(2026, 11, 12, 22, 0, 0)
EXP = datetime(2026, 11, 14, 6, 0)


class FakeMarket:
    """ES 경로를 함수로 주면 옵션 호가를 Black-76(IV 고정)으로 만들어 준다. 시계는 sleep 한 만큼 간다."""

    def __init__(self, es_path, iv=0.19, opt_spread=0.5):
        self.now, self.es_path, self.iv, self.opt_spread = T0, es_path, iv, opt_spread
        self.bad_opt = False

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)

    def F(self):
        return self.es_path((self.now - T0).total_seconds())

    def quote(self, sym):
        F = self.F()
        p = parse_opt_sym(sym)
        if p is None:
            return Q(F - 0.125, F + 0.125, 10, 10, ts=self.now)
        if self.bad_opt:
            return Q(0.0, 0.0, ts=self.now)
        mid = bs.price(F, p[2], year_frac(self.now, EXP), self.iv, p[1])
        return Q(mid - self.opt_spread / 2, mid + self.opt_spread / 2, 5, 5, ts=self.now)


def make(es_path, **cfg_kw):
    m = FakeMarket(es_path)
    b = SimBroker(m.quote)
    kill = os.path.join(tempfile.gettempdir(), "nh_w04_test_KILL_absent")
    cfg = Cfg(series="O_EW2X26", expiry=EXP, exit_at=T0 + timedelta(minutes=30), deadline=T0 + timedelta(minutes=60),
              kill_file=kill, stop_file=kill + "_stop", **cfg_kw)       # 실제 state/ 의 신호 파일과 섞이지 않게
    logs = []
    e = Engine(b, cfg, clock=m.clock, sleep=m.sleep, log=logs.append)
    return m, b, e, logs


def kinds(logs, kind):
    return [r for r in logs if r["kind"] == kind]


class TestBlack76(unittest.TestCase):
    def test_parity_and_iv_roundtrip(self):
        F, K, T, s = 7580.0, 7565.0, 3.3 / 365, 0.198
        c, p = bs.price(F, K, T, s, "C"), bs.price(F, K, T, s, "P")
        self.assertAlmostEqual(c - p, F - K, places=9)
        self.assertAlmostEqual(bs.implied_vol(c, F, K, T, "C"), s, places=6)
        self.assertAlmostEqual(bs.implied_vol(p, F, K, T, "P"), s, places=6)
        gc, gp = bs.greeks(F, K, T, s, "C"), bs.greeks(F, K, T, s, "P")
        self.assertAlmostEqual(gc["delta"] - gp["delta"], 1.0, places=9)
        self.assertAlmostEqual(gc["gamma"], gp["gamma"], places=12)
        self.assertLess(gc["theta"], 0)

    def test_greeks_match_recorded_snapshot(self):
        # 11/10 22:17 기록: ES 7566.875, K7565, 만기까지 3.32일, IV 19.82% → 델타 0.509, 감마 0.00279, 세타 −$17.9/시간, 베가 $144
        g = bs.greeks(7566.875, 7565.0, 3.32 / 365, 0.1982, "C")
        self.assertAlmostEqual(g["delta"], 0.509, delta=0.003)
        self.assertAlmostEqual(g["gamma"], 0.00279, delta=0.00003)
        self.assertAlmostEqual(g["theta"] / 8760 * 50, -17.9, delta=0.3)
        self.assertAlmostEqual(g["vega"] / 100 * 50, 144, delta=2)

    def test_iv_none_below_intrinsic(self):
        self.assertIsNone(bs.implied_vol(10.0, 7600.0, 7565.0, 0.01, "C"))


class TestCore(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(opt_sym("O_EW2X26", "C", 7580.0), "O_EW2X26-C7580.0")
        self.assertEqual(parse_opt_sym("O_EW2X26-P7565.0"), ("O_EW2X26", "P", 7565.0))
        self.assertIsNone(parse_opt_sym("MESZ26"))
        self.assertEqual(atm_strike(7577.5, 5.0), 7580.0)
        self.assertEqual(atm_strike(7567.4, 5.0), 7565.0)

    def test_hedge_order_band(self):
        self.assertEqual(hedge_order(-0.09, 0, 1.0), 0)          # 0.9계약 어긋남 → 가만히
        self.assertEqual(hedge_order(-0.10, 0, 1.0), 1)          # 1계약 어긋남 → 1계약 매수
        self.assertEqual(hedge_order(-0.251, 0, 1.0), 3)         # 한 번에 가장 가까운 정수로
        self.assertEqual(hedge_order(-0.251, 3, 1.0), 0)
        self.assertEqual(hedge_order(-0.16, 3, 1.0), -1)         # 되돌아오면 판다
        self.assertEqual(hedge_order(0.50, 0, 1.0), -5)          # 콜만 있으면 MES 5 매도
        self.assertEqual(hedge_order(-0.19, 0, 2.0), 0)          # band 2
        self.assertEqual(hedge_order(-0.20, 0, 2.0), 2)

    def test_no_flip_flop_inside_band(self):
        pos, trades = 0, 0
        for d in (-0.14, -0.151, -0.149, -0.151, -0.149, -0.16, -0.14):   # 반올림 경계(1.5계약) 근처를 왔다 갔다
            n = hedge_order(d, pos, 1.0)
            pos += n
            trades += abs(n) > 0
        self.assertEqual(trades, 1)

    def test_throttle(self):
        t = HedgeThrottle(10.0, 3)
        self.assertTrue(t.allowed(T0))
        t.record(T0)
        self.assertFalse(t.allowed(T0 + timedelta(seconds=9)))
        self.assertTrue(t.allowed(T0 + timedelta(seconds=10)))
        t.record(T0 + timedelta(seconds=10))
        t.record(T0 + timedelta(seconds=20))
        self.assertEqual(t.band(1.0, T0 + timedelta(seconds=30)), 2.0)   # 시간당 상한 → band 2배
        self.assertEqual(t.band(1.0, T0 + timedelta(hours=2)), 1.0)

    def test_stopwatch(self):
        s = StopWatch(400.0, 30.0)
        self.assertFalse(s.update(T0, -500))
        self.assertFalse(s.update(T0 + timedelta(seconds=20), -500))
        self.assertFalse(s.update(T0 + timedelta(seconds=25), -100))    # 회복하면 다시 센다
        self.assertFalse(s.update(T0 + timedelta(seconds=30), -500))
        self.assertTrue(s.update(T0 + timedelta(seconds=60), -500))


class TestEngine(unittest.TestCase):
    def test_enter_hedge_exit_flat(self):
        m, b, e, logs = make(lambda t: 7580.0 - min(t, 600) * 0.1)      # 10분에 걸쳐 60pt 하락
        e.run()
        self.assertEqual(b.positions(), {})
        entry = [t for t in b.trades if t["sym"].startswith("O_") and t["side"] == "buy"]
        self.assertEqual(sorted(t["sym"] for t in entry), ["O_EW2X26-C7580.0", "O_EW2X26-P7580.0"])
        mes_buys = sum(t["qty"] for t in b.trades if t["sym"] == "MESZ26" and t["side"] == "buy")
        self.assertGreaterEqual(mes_buys, 3)                              # 스트래들이 매도 쪽으로 기울어 MES 를 삼
        self.assertEqual(e.exit_reason, "time")
        s = kinds(logs, "summary")[0]
        self.assertTrue(s["flat"])
        self.assertAlmostEqual(s["fees"], 4 * 8.0 + 2.0 * s["mes_traded"])
        self.assertGreater(s["attr"]["gamma"], 0)
        self.assertLess(s["attr"]["theta"], 0)
        gaps = [(datetime.fromisoformat(b_["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds()
                for a, b_ in zip(kinds(logs, "hedge"), kinds(logs, "hedge")[1:])]
        self.assertTrue(all(g >= 10 for g in gaps), gaps)                 # 헤지 주문 사이 최소 10초

    def test_hedge_keeps_delta_within_band(self):
        m, b, e, logs = make(lambda t: 7580.0 + 30 * __import__("math").sin(t / 120))
        e.run()
        for r in kinds(logs, "state"):
            if r["mismatch"] is not None:
                self.assertLess(abs(r["mismatch"]), 2.5)                  # band 1 + 한 주기 이동 여유

    def test_put_rejected_becomes_call_plus_mes(self):
        m, b, e, logs = make(lambda t: 7580.0)
        b.reject["O_EW2X26-P"] = "9999"
        e.cfg.exit_at = T0 + timedelta(minutes=2)
        e.run()
        sells = [t for t in b.trades if t["sym"] == "MESZ26" and t["side"] == "sell"]
        self.assertEqual(sum(t["qty"] for t in sells[:1]), 5)             # 콜 델타 0.5 → MES 5 매도
        self.assertEqual(b.positions(), {})
        self.assertTrue(any(r.get("what") == "put_failed" for r in kinds(logs, "warn")))

    def test_call_rejected_no_position(self):
        m, b, e, logs = make(lambda t: 7580.0)
        b.reject["O_EW2X26-C"] = "9999"
        e.run()
        self.assertEqual(b.trades, [])
        self.assertEqual(b.positions(), {})
        self.assertIn("콜", e.exit_reason)

    def test_entry_waits_for_valid_quotes_then_gives_up(self):
        m, b, e, logs = make(lambda t: 7580.0, entry_timeout=20.0)
        m.bad_opt = True
        e.run()
        self.assertEqual(b.trades, [])
        self.assertIn("진입 실패", e.exit_reason)

    def test_unknown_but_executed_no_duplicate(self):
        m, b, e, logs = make(lambda t: 7580.0)
        b.unknown_next = 1                                                # 콜 주문 응답 없음(실제로는 체결)
        e.cfg.exit_at = T0 + timedelta(minutes=1)
        e.run()
        call_buys = [t for t in b.trades if t["sym"] == "O_EW2X26-C7580.0" and t["side"] == "buy"]
        self.assertEqual(sum(t["qty"] for t in call_buys), 1)
        self.assertEqual(b.positions(), {})

    def test_unknown_not_executed_is_not_resent_blindly(self):
        m, b, e, logs = make(lambda t: 7580.0)
        b.unknown_next, b.unknown_executes = 1, False                     # 콜 주문이 서버에 안 닿음
        e.run()
        self.assertEqual(b.trades, [])                                    # 재주문하지 않고 진입 포기
        self.assertIn("콜", e.exit_reason)

    def test_limit_unfilled_cancel_then_market(self):
        m, b, e, logs = make(lambda t: 7580.0)
        b.limit_fills = False
        e.cfg.exit_at = T0 + timedelta(minutes=2)
        e.run()
        self.assertEqual(b.positions(), {})
        self.assertEqual(sum(t["qty"] for t in b.trades if t["side"] == "buy" and t["sym"].startswith("O_")), 2)
        self.assertTrue(any(r.get("why") == "cancel_unfilled" for r in kinds(logs, "order")))
        self.assertTrue(any(str(r.get("why", "")).endswith("/market") for r in kinds(logs, "order")))

    def test_partial_fill_remainder_goes_market(self):
        m, b, e, logs = make(lambda t: 7580.0, pairs=2)
        b.max_fill["O_EW2X26-C7580.0"] = 1                               # 주문당 1계약만 체결
        e.cfg.exit_at = T0 + timedelta(minutes=1)
        e.run()
        bought = sum(t["qty"] for t in b.trades if t["sym"] == "O_EW2X26-C7580.0" and t["side"] == "buy")
        self.assertEqual(bought, 2)
        self.assertEqual(b.positions(), {})

    def test_stop_loss_exits(self):
        m, b, e, logs = make(lambda t: 7580.0, stop_per_pair=5.0, stop_hold=10.0)   # 호가폭·수수료만으로도 −$5 아래
        e.run()
        self.assertEqual(e.exit_reason, "stop")
        self.assertEqual(b.positions(), {})
        self.assertLess(datetime.fromisoformat(kinds(logs, "summary")[0]["ts"]), T0 + timedelta(minutes=5))

    def test_never_sells_more_options_than_held(self):
        m, b, e, logs = make(lambda t: 7580.0 + t * 0.05)
        e.run()
        self.assertFalse([r for r in kinds(logs, "order") if r.get("code") == "7093"])

    def test_refuses_when_positions_exist_without_resume(self):
        m, b, e, logs = make(lambda t: 7580.0)
        b.pos["MESZ26"] = 2
        e.run()
        self.assertEqual(b.trades, [])
        self.assertIn("진입 거부", e.exit_reason)

    def test_resume_adopts_and_hedges(self):
        m, b, e, logs = make(lambda t: 7560.0)
        b.pos.update({"O_EW2X26-C7580.0": 1, "O_EW2X26-P7580.0": 1})
        e.resume = True
        e.cfg.exit_at = T0 + timedelta(minutes=1)
        e.run()
        self.assertTrue(kinds(logs, "resume"))
        self.assertTrue(any(t["sym"] == "MESZ26" and t["side"] == "buy" for t in b.trades))   # ES 가 행사가 아래 → MES 매수
        self.assertEqual(b.positions(), {})

    def test_other_symbols_untouched(self):
        m, b, e, logs = make(lambda t: 7580.0)
        b.pos["MGCZ26"] = 1                                               # 다른 전략의 포지션
        e.cfg.exit_at = T0 + timedelta(minutes=1)
        e.run()
        self.assertEqual(b.positions(), {"MGCZ26": 1})

    def test_kill_file_exits(self):
        m, b, e, logs = make(lambda t: 7580.0)
        with tempfile.TemporaryDirectory() as d:
            e.cfg.kill_file = os.path.join(d, "KILL")
            orig = m.sleep

            def sleep(s):
                orig(s)
                if m.now >= T0 + timedelta(seconds=30) and not os.path.exists(e.cfg.kill_file):
                    open(e.cfg.kill_file, "w").close()
            e.sleep = sleep
            e.run()
        self.assertEqual(e.exit_reason, "kill")
        self.assertEqual(b.positions(), {})

    def test_refuses_entry_when_exit_time_already_passed(self):
        m, b, e, logs = make(lambda t: 7580.0)
        e.cfg.exit_at = T0 - timedelta(minutes=1)                         # 예: 자정 넘어 --exit 23:40 을 줌
        e.run()
        self.assertEqual(b.trades, [])
        self.assertIn("진입 거부", e.exit_reason)

    def test_refuses_entry_near_deadline(self):
        m, b, e, logs = make(lambda t: 7580.0)
        e.cfg.exit_at, e.cfg.deadline = None, T0 + timedelta(minutes=2)
        e.run()
        self.assertEqual(b.trades, [])
        self.assertIn("마감", e.exit_reason)

    def test_hedge_rejections_back_off(self):
        m, b, e, logs = make(lambda t: 7580.0 - min(t, 300) * 0.2)       # 5분에 60pt 하락 → 헤지가 계속 필요
        b.reject["MESZ26"] = "9999"                                       # MES 주문이 전부 거부됨
        e.run()
        tries = [r for r in kinds(logs, "order") if r["sym"] == "MESZ26" and r.get("why") == "hedge"]
        self.assertLess(len(tries), 12)                                   # 30분 동안 10초마다(180번) 두드리지 않는다
        self.assertTrue(any(r.get("what") == "hedge_failing" for r in kinds(logs, "warn")))
        self.assertEqual(b.positions(), {})                               # 옵션은 청산됨

    def test_exit_retries_when_position_query_fails(self):
        m, b, e, logs = make(lambda t: 7580.0 - min(t, 600) * 0.1)
        real, n = b.positions, {"i": 0}

        def flaky():
            if m.now >= e.cfg.exit_at:                                    # 청산 중에는 두 번에 한 번 조회 실패
                n["i"] += 1
                if n["i"] % 2:
                    raise RuntimeError("조회 실패")
            return real()
        b.positions = flaky
        e.run()
        self.assertEqual(real(), {})                                      # 그래도 MES 까지 다 정리
        self.assertTrue(kinds(logs, "summary")[0]["flat"])

    def test_stale_stop_file_is_removed_and_kill_switch_blocks_entry(self):
        with tempfile.TemporaryDirectory() as d:
            m, b, e, logs = make(lambda t: 7580.0)
            e.cfg.stop_file = os.path.join(d, "STOP")
            open(e.cfg.stop_file, "w").close()                            # 지난번 중지 신호가 남아 있음
            e.cfg.exit_at = T0 + timedelta(minutes=2)
            e.run()
            self.assertEqual(e.exit_reason, "time")                       # 바로 청산되지 않고 정상 진행
            self.assertFalse(os.path.exists(e.cfg.stop_file))
            m, b, e, logs = make(lambda t: 7580.0)
            e.cfg.kill_file = os.path.join(d, "KILL")
            open(e.cfg.kill_file, "w").close()                            # 공용 킬스위치 켜짐
            e.run()
            self.assertEqual(b.trades, [])
            self.assertIn("킬스위치", e.exit_reason)

    def test_deadline_forces_market_exit(self):
        m, b, e, logs = make(lambda t: 7580.0)
        e.cfg.exit_at = None
        e.cfg.deadline = T0 + timedelta(minutes=5)
        e.run()
        self.assertEqual(e.exit_reason, "deadline")
        self.assertEqual(b.positions(), {})
        self.assertLessEqual(datetime.fromisoformat(kinds(logs, "summary")[0]["ts"]), T0 + timedelta(minutes=5))


DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


@unittest.skipUnless(os.path.exists(os.path.join(DATA, "20261110_ew2_chain.csv")), "11/10 기록 없음")
class TestReplay1110(unittest.TestCase):
    def test_cpi_night(self):
        from replay import ReplayMarket
        day = datetime(2026, 11, 10)
        mkt = ReplayMarket(DATA, "20261110", day.replace(hour=22, minute=29))
        b = SimBroker(mkt.quote)
        cfg = Cfg(exit_at=day.replace(hour=23, minute=40), kill_file="__none__", stop_file="__none_stop__")
        logs = []
        Engine(b, cfg, clock=mkt.clock, sleep=mkt.sleep, log=logs.append).run()
        s = kinds(logs, "summary")[0]
        self.assertTrue(s["flat"])
        self.assertEqual(kinds(logs, "entry")[0]["strike"], 7580.0)
        self.assertTrue(1 <= s["hedges"] <= 6)
        self.assertTrue(-250 < s["pnl_net"] < 50, s["pnl_net"])          # 사후 재현(ew2_replay)은 −98, 지연·IV 평활 차이 허용
        self.assertAlmostEqual(s["attr"]["gamma"], 264, delta=40)        # 사후 재현의 감마 +264, 세타 −37
        self.assertAlmostEqual(s["attr"]["theta"], -37, delta=6)


if __name__ == "__main__":
    unittest.main()
