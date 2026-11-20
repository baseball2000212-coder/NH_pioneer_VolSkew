# 출처: 4주차 submission/order_smoke.py (MES 부분만 유지, 옵션 단계 제거, 5주차 엔진의 진입 방식 점검 추가)
"""모의계좌 주문 스모크 — MES 1계약. 실전(real) 환경은 지원하지 않는다.

순서
 1. 시작 대사(보유·미체결이 있으면 중단), 예수금·주문가능수량 조회
 2. 시세에서 멀리 떨어진 지정가 매수 → 접수·미체결 확인 → 정정 → 취소 → 미체결 없음 확인
 3. 엔진 진입 방식 점검: 최우선 매수호가에 지정가(같은 쪽 호가) → 제한시간 대기 → 안 체결되면 취소, 체결되면 기록 후 시장가 청산
 4. 시장가 매수 1계약 → 주문내역으로 체결 확인 → 잔고 → 시장가 매도(reduce_only) → 체결 확인 → 잔고 0
 5. 잔고·미체결·예수금 재조회
모든 응답(마스킹 후)과 단계가 JSONL(mode=smoke)로 남는다. 실패·응답 불명이면 그 단계를 멈추고 마지막에 미체결 취소·청산을 한다.

  python order_smoke.py --run-id smoke-w05 --log-dir ../logs --yes
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from nhapi import Client, Journal, OverseasGateway, find_config_path, load_config   # noqa: E402
from nhapi.overseas import OverseasOrderRequest                                      # noqa: E402

TICK = Decimal("0.25")


def snap(price: Decimal) -> Decimal:
    return (price // TICK) * TICK


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--log-dir", default=str(HERE / "logs"))
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cfg = json.loads((HERE / "strategy.json").read_text(encoding="utf-8"))
    sym = cfg["trade"]["symbol"]
    timeout = cfg["risk"]["entry_timeout_sec"]
    journal = Journal(Path(a.log_dir) / f"{a.run_id}.jsonl", a.run_id, env="paper", mode="smoke", sym=sym)
    keys = load_config(find_config_path())
    if keys.default_environment != "paper":
        raise SystemExit("모의(paper) 키가 아닙니다. 중단합니다.")
    client = Client(keys, journal=journal)
    gw = OverseasGateway(client, journal=journal)

    def step(name: str, **data) -> None:
        print(f"[{name}]", data, flush=True)
        journal.event("step", name, sym, **data)

    def raw(api: str, payload: dict) -> None:
        try:
            body = client.query(api, payload).body
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 기록하고 계속(주문이 아님)
            step("raw_error", api=api, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return
        journal.event("raw_response", api, sym, api_id=api, body=body)

    def book() -> tuple[Decimal, Decimal]:
        b = client.query("overseas_bid-price", {"exch_cd": "FCME", "sym": sym}, retries=1).body
        return Decimal(str(b["bid_pric_1"])), Decimal(str(b["ask_pric_1"]))

    def fill_of(no: str, tries: int = 20):
        for _ in range(tries):
            rec = gw.fills_of(no)
            if rec is not None and rec.exec_qty and rec.exec_qty >= 1 and rec.exec_price:
                return rec
            time.sleep(1.0)
        return None

    def flat_after(label: str) -> bool:
        time.sleep(1.0)
        left = [x for x in gw.positions() if x.sym == sym]
        step(f"{label}_flat_check", left=[(x.side, str(x.qty)) for x in left])
        return not left

    report = gw.startup_reconcile()
    step("startup_reconcile", positions=len(report.positions), open_orders=len(report.open_orders))
    if report.positions or report.open_orders:
        print("기존 포지션·미체결이 있어 중단합니다.")
        return 3
    if not a.yes:
        print(f"모의계좌에 {sym} 1계약 주문을 냅니다. 동의하면 --yes 를 붙이세요.")
        return 1

    results: dict[str, bool] = {}
    try:
        raw("overseas_deposit", {"cur_cd": "KRW", "cvs_div": "2"})
        bid, ask = book()
        step("quote", bid=str(bid), ask=str(ask))
        raw("overseas_orderable-quantity", {"sym": sym, "glb_ord_tp_cd": "1", "ord_pric": "0", "stop_pric": ""})

        # ---- 2. 먼 지정가: 접수 → 정정 → 취소
        price = snap(bid - Decimal("25"))
        out = gw.place(OverseasOrderRequest(sym, "buy", 1, "limit", price=price, tick=TICK))
        step("far_limit_place", status=out.status, order_no=out.order_no, code=out.code, msg=out.message, price=str(price))
        ok = out.status == "accepted"
        if ok:
            time.sleep(1.5)
            raw("overseas_order-history", {"inq_div": "3", "trd_div_cd": "0", "nxt_key": ""})
            step("open_after_place", count=len([r for r in gw.open_orders() if r.sym == sym]))
            new_price = price - 5 * TICK
            mod = gw.modify(OverseasOrderRequest(sym, "buy", 1, "limit", price=new_price, tick=TICK), out.order_no)
            step("far_limit_modify", status=mod.status, order_no=mod.order_no, code=mod.code, msg=mod.message, price=str(new_price))
            time.sleep(1.5)
            cur = mod.order_no if mod.status == "accepted" and mod.order_no else out.order_no
            can = gw.cancel(sym, "buy", 1, cur)
            step("far_limit_cancel", status=can.status, order_no=can.order_no, code=can.code, msg=can.message, target=cur)
            time.sleep(1.5)
            left = [r for r in gw.open_orders() if r.sym == sym]
            step("open_after_cancel", count=len(left))
            ok = not left
        results["far_limit_cycle"] = ok

        # ---- 3. 엔진 진입 방식: 최우선 매수호가에 지정가 → timeout 대기 → 취소(체결되면 시장가 청산)
        bid, ask = book()
        e = gw.place(OverseasOrderRequest(sym, "buy", 1, "limit", price=bid, tick=TICK))
        step("engine_style_entry_place", status=e.status, order_no=e.order_no, code=e.code, msg=e.message, price=str(bid), ask_at_order=str(ask))
        ok = e.status == "accepted"
        if ok:
            rec = None
            for _ in range(timeout):
                rec = gw.fills_of(e.order_no)
                if rec is not None and rec.exec_qty and rec.exec_qty >= 1:
                    break
                time.sleep(1.0)
            filled = rec is not None and bool(rec.exec_qty and rec.exec_qty >= 1)
            step("engine_style_entry_result", filled=filled, status=rec.status if rec else None)
            if filled:
                s = gw.place(OverseasOrderRequest(sym, "sell", 1, reduce_only=True))
                step("engine_style_exit_market", status=s.status, order_no=s.order_no)
                ok = s.status == "accepted" and fill_of(s.order_no) is not None
            else:
                c = gw.cancel(sym, "buy", 1, e.order_no)
                step("engine_style_entry_cancel", status=c.status, order_no=c.order_no, code=c.code, msg=c.message)
                time.sleep(1.5)
                after = gw.fills_of(e.order_no)                       # 취소 직전 체결됐는지 재확인 (엔진과 같은 순서)
                if after is not None and after.exec_qty and after.exec_qty >= 1:
                    s = gw.place(OverseasOrderRequest(sym, "sell", 1, reduce_only=True))
                    step("engine_style_late_fill_exit", status=s.status, order_no=s.order_no)
                ok = not [r for r in gw.open_orders() if r.sym == sym]
            ok = ok and flat_after("engine_style")
        results["engine_style_entry"] = ok

        # ---- 4. 시장가 왕복
        buy = gw.place(OverseasOrderRequest(sym, "buy", 1))
        step("market_buy", status=buy.status, order_no=buy.order_no, code=buy.code, msg=buy.message)
        ok = False
        if buy.status == "accepted":
            rec = fill_of(buy.order_no)
            step("market_buy_fill", filled=rec is not None, price=str(rec.exec_price) if rec else None, status=rec.status if rec else None)
            raw("overseas_order-history", {"inq_div": "2", "trd_div_cd": "0", "nxt_key": ""})
            raw("overseas_open-interest", {"nxt_key": ""})
            held = [x for x in gw.positions() if x.sym == sym]
            step("positions_after_buy", detail=[(x.side, str(x.qty), str(x.avg_price)) for x in held])
            if rec is not None and held:
                sell = gw.place(OverseasOrderRequest(sym, "sell", int(held[0].qty), reduce_only=True))
                step("market_sell", status=sell.status, order_no=sell.order_no, code=sell.code, msg=sell.message)
                if sell.status == "accepted":
                    rec2 = fill_of(sell.order_no)
                    step("market_sell_fill", filled=rec2 is not None, price=str(rec2.exec_price) if rec2 else None, status=rec2.status if rec2 else None)
                    ok = rec2 is not None and flat_after("market")
        results["market_round_trip"] = ok
    finally:
        for r in gw.open_orders():
            if r.sym == sym:
                c = gw.cancel(r.sym, r.side, int(r.remaining_qty or r.qty or 1), r.order_no)
                step("cleanup_cancel", order_no=r.order_no, status=c.status)
        for pos in gw.positions():
            if pos.sym == sym:
                s = gw.place(OverseasOrderRequest(pos.sym, "sell" if pos.side == "buy" else "buy", int(pos.qty), reduce_only=True))
                step("cleanup_close", status=s.status, order_no=s.order_no, code=s.code, msg=s.message)
                if s.status == "accepted":
                    rec = fill_of(s.order_no)
                    step("cleanup_close_fill", filled=rec is not None, price=str(rec.exec_price) if rec else None)
        time.sleep(1.5)
        raw("overseas_order-history", {"inq_div": "0", "trd_div_cd": "0", "nxt_key": ""})
        raw("overseas_open-interest", {"nxt_key": ""})
        final = gw.positions()
        step("final", positions=len(final), open_orders=len(gw.open_orders()))
        raw("overseas_deposit", {"cur_cd": "KRW", "cvs_div": "2"})
        step("results", **results)
    print("결과:", results, "잔고 0" if not final else "잔고 남음 — 확인 필요")
    return 0 if not final and all(results.values()) else 4


if __name__ == "__main__":
    raise SystemExit(main())
