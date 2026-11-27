"""6주차 실행 기록: 15:45 종가 기준 계약당 손익(수수료 뒤)과 results/w06_runs.csv 한 줄.

모의계좌는 15:35:30 단일가 청산 주문을 그 자리에서 체결한다. 그래서 정해진 청산(scheduled)은 실제 15:45 종가로 다시 계산한다.
손절·킬·시세 끊김으로 나간 수량은 실제 체결가를 쓴다. 비용은 수수료 편도 0.0098% 만
(호가 충격은 조각 기록의 호가창 기준 체결가로 따로 본다).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

FEE = Decimal("0.000098")


def pnl_per_contract(trader, close_1545: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """(계약당 순손익 pt, 청산가). 진입가는 조각 체결 평균."""
    if trader.entry_price is None or not trader.qty_entered:
        return None, None
    res = trader.result or {}
    if res.get("exit_reason") == "scheduled":
        px = close_1545
    else:
        px = Decimal(str(res["exit_avg"])) if res.get("exit_avg") else None
    if px is None:
        return None, None
    side = 1 if trader.side == "buy" else -1
    e = Decimal(str(trader.entry_price))
    return side * (px - e) - FEE * (e + px), px


def result_row(run_id, mode, drill, trader, result, close_1545, ppc) -> dict[str, Any]:
    t = trader
    ch = getattr(t, "children", []) if t else []
    slips = [float(c["slip_ticks"]) for c in ch if c.get("slip_ticks") is not None]
    won = (ppc * t.qty_entered * 10000) if (t is not None and ppc is not None) else None
    return {
        "run_id": run_id, "mode": mode, "drill": drill, "outcome": result.get("outcome"), "reason": result.get("reason"),
        "side": getattr(t, "side", None), "planned_qty": t.plan.qty if (t and t.plan) else None,
        "filled_qty": getattr(t, "qty_entered", None), "children": len(ch),
        "slip_ticks_avg": round(sum(slips) / len(slips), 2) if slips else None,
        "entry_vwap": str(getattr(t, "entry_price", "") or ""), "exit_reason": result.get("exit_reason"),
        "gross_pt": result.get("gross_pt"), "close_1545": str(close_1545) if close_1545 is not None else None,
        "pnl_pt_per_contract_1545": str(round(ppc, 3)) if ppc is not None else None,
        "pnl_won_1545": str(round(won)) if won is not None else None,
        "stop_pt": str(t.plan.stop_pt) if (t and t.plan) else None,
    }
