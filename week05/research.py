"""연구·검증 파이프라인 (백테스트): 후보 8개 사전 등록 → in-sample CV 로 하나 선택 → out-of-sample 한 번 평가 → 교차상품 확인.

  python research.py --data-dir ../data --out-dir results --run-id bt-20261117

순서 (전략 규칙은 OOS 결과를 보기 전에 고정한다):
 1. 1분봉 → 5분봉 → 특징 → 라벨 (라벨 임계 = label_mult × 왕복 비용)
 2. 후보 8개(h × 게이트 × 라벨 임계) 각각 IS 를 purged expanding CV 로 평가 → 비용 후 거래당 평균손익이 가장 큰 후보 선택
 3. 선택·나머지 후보를 IS 전체로 학습한 **고정 모형**으로 OOS 평가 (8개 전부 보고, 선택은 CV 로만 했다)
 4. 기준선(z-score 평균회귀), T² 게이트 효과, SVM 분류 성능, 교차상품(MNQ·MGC·6E: 각자 IS 로 같은 절차 학습)
결과 JSON 에 가정(assumptions)을 함께 저장한다.
"""
from __future__ import annotations

import argparse
import gzip
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backtest import AssetSpec, asset_spec, simulate, stats  # noqa: E402
from bars import load_minute, quality_report, to_bars  # noqa: E402
from signals import FEATURES, SignalModel, compute_features, decide, forward_pnl, in_blackout, make_labels  # noqa: E402


def load_asset(sym: str, data_dir: Path, cfg: dict[str, Any]):
    minute = load_minute(data_dir / f"minute_{sym}.csv.gz")
    bars = to_bars(minute, cfg["bar_minutes"], cfg["min_sub"])
    return minute, bars, compute_features(bars, cfg)


def date_mask(index: pd.DatetimeIndex, start: str, end: str) -> np.ndarray:
    d = index.normalize()
    return np.asarray((d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end)))


def train_model(bars, feats, spec: AssetSpec, cfg, h: int, mult: float, train_mask: np.ndarray, label: str = "") -> SignalModel:
    fwd = forward_pnl(bars, h, cfg)
    y = make_labels(fwd, mult * spec.cost_pt("opposite"))
    rows = feats["ok"].to_numpy() & y.notna().to_numpy() & train_mask
    x = feats.loc[rows, list(FEATURES)].to_numpy()
    yy = y[rows].astype(int).to_numpy()
    meta = {"label": label, "h": h, "label_mult": mult, "train_rows": int(rows.sum()),
            "class_counts": {int(k): int((yy == k).sum()) for k in (-1, 0, 1)}}
    return SignalModel.fit(x, yy, cfg, meta)


def run_trades(bars, feats, model, spec, cfg, h, gate, fill_model, window, mask: np.ndarray | None = None) -> pd.DataFrame:
    sig = decide(model, feats, cfg, use_gate=gate)
    side = sig["side"].copy()
    if mask is not None:
        side[~mask] = 0
    tr = simulate(bars, feats, side, h=h, spec=spec, cfg=cfg, fill_model=fill_model, window=tuple(cfg["windows"][window]),
                  max_per_day=cfg["risk"]["max_trades_per_day"])
    if not tr.empty:
        tr = tr.join(sig[["t2", "in_control"]], on="signal_ts")
    return tr


