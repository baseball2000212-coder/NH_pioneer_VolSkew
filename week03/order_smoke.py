"""모의계좌 주문 스모크 — MGC 1계약. 실전(real) 환경은 지원하지 않습니다.

순서
 1. 시작 대사(보유·미체결이 있으면 중단), 예수금·주문가능수량 조회
 2. 시세에서 멀리 떨어진 지정가 매수 1계약 → 접수 확인 → 미체결 조회 → 정정 → 취소 → 미체결 없음 확인
 3. 시장가 매수 1계약 → 주문내역으로 체결 확인 → 잔고 확인 → 시장가 매도(청산) → 체결 확인 → 잔고 0 확인
 4. 예수금 재조회
모든 응답(가림 처리 후)과 단계가 JSONL 로 남습니다. 실패하거나 응답이 불명이면 즉시 멈추고 남은 포지션·미체결을 정리합니다.

  python order_smoke.py --run-id smoke-001 --log-dir ../logs
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
from nhapi.overseas import OverseasOrderRequest, parse_overseas_quote                # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--log-dir", default=str(HERE / "results"))
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    cfg = json.loads((HERE / "strategy.json").read_text(encoding="utf-8"))
    sym, tick = cfg["strategy"]["sym"], Decimal(cfg["strategy"]["tick"])
    journal = Journal(Path(a.log_dir) / f"{a.run_id}.jsonl", a.run_id, env="paper", mode="smoke", sym=sym)
    client = Client(load_config(find_config_path()), journal=journal)
    gw = OverseasGateway(client, journal=journal)

    def step(name: str, **data) -> None:
        print(f"[{name}]", {k: v for k, v in data.items() if k != "raw"})
        journal.event("step", name, sym, **data)

    def raw(api: str, payload: dict) -> dict:
        body = client.query(api, payload).body
        journal.event("raw_response", api, sym, api_id=api, body=body)
        return body

    report = gw.startup_reconcile()
    step("startup_reconcile", positions=len(report.positions), open_orders=len(report.open_orders))
    if report.positions or report.open_orders:
        print("기존 포지션·미체결이 있어 중단합니다.")
        return 3
    if not a.yes and input(f"모의계좌에 {sym} 주문을 냅니다(지정가 접수·정정·취소, 시장가 1계약 왕복). 계속할까요? [y/N] ").strip().lower() != "y":
        return 1
    raw("overseas_deposit", {"cur_cd": "KRW", "cvs_div": "2"})
    raw("overseas_orderable-quantity", {"sym": sym, "glb_ord_tp_cd": "1", "ord_pric": "0", "stop_pric": ""})
    quote = parse_overseas_quote(client.query("overseas_price", {"Iccurs1": [{"iccurs1_exch_cd": cfg["strategy"]["exchange"], "iccurs1_sym": sym}]}).body)
    last = quote.last
    step("quote", last=str(last), bid=str(quote.bid), ask=str(quote.ask))
    ok = True
    try:
        # ---- 2. 지정가 접수 → 정정 → 취소
        far = (last * Decimal("0.98") // tick) * tick
        out = gw.place(OverseasOrderRequest(sym, "buy", 1, "limit", price=far, tick=tick))
        step("limit_place", status=out.status, order_no=out.order_no, code=out.code, msg=out.message, price=str(far))
        if out.status != "accepted":
            ok = False
        else:
            time.sleep(1.5)
            raw("overseas_order-history", {"inq_div": "3", "trd_div_cd": "0", "nxt_key": ""})
            open_now = gw.open_orders()
            step("open_orders_after_place", count=len(open_now), numbers=[r.order_no for r in open_now], status=[r.status for r in open_now])
            new_price = far - 5 * tick
            mod = gw.modify(OverseasOrderRequest(sym, "buy", 1, "limit", price=new_price, tick=tick), out.order_no)
            step("limit_modify", status=mod.status, order_no=mod.order_no, code=mod.code, msg=mod.message, price=str(new_price))
            time.sleep(1.5)
            cur_no = mod.order_no if mod.status == "accepted" and mod.order_no else out.order_no
            raw("overseas_order-history", {"inq_div": "0", "trd_div_cd": "0", "nxt_key": ""})
            can = gw.cancel(sym, "buy", 1, cur_no)
            step("limit_cancel", status=can.status, order_no=can.order_no, code=can.code, msg=can.message, target=cur_no)
            time.sleep(1.5)
            left = gw.open_orders()
            step("open_orders_after_cancel", count=len(left), numbers=[r.order_no for r in left])
            if left:
                ok = False
        # ---- 3. 시장가 왕복
        if ok:
            def fill_of(no: str):
                for _ in range(20):
                    rec = gw.fills_of(no)
                    if rec is not None and rec.exec_qty and rec.exec_qty >= 1 and rec.exec_price:
                        return rec
                    time.sleep(1.0)
                return None

            buy = gw.place(OverseasOrderRequest(sym, "buy", 1))
            step("market_buy", status=buy.status, order_no=buy.order_no, code=buy.code, msg=buy.message)
            if buy.status == "accepted":
                rec = fill_of(buy.order_no)
                step("market_buy_fill", filled=rec is not None, price=str(rec.exec_price) if rec else None, status=rec.status if rec else None)
                raw("overseas_order-history", {"inq_div": "2", "trd_div_cd": "0", "nxt_key": ""})
                raw("overseas_open-interest", {"nxt_key": ""})
                held = gw.positions()
                step("positions_after_buy", count=len(held), detail=[(p.sym, p.side, str(p.qty), str(p.avg_price)) for p in held])
                if rec is None:
                    ok = False
            else:
                ok = False
    finally:
        # 정리: 남은 미체결 취소 → 남은 포지션 시장가 청산
        for r in gw.open_orders():
            c = gw.cancel(r.sym, r.side, int(r.remaining_qty or r.qty or 1), r.order_no)
            step("cleanup_cancel", order_no=r.order_no, status=c.status)
        held = gw.positions()
        for pos in held:
            side = "sell" if pos.side == "buy" else "buy"
            s = gw.place(OverseasOrderRequest(pos.sym, side, int(pos.qty)))
            step("market_close", status=s.status, order_no=s.order_no, code=s.code, msg=s.message)
            if s.status == "accepted":
                for _ in range(20):
                    rec = gw.fills_of(s.order_no)
                    if rec is not None and rec.exec_qty and rec.exec_price:
                        step("market_close_fill", price=str(rec.exec_price), status=rec.status)
                        break
                    time.sleep(1.0)
        time.sleep(1.5)
        raw("overseas_order-history", {"inq_div": "0", "trd_div_cd": "0", "nxt_key": ""})
        raw("overseas_open-interest", {"nxt_key": ""})
        final = gw.positions()
        step("final_positions", count=len(final), open_orders=len(gw.open_orders()))
        raw("overseas_deposit", {"cur_cd": "KRW", "cvs_div": "2"})
    print("결과:", "정상" if ok and not final else "확인 필요")
    return 0 if ok and not final else 4


if __name__ == "__main__":
    raise SystemExit(main())
