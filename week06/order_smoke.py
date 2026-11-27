"""모의계좌 주문 스모크 — [모의계좌 주문] 1계약. 본 실행 전에 주문 경로가 실제로 되는지 확인한다.

    python order_smoke.py --stage am    --run-id smoke-1028-am    --confirm-orders   # 09:05~15:30
    python order_smoke.py --stage probe --run-id smoke-1028-probe --confirm-orders   # 15:36~15:43 (종가 단일가)

stage am    : ① 지정가 접수 → 정정 → 취소 (체결되지 않는 먼 가격) ② 시장가 매수 1 → 주문내역 체결 확인 → 시장가 매도 1 → 잔고 0 확인
stage probe : 종가 단일가 구간에 먼 가격 지정가 접수 가능 여부 → 취소. (체결 안 됨, 시장가는 본 실행 때 확인)

각 단계의 서버 응답·주문내역 원본은 logs/<run-id>.jsonl 에, 요약은 logs/<run-id>_summary.json 에 남는다.
시작 시 보유·미체결이 있으면 아무 주문도 내지 않고 멈춘다. 끝나면(예외 포함) 이 종목 미체결을 취소한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time as _time
from datetime import datetime, time
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

from nhapi.timeutil import now_kst, yyyymmdd  # noqa: E402

WINDOWS = {"am": (time(9, 5), time(15, 30)), "probe": (time(15, 36), time(15, 43))}
LIMIT_OFFSET_TICKS = 20      # 최우선 매수호가보다 2.0pt 아래 — 체결될 수 없는 먼 가격
MODIFY_EXTRA_TICKS = 5       # 정정은 0.5pt 더 아래로
HISTORY_WAIT_S = 6.0


def floor_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def rows_for(gw, order_nos: set[str]) -> list[dict[str, Any]]:
    out = []
    for r in gw.history():
        if r.order_no in order_nos or r.org_order_no in order_nos:
            out.append({"order_no": r.order_no, "org_order_no": r.org_order_no, "status_code": r.status_code, "status_name": r.status_name,
                        "qty": r.qty, "exec_qty": r.exec_qty, "remaining": r.remaining_qty, "cancelled": r.cancelled_qty, "price": r.price,
                        "reject_reason": r.reject_reason, "recv_time": r.recv_time})
    return out


def wait_rows(gw, order_nos: set[str], clock: Callable[[], datetime], sleep: Callable[[float], None], want: Callable[[list], bool]) -> list[dict[str, Any]]:
    start = clock()
    while True:
        rows = rows_for(gw, order_nos)
        if want(rows) or (clock() - start).total_seconds() >= HISTORY_WAIT_S:
            return rows
        sleep(0.5)


def _step(name: str, out, rows=None) -> dict[str, Any]:
    return {"step": name, "status": out.status, "code": out.code, "message": out.message, "order_no": out.order_no, "history": rows or []}


def limit_leg(gw, cfg, journal, bid: Decimal, *, clock, sleep) -> list[dict[str, Any]]:
    """지정가 접수 → 정정 → 취소. 한 단계가 실패하면 거기서 멈추고 정리는 호출자가 한다."""
    from nhapi.orders import OrderRequest

    tick = cfg.tick
    steps: list[dict[str, Any]] = []
    price = floor_tick(bid - LIMIT_OFFSET_TICKS * tick, tick)
    req = OrderRequest(cfg.sym, "buy", 1, "limit", price, "FAS", "day", tick)
    placed = gw.place(req)
    nos = {placed.order_no} if placed.order_no else set()
    rows = wait_rows(gw, nos, clock, sleep, lambda r: bool(r)) if nos else []
    steps.append({**_step("place_limit", placed, rows), "price": price})
    if placed.status != "accepted":
        return steps

    new_price = price - MODIFY_EXTRA_TICKS * tick
    modified = gw.modify(OrderRequest(cfg.sym, "buy", 1, "limit", new_price, "FAS", "day", tick), placed.order_no)
    if modified.order_no:
        nos.add(modified.order_no)
    steps.append({**_step("modify_limit", modified, wait_rows(gw, nos, clock, sleep, lambda r: len(r) >= 2)), "price": new_price})
    target = modified.order_no if modified.status == "accepted" and modified.order_no else placed.order_no

    cancelled = gw.cancel(cfg.sym, "buy", 1, target)
    if cancelled.order_no:
        nos.add(cancelled.order_no)
    still_open = [r for r in gw.open_orders() if r.sym == cfg.sym and r.order_no in nos]
    steps.append({**_step("cancel_limit", cancelled, rows_for(gw, nos)), "still_open_after_cancel": len(still_open)})
    return steps


def market_leg(broker, gw, cfg, book_before: dict[str, Any]) -> list[dict[str, Any]]:
    """시장가 매수 1 → 체결 확인 → 시장가 매도 1 → 잔고 0 확인."""
    steps: list[dict[str, Any]] = []
    buy = broker.market_order("buy", 1, "smoke_entry")
    steps.append({"step": "market_buy", "status": buy.status, "filled": buy.filled_qty, "price": buy.price, "order_no": buy.order_no,
                  "code": buy.code, "message": buy.message, "ask_before": book_before.get("ask"), "bid_before": book_before.get("bid"),
                  "slippage_vs_ask_pt": None if buy.price is None or book_before.get("ask") is None else buy.price - book_before["ask"]})
    steps.append({"step": "position_after_buy", "net_position": broker.net_position()})
    if buy.filled_qty <= 0:
        return steps
    sell = broker.market_order("sell", buy.filled_qty, "smoke_exit")
    steps.append({"step": "market_sell", "status": sell.status, "filled": sell.filled_qty, "price": sell.price, "order_no": sell.order_no,
                  "code": sell.code, "message": sell.message})
    steps.append({"step": "position_after_sell", "net_position": broker.net_position()})
    if buy.price is not None and sell.price is not None:
        steps.append({"step": "round_trip", "gross_pt": sell.price - buy.price, "note": "스프레드·슬리피지가 포함된 왕복 손익(수수료 별도)"})
    return steps


def probe_leg(gw, cfg, journal, bid: Decimal, *, clock, sleep) -> list[dict[str, Any]]:
    """종가 단일가 구간에서 먼 가격 지정가가 접수되는지 → 취소."""
    from nhapi.orders import OrderRequest

    price = floor_tick(bid - LIMIT_OFFSET_TICKS * cfg.tick, cfg.tick)
    placed = gw.place(OrderRequest(cfg.sym, "buy", 1, "limit", price, "FAS", "day", cfg.tick))
    nos = {placed.order_no} if placed.order_no else set()
    rows = wait_rows(gw, nos, clock, sleep, lambda r: bool(r)) if nos else []
    steps = [{**_step("auction_place_limit", placed, rows), "price": price}]
    if placed.status == "accepted":
        cancelled = gw.cancel(cfg.sym, "buy", 1, placed.order_no)
        steps.append({**_step("auction_cancel_limit", cancelled, wait_rows(gw, nos | ({cancelled.order_no} if cancelled.order_no else set()), clock, sleep, lambda r: len(r) >= 1))})
    return steps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="모의계좌 주문 스모크(1계약)")
    ap.add_argument("--stage", choices=["am", "probe"], required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--strategy", default=str(HERE / "strategy.json"))
    ap.add_argument("--logs", default=str(HERE / "logs"))
    ap.add_argument("--confirm-orders", action="store_true")
    ap.add_argument("--ignore-hours", action="store_true", help="시간 창 검사를 건너뜀(권장하지 않음)")
    args = ap.parse_args(argv)
    if not args.confirm_orders:
        print("모의계좌에 실제 주문을 냅니다. 확인하려면 --confirm-orders 를 붙이세요.", file=sys.stderr)
        return 2
    lo, hi = WINDOWS[args.stage]
    now = now_kst()
    if not args.ignore_hours and not (lo <= now.time() <= hi):
        print(f"stage {args.stage} 는 {lo:%H:%M}~{hi:%H:%M} 에만 실행합니다 (지금 {now:%H:%M:%S}).", file=sys.stderr)
        return 3

    from nhapi import Client, Journal, load_config
    from nhapi.orders import OrderGateway
    from nhapi.parsers import parse_book, parse_deposit

    from live import NhOrderBroker
    from strategy_core import StrategyConfig

    cfg = StrategyConfig.from_dict(json.loads(Path(args.strategy).read_text(encoding="utf-8")))
    run_id = args.run_id or f"smoke-{args.stage}-{now:%Y%m%d-%H%M%S}"
    logs = Path(args.logs)
    journal = Journal(logs / f"{run_id}.jsonl", run_id, env="paper", mode="smoke", sym=cfg.sym)
    client = Client(load_config(), journal=journal)
    gw = OrderGateway(client, journal=journal)
    broker = NhOrderBroker(gw, cfg, journal)
    summary: dict[str, Any] = {"run_id": run_id, "stage": args.stage, "label": "[모의계좌 주문] 스모크", "started": now.isoformat(timespec="seconds"), "steps": []}
    code = 0
    try:
        report = gw.startup_reconcile()
        mine = [p for p in report.positions if p.sym == cfg.sym]
        summary["start_state"] = {"positions": len(report.positions), "open_orders": len(report.open_orders)}
        if mine or report.open_orders:
            print("시작 시 보유 또는 미체결이 있어 주문을 내지 않습니다. 잔고·주문내역을 확인하세요.", file=sys.stderr)
            summary["aborted"] = "start_state_not_clean"
            return 4
        book = parse_book(client.query("domestic_price-bid", {"sym": cfg.sym, "exch_cd": "KSP"}).body)
        if book.best_bid is None:
            print("호가를 얻지 못해 중단합니다.", file=sys.stderr)
            summary["aborted"] = "no_book"
            return 4
        before = {"bid": book.best_bid, "ask": book.best_ask}
        summary["book_before"] = before
        dep0 = _deposit(client, parse_deposit)

        if args.stage == "am":
            summary["steps"] += limit_leg(gw, cfg, journal, book.best_bid, clock=now_kst, sleep=_time.sleep)
            _cleanup(broker, summary)
            if _limit_ok(summary["steps"]):
                summary["steps"] += market_leg(broker, gw, cfg, before)
            else:
                summary["market_leg"] = "지정가 단계가 실패해 시장가 왕복은 건너뜀"
        else:
            summary["steps"] += probe_leg(gw, cfg, journal, book.best_bid, clock=now_kst, sleep=_time.sleep)

        dep1 = _deposit(client, parse_deposit)
        summary["deposit"] = {"start": dep0, "end": dep1}
    except BaseException as exc:  # noqa: BLE001 — 어떤 중단이든 정리와 요약은 남긴다.
        summary["error"] = f"{type(exc).__name__}: {exc}"
        journal.error("smoke", msg=summary["error"], action="cleanup")
        code = 1
    finally:
        _cleanup(broker, summary)
        try:
            summary["final_position"] = broker.net_position()
        except Exception as exc:  # noqa: BLE001
            summary["final_position"] = f"조회 실패: {exc}"
        summary["finished"] = now_kst().isoformat(timespec="seconds")
        out = logs / f"{run_id}_summary.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        client.close()
    _print(summary, logs / f"{run_id}_summary.json")
    return code


def _limit_ok(steps: list[dict[str, Any]]) -> bool:
    """지정가 단계가 끝났는지(성공 여부와 별개로 미체결이 남지 않았는지)만 본다. 정정·취소가 모의에서 막혀도 시장가 왕복은 진행."""
    return not any(s.get("still_open_after_cancel") for s in steps)


def _cleanup(broker, summary: dict[str, Any]) -> None:
    try:
        broker.cancel_open()
    except Exception as exc:  # noqa: BLE001
        summary.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")


def _deposit(client, parse_deposit) -> dict[str, Any]:
    try:
        d = parse_deposit(client.query("domestic_deposit", {"biz_dt": yyyymmdd(now_kst())}).body)
        return {"total_assets": d.total_assets, "cash": d.cash, "orderable_total": d.orderable_total, "fee": d.fee}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _print(summary: dict[str, Any], path: Path) -> None:
    print(f"\n[{summary['stage']}] {summary['run_id']}")
    for s in summary["steps"]:
        brief = {k: v for k, v in s.items() if k not in ("history",) and v not in (None, "", [])}
        print("  ", brief)
        for row in s.get("history", [])[:4]:
            print("      주문내역:", {k: row[k] for k in ("order_no", "status_name", "qty", "exec_qty", "price", "reject_reason")})
    print("종료 시 보유:", summary.get("final_position"), "| 예수금 fee:", summary.get("deposit", {}).get("end", {}).get("fee"))
    print("요약 파일:", path)


if __name__ == "__main__":
    sys.exit(main())