def cv_score(bars, feats, spec, cfg, h, gate, mult, is_mask) -> dict[str, Any]:
    """IS 날짜를 시간순 조각으로 나눠 expanding 학습 / 다음 조각 평가 (학습 행은 평가 시작 h 봉 전까지만 = purge)."""
    dates = np.array(sorted(set(bars.index[is_mask].normalize())))
    chunks = np.array_split(dates, cfg["cv"]["folds"] + 1)
    pieces = []
    for k in range(1, len(chunks)):
        test_days = set(chunks[k])
        test_start = chunks[k][0]
        purge = test_start - pd.Timedelta(minutes=cfg["bar_minutes"] * (h + 1))
        train_mask = is_mask & np.asarray(bars.index < purge)
        test_mask = np.asarray(bars.index.normalize().isin(test_days))
        model = train_model(bars, feats, spec, cfg, h, mult, train_mask)
        pieces.append(run_trades(bars, feats, model, spec, cfg, h, gate, cfg["primary"]["fill_model"], cfg["primary"]["window"], test_mask))
    tr = pd.concat([p for p in pieces if not p.empty]) if any(not p.empty for p in pieces) else pd.DataFrame()
    st = stats(tr, reps=200, seed=cfg["bootstrap"]["seed"])
    st["score"] = (st["net_total_usd"] / st["n"]) if st.get("n", 0) >= 10 else -1e9
    return st


def baseline_z(bars, feats, spec, cfg, mask, fill_model, window) -> pd.DataFrame:
    k, h = cfg["baseline_z"]["k"], cfg["baseline_z"]["h"]
    z = feats["z"]
    side = pd.Series(0, index=feats.index)
    sel = feats["ok"] & (z.abs() >= k) & ~pd.Series(in_blackout(feats.index, cfg), index=feats.index)
    side[sel] = -np.sign(z[sel]).astype(int)
    side[~mask] = 0
    return simulate(bars, feats, side, h=h, spec=spec, cfg=cfg, fill_model=fill_model, window=tuple(cfg["windows"][window]),
                    max_per_day=cfg["risk"]["max_trades_per_day"])


