"""모의계좌 주문 스모크 — MES 선물과 ES 주간옵션 콜 각 1계약. 실전(real) 환경은 지원하지 않습니다.

순서
 1. 시작 대사(보유·미체결이 있으면 중단), 예수금·주문가능수량 조회(옵션 신규 매도 가능 수량 포함)
 2. MES: 시세에서 멀리 떨어진 지정가 매수 1계약 → 접수·미체결 확인 → 정정 → 취소 → 미체결 없음 확인
 3. 옵션 ATM 콜: 같은 순서(지정가 접수·정정·취소)
 4. 옵션 신규 매도 점검: 보유 없이 **체결될 수 없는 높은 가격**의 지정가 매도 1계약 → 접수/거부(7093?) 기록, 접수되면 취소
 5. MES 시장가 매수 1계약 → 주문내역으로 체결 확인 → 잔고 → 시장가 매도 → 체결 확인 → 잔고 0
 6. 옵션 콜 시장가 매수 1계약 → 체결 확인 → 보유분 시장가 매도(reduce_only) → 체결 확인 → 잔고 0
 7. 잔고·미체결·예수금 재조회
모든 응답(가림 처리 후)과 단계가 JSONL(mode=smoke) 로 남습니다. 실패하거나 응답이 불명이면 해당 단계를 멈추고 마지막에 남은 미체결 취소·포지션 청산을 합니다.

  python order_smoke.py --run-id smoke-w04-a --log-dir ../logs --yes
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


def snap(price: Decimal, tick: Decimal = TICK) -> Decimal:
    return (price // tick) * tick


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--log-dir", default=str(HERE / "logs"))
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    conf = json.loads((HERE / "strategy.json").read_text(encoding="utf-8"))
    series, es_sym, mes_sym = conf["strategy"]["series"], conf["strategy"]["es_sym"], conf["strategy"]["mes_sym"]
    journal = Journal(Path(a.log_dir) / f"{a.run_id}.jsonl", a.run_id, env="paper", mode="smoke", sym=series)
    keys = load_config(find_config_path())
    if keys.default_environment != "paper":
        raise SystemExit("모의(paper) 키가 아닙니다. 중단합니다.")
    client = Client(keys, journal=journal)
    gw = OverseasGateway(client, journal=journal)
    touched: set[str] = set()

    def step(name: str, **data) -> None:
        print(f"[{name}]", {k: v for k, v in data.items() if k != "raw"}, flush=True)
        journal.event("step", name, data.pop("sym", None), **data)

    def raw(api: str, payload: dict, sym: str | None = None) -> dict | None:
        try:
            body = client.query(api, payload).body
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 기록하고 계속(주문이 아님)
            step("raw_error", api=api, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return None
        journal.event("raw_response", api, sym, api_id=api, body=body)
        return body

    def book(sym: str, exch: str) -> tuple[Decimal, Decimal]:
        b = client.query("overseas_bid-price", {"exch_cd": exch, "sym": sym}, retries=1).body
        return Decimal(str(b["bid_pric_1"])), Decimal(str(b["ask_pric_1"]))

    def fill_of(no: str, tries: int = 20):
        for _ in range(tries):
            rec = gw.fills_of(no)
            if rec is not None and rec.exec_qty and rec.exec_qty >= 1 and rec.exec_price:
                return rec
            time.sleep(1.0)
        return None

    def limit_cycle(label: str, sym: str, price: Decimal) -> bool:
        """지정가 접수 → 미체결 확인 → 정정 → 취소 → 미체결 없음. 성공하면 True."""
        touched.add(sym)
        out = gw.place(OverseasOrderRequest(sym, "buy", 1, "limit", price=price, tick=TICK))
        step(f"{label}_limit_place", sym=sym, status=out.status, order_no=out.order_no, code=out.code, msg=out.message, price=str(price))
        if out.status != "accepted":
            return False
        time.sleep(1.5)
        raw("overseas_order-history", {"inq_div": "3", "trd_div_cd": "0", "nxt_key": ""}, sym)
        now_open = [r for r in gw.open_orders() if r.sym == sym]
        step(f"{label}_open_after_place", count=len(now_open), numbers=[r.order_no for r in now_open], status=[r.status for r in now_open])
        new_price = price - 5 * TICK
        mod = gw.modify(OverseasOrderRequest(sym, "buy", 1, "limit", price=new_price, tick=TICK), out.order_no)
        step(f"{label}_limit_modify", sym=sym, status=mod.status, order_no=mod.order_no, code=mod.code, msg=mod.message, price=str(new_price))
        time.sleep(1.5)
        cur = mod.order_no if mod.status == "accepted" and mod.order_no else out.order_no
        raw("overseas_order-history", {"inq_div": "0", "trd_div_cd": "0", "nxt_key": ""}, sym)
        can = gw.cancel(sym, "buy", 1, cur)
        step(f"{label}_limit_cancel", sym=sym, status=can.status, order_no=can.order_no, code=can.code, msg=can.message, target=cur)
        time.sleep(1.5)
        left = [r for r in gw.open_orders() if r.sym == sym]
        step(f"{label}_open_after_cancel", count=len(left), numbers=[r.order_no for r in left])
        return not left

    def market_round_trip(label: str, sym: str) -> bool:
        touched.add(sym)
        buy = gw.place(OverseasOrderRequest(sym, "buy", 1))
        step(f"{label}_market_buy", sym=sym, status=buy.status, order_no=buy.order_no, code=buy.code, msg=buy.message)
        if buy.status != "accepted":
            return False
        rec = fill_of(buy.order_no)
        step(f"{label}_market_buy_fill", sym=sym, filled=rec is not None, price=str(rec.exec_price) if rec else None, status=rec.status if rec else None)
        raw("overseas_order-history", {"inq_div": "2", "trd_div_cd": "0", "nxt_key": ""}, sym)
        raw("overseas_open-interest", {"nxt_key": ""}, sym)
        held = [x for x in gw.positions() if x.sym == sym]
        step(f"{label}_positions_after_buy", sym=sym, detail=[(x.side, str(x.qty), str(x.avg_price)) for x in held])
        if rec is None or not held:
            return False
        sell = gw.place(OverseasOrderRequest(sym, "sell", int(held[0].qty), reduce_only=True))
        step(f"{label}_market_sell", sym=sym, status=sell.status, order_no=sell.order_no, code=sell.code, msg=sell.message)
        if sell.status != "accepted":
            return False
        rec2 = fill_of(sell.order_no)
        step(f"{label}_market_sell_fill", sym=sym, filled=rec2 is not None, price=str(rec2.exec_price) if rec2 else None, status=rec2.status if rec2 else None)
        time.sleep(1.0)
        left = [x for x in gw.positions() if x.sym == sym]
        step(f"{label}_flat_check", sym=sym, left=[(x.side, str(x.qty)) for x in left])
        return rec2 is not None and not left

    report = gw.startup_reconcile()
    step("startup_reconcile", positions=len(report.positions), open_orders=len(report.open_orders))
    if report.positions or report.open_orders:
        print("기존 포지션·미체결이 있어 중단합니다.")
        return 3
    if not a.yes:
        print("모의계좌에 MES·ES 옵션 1계약 주문을 냅니다. 동의하면 --yes 를 붙이세요.")
        return 1

    results: dict[str, bool] = {}
    final: list = []
    try:
        raw("overseas_deposit", {"cur_cd": "KRW", "cvs_div": "2"})
        es_bid, es_ask = book(es_sym, "FCME")
        mes_bid, mes_ask = book(mes_sym, "FCME")
        strike = (((es_bid + es_ask) / 2) / 5).quantize(Decimal("1")) * 5
        call = f"{series}-C{strike:.1f}"
        c_bid, c_ask = book(call, "OCME")
        step("quotes", es=[str(es_bid), str(es_ask)], mes=[str(mes_bid), str(mes_ask)], call=call, call_book=[str(c_bid), str(c_ask)])
        raw("overseas_orderable-quantity", {"sym": mes_sym, "glb_ord_tp_cd": "1", "ord_pric": "0", "stop_pric": ""}, mes_sym)
        raw("overseas_orderable-quantity", {"sym": call, "glb_ord_tp_cd": "1", "ord_pric": "0", "stop_pric": ""}, call)
        # 옵션 신규 매도 가능 수량 조회(같은 API 에 방향 칸이 있는지는 명세 확인 전 — 기본 칸으로만 조회)
        results["mes_limit_cycle"] = limit_cycle("mes", mes_sym, snap(mes_bid - Decimal("25")))
        results["opt_limit_cycle"] = limit_cycle("opt", call, snap(c_bid * Decimal("0.5")))
        # ---- 옵션 신규 매도 점검: 체결될 수 없는 높은 가격의 지정가 매도 (보유 없음)
        touched.add(call)
        sell_px = snap(c_ask + Decimal("40"))
        s = gw.place(OverseasOrderRequest(call, "sell", 1, "limit", price=sell_px, tick=TICK))
        step("opt_naked_sell_probe", sym=call, status=s.status, order_no=s.order_no, code=s.code, msg=s.message, price=str(sell_px))
        results["opt_naked_sell_probe_rejected"] = s.status == "rejected"
        if s.status == "accepted":
            time.sleep(1.5)
            c = gw.cancel(call, "sell", 1, s.order_no)
            step("opt_naked_sell_probe_cancel", status=c.status, order_no=c.order_no, code=c.code, msg=c.message)
        if s.status == "rejected" and s.code == "7093":
            journal.api_issue("CAND-7093", "overseas_order", known_discrepancy=False, sym=call, code=s.code, note="옵션 신규매도 거부", msg=s.message)
        results["mes_market_round_trip"] = market_round_trip("mes", mes_sym)
        results["opt_market_round_trip"] = market_round_trip("opt", call)
    finally:
        for r in gw.open_orders():
            if r.sym in touched:
                c = gw.cancel(r.sym, r.side, int(r.remaining_qty or r.qty or 1), r.order_no)
                step("cleanup_cancel", sym=r.sym, order_no=r.order_no, status=c.status)
        for pos in gw.positions():
            if pos.sym in touched:
                side = "sell" if pos.side == "buy" else "buy"
                s = gw.place(OverseasOrderRequest(pos.sym, side, int(pos.qty), reduce_only=True))
                step("cleanup_close", sym=pos.sym, status=s.status, order_no=s.order_no, code=s.code, msg=s.message)
                if s.status == "accepted":
                    rec = fill_of(s.order_no)
                    step("cleanup_close_fill", sym=pos.sym, filled=rec is not None, price=str(rec.exec_price) if rec else None)
        time.sleep(1.5)
        raw("overseas_order-history", {"inq_div": "0", "trd_div_cd": "0", "nxt_key": ""})
        raw("overseas_open-interest", {"nxt_key": ""})
        final = gw.positions()
        step("final", positions=len(final), open_orders=len(gw.open_orders()))
        raw("overseas_deposit", {"cur_cd": "KRW", "cvs_div": "2"})
        step("results", **{k: v for k, v in results.items()})
    flat = not final
    print("결과:", results, "잔고 0" if flat else "잔고 남음 — 확인 필요")
    return 0 if flat and all(v for k, v in results.items() if k != "opt_naked_sell_probe_rejected") else 4


if __name__ == "__main__":
    raise SystemExit(main())