def classification_report(model, bars, feats, spec, cfg, h, mult, mask) -> dict[str, Any]:
    fwd = forward_pnl(bars, h, cfg)
    y = make_labels(fwd, mult * spec.cost_pt("opposite"))
    rows = feats["ok"].to_numpy() & y.notna().to_numpy() & mask
    pred = model.predict(feats.loc[rows, list(FEATURES)].to_numpy())
    true = y[rows].astype(int).to_numpy()
    conf = {f"true{t}": {f"pred{p}": int(((true == t) & (pred == p)).sum()) for p in (-1, 0, 1)} for t in (-1, 0, 1)}
    majority = max(int((true == k).sum()) for k in (-1, 0, 1)) / max(len(true), 1)
    out = {"rows": int(rows.sum()), "accuracy": round(float((pred == true).mean()), 4), "majority_baseline": round(majority, 4), "confusion": conf}
    for side, name in ((1, "long"), (-1, "short")):
        n_pred = int((pred == side).sum())
        out[f"{name}_precision"] = round(float(((pred == side) & (true == side)).sum() / n_pred), 4) if n_pred else None
        out[f"{name}_predicted"] = n_pred
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="5주차 백테스트·검증 파이프라인")
    p.add_argument("--config", default=str(HERE / "strategy.json"))
    p.add_argument("--data-dir", default=str(HERE / "data"))
    p.add_argument("--out-dir", default=str(HERE / "results"))
    p.add_argument("--run-id", required=True)
    args = p.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    data_dir, out = Path(args.data_dir), Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sp = cfg["split"]
    sym = cfg["trade"]["symbol"]
    primary_fill, primary_window = cfg["primary"]["fill_model"], cfg["primary"]["window"]
    results: dict[str, Any] = {
        "run_id": args.run_id, "env": "paper(시세만)", "mode": "backtest", "source": "backtest-model", "strategy_id": cfg["strategy_id"],
        "assumptions": {
            "signal": "5분봉 종가에서 계산, 체결은 다음 봉 시가(지연 1봉)",
            "fill_models": {"opposite": "진입·청산 모두 맞은편 호가 (호가 폭 2번)", "touch": "진입 지정가 터치(호가 폭 0) + 청산 시장가(호가 폭 1번)"},
            "stop": f"진입가 ∓ {cfg['stop_k']} × σ5m × √h, 한 봉에서 손절·이익 동시 가능하면 손절 먼저, 시가 갭이면 시가 청산",
            "windows": cfg["windows"], "flatten": cfg["flatten"], "blackout": cfg["blackout"],
            "costs": {s: {"fee_roundtrip_usd": a["fee_roundtrip_usd"], "spread_ticks": a["spread_ticks"], "note": a["note"]} for s, a in cfg["assets"].items()},
            "split": sp, "primary": cfg["primary"],
            "selection": "IS purged expanding CV 에서 비용 후 거래당 평균손익 최대 후보. OOS 는 선택 후 한 번만 평가",
            "t2_ucl": f"IS T² 의 경험적 {cfg['t2']['ucl_quantile']} 분위수",
        }}

    # ---------------- MES
    minute, bars, feats = load_asset(sym, data_dir, cfg)
    spec = asset_spec(cfg, sym)
    is_mask, oos_mask = date_mask(bars.index, sp["is_start"], sp["is_end"]), date_mask(bars.index, sp["oos_start"], sp["oos_end"])
    results["data"] = {"quality": quality_report(minute, bars), "is_bars": int(is_mask.sum()), "oos_bars": int(oos_mask.sum()),
                       "is_days": int(bars.index[is_mask].normalize().nunique()), "oos_days": int(bars.index[oos_mask].normalize().nunique())}
    print(f"{sym}: 5분봉 {len(bars)} IS {is_mask.sum()} OOS {oos_mask.sum()}", flush=True)

    cands = []
    for h, gate, mult in itertools.product(cfg["candidates"]["h"], cfg["candidates"]["gate"], cfg["candidates"]["label_mult"]):
        cv = cv_score(bars, feats, spec, cfg, h, gate, mult, is_mask)
        model = train_model(bars, feats, spec, cfg, h, mult, is_mask, label=f"h{h}-gate{int(gate)}-m{mult}")
        row = {"id": f"h{h}-gate{int(gate)}-m{mult}", "h": h, "gate": gate, "label_mult": mult, "cv": cv, "_model": model}
        for fm in cfg["fill_models"]:
            for w in ("live", "all"):
                row[f"is_fit_{w}_{fm}"] = stats(run_trades(bars, feats, model, spec, cfg, h, gate, fm, w, is_mask), reps=200)
                row[f"oos_{w}_{fm}"] = stats(run_trades(bars, feats, model, spec, cfg, h, gate, fm, w, oos_mask), reps=cfg["bootstrap"]["reps"], seed=cfg["bootstrap"]["seed"])
        cands.append(row)
        print(f"  {row['id']}: CV n={cv.get('n')} 거래당 {cv.get('score'):.2f}$ | OOS(live,opposite) n={row['oos_live_opposite'].get('n')} 거래당 {row['oos_live_opposite'].get('net_mean_usd')}", flush=True)
    best = max(cands, key=lambda r: r["cv"]["score"])
    results["selected"] = best["id"]
    print("선택(CV 기준):", best["id"], flush=True)
    results["candidates"] = [{k: v for k, v in r.items() if k != "_model"} for r in cands]
    sel_model: SignalModel = best["_model"]
    sel_model.meta.update({"id": best["id"], "gate": best["gate"], "symbol": sym, "trained_on": f"{sp['is_start']}~{sp['is_end']}",
                           "run_id": args.run_id})
    sel_model.to_json(HERE / f"model_{sym}.json")
    sel_model.to_json(out / f"model_{sym}.json")

    # 선택 후보의 OOS 거래와 T²/분류/기준선 분석
    h, gate, mult = best["h"], best["gate"], best["label_mult"]
    tr_oos = run_trades(bars, feats, sel_model, spec, cfg, h, gate, primary_fill, primary_window, oos_mask)
    if not tr_oos.empty:
        tr_oos.drop(columns=["day"]).to_csv(out / f"trades_backtest_{sym}_oos.csv", index=False)
    results["selected_detail"] = {
        "oos_trades": stats(tr_oos, reps=cfg["bootstrap"]["reps"], seed=cfg["bootstrap"]["seed"]),
        "classification_oos_live_window": classification_report(sel_model, bars, feats, spec, cfg, h, mult, oos_mask & np.asarray(
            [True] * len(bars))),
        "svm_coef": {str(int(c)): dict(zip(FEATURES, np.round(coef, 4).tolist())) for c, coef in zip(sel_model.classes, sel_model.coef)},
        "t2_ucl": sel_model.chart.ucl,
    }
    ungated = run_trades(bars, feats, sel_model, spec, cfg, h, False, primary_fill, primary_window, oos_mask)
    if not ungated.empty:
        gb = {}
        for name, sub in (("in_control", ungated[ungated.in_control]), ("out_of_control", ungated[~ungated.in_control])):
            gb[name] = stats(sub, reps=500, seed=cfg["bootstrap"]["seed"])
        results["selected_detail"]["gate_effect_oos_ungated_trades"] = gb
    sig = decide(sel_model, feats, cfg, use_gate=False)
    results["selected_detail"]["t2_out_of_control_ratio"] = {
        "is": round(float((~sig.loc[is_mask & feats["ok"].to_numpy(), "in_control"]).mean()), 4),
        "oos": round(float((~sig.loc[oos_mask & feats["ok"].to_numpy(), "in_control"]).mean()), 4)}
    results["baseline_z_mr"] = {
        f"{w}_{fm}": {"is": stats(baseline_z(bars, feats, spec, cfg, is_mask, fm, w), reps=200),
                      "oos": stats(baseline_z(bars, feats, spec, cfg, oos_mask, fm, w), reps=cfg["bootstrap"]["reps"], seed=cfg["bootstrap"]["seed"])}
        for w in ("live", "all") for fm in cfg["fill_models"]}

    # 산출 데이터: 5분봉, T² 시계열
    bars.join(feats[list(FEATURES)]).join(sig[["t2", "in_control", "svm_side", "side"]]).to_csv(out / f"bars_{sym}_5m.csv.gz", compression="gzip")

    # ---------------- 교차상품 (각자 IS 로 같은 설정 학습, 규칙·후보 재선택 없음)
    cross = {}
    for csym in [s for s in cfg["assets"] if s != sym]:
        try:
            m2, b2, f2 = load_asset(csym, data_dir, cfg)
        except FileNotFoundError:
            continue
        s2 = asset_spec(cfg, csym)
        i2, o2 = date_mask(b2.index, sp["is_start"], sp["is_end"]), date_mask(b2.index, sp["oos_start"], sp["oos_end"])
        mdl = train_model(b2, f2, s2, cfg, h, mult, i2, label=f"{csym}-{best['id']}")
        entry = {"bars": int(len(b2)), "cost_pt": {fm: round(s2.cost_pt(fm), 5) for fm in cfg["fill_models"]}}
        for fm in cfg["fill_models"]:
            entry[f"oos_live_{fm}"] = stats(run_trades(b2, f2, mdl, s2, cfg, h, gate, fm, "live", o2), reps=cfg["bootstrap"]["reps"], seed=cfg["bootstrap"]["seed"])
            entry[f"oos_all_{fm}"] = stats(run_trades(b2, f2, mdl, s2, cfg, h, gate, fm, "all", o2), reps=cfg["bootstrap"]["reps"], seed=cfg["bootstrap"]["seed"])
        entry["baseline_z_oos_live_opposite"] = stats(baseline_z(b2, f2, s2, cfg, o2, "opposite", "live"), reps=500, seed=cfg["bootstrap"]["seed"])
        cross[csym] = entry
        print("교차", csym, entry["oos_live_opposite"].get("n"), entry["oos_live_opposite"].get("net_mean_usd"), flush=True)
    results["cross_assets"] = cross

    # 수집 요약 합치기
    summaries = sorted(data_dir.glob("collect_summary_*.json"))
    if summaries:
        results["collection"] = json.loads(summaries[-1].read_text(encoding="utf-8"))
    (out / f"backtest_{args.run_id}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print("저장:", out / f"backtest_{args.run_id}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
