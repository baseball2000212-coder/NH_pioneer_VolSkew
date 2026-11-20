"""라이브-리플레이 신호 일치 검사: 실행 중 기록한 5분봉 신호(`live_bars_<run_id>.csv`)를, 실행 뒤에 다시 받은 1분봉으로
같은 `signals.py` 로 다시 계산한 값과 비교한다. 읽기만 한다(주문·API 호출 없음).

  python collect_chart.py --syms MESZ26 --start 20261117 --out-dir ../data --run-id recheck-1118     (실행 뒤 재수집)
  python replay_check.py --run-id sim-1118 --minute ../data/minute_MESZ26.csv.gz --out-dir results

일치 판정: 같은 봉에서 `side`(최종 신호)가 같고, 특징·T² 값이 허용 오차(기본 1e-3) 안.
실행 중 받은 최근 봉은 "아직 안 끝난 분" 때문에 마지막 분봉 하나가 늦게 확정될 수 있다 → 불일치 봉은 이유 후보와 함께 목록으로 남긴다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bars import load_minute, to_bars  # noqa: E402
from signals import FEATURES, SignalModel, compute_features, decide  # noqa: E402


def compare(live: pd.DataFrame, minute: pd.DataFrame, model: SignalModel, cfg: dict, gate: bool, tol: float = 1e-3) -> dict:
    bars = to_bars(minute, cfg["bar_minutes"], cfg["min_sub"])
    feats = compute_features(bars, cfg)
    sig = decide(model, feats, cfg, use_gate=gate)
    rows, diffs = [], []
    for _, r in live.iterrows():
        t = pd.Timestamp(r["bar"])
        if t not in feats.index or not bool(feats.loc[t, "ok"]):
            rows.append({"bar": str(t), "status": "missing_in_replay"})
            continue
        fd = max(abs(float(r[k]) - float(feats.loc[t, k])) for k in FEATURES if pd.notna(r[k]))
        t2d = abs(float(r["t2"]) - float(sig.loc[t, "t2"])) if pd.notna(r["t2"]) else 0.0
        same_side = int(r["side"]) == int(sig.loc[t, "side"])
        diffs.append(max(fd, t2d))
        rows.append({"bar": str(t), "status": "match" if same_side and max(fd, t2d) <= tol else "mismatch",
                     "live_side": int(r["side"]), "replay_side": int(sig.loc[t, "side"]), "max_abs_diff": round(max(fd, t2d), 6)})
    n = len(rows)
    match = sum(1 for x in rows if x["status"] == "match")
    return {"live_bars": n, "match": match, "match_ratio": round(match / n, 4) if n else None,
            "side_agree": sum(1 for x in rows if x.get("live_side") == x.get("replay_side") and "live_side" in x),
            "missing_in_replay": sum(1 for x in rows if x["status"] == "missing_in_replay"),
            "max_abs_diff": round(max(diffs), 6) if diffs else None, "tolerance": tol,
            "mismatches": [x for x in rows if x["status"] != "match"][:30]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="라이브-리플레이 신호 일치 검사")
    p.add_argument("--run-id", required=True)
    p.add_argument("--minute", required=True, help="실행 뒤 다시 받은 1분봉 CSV.gz")
    p.add_argument("--out-dir", default=str(HERE / "results"))
    p.add_argument("--config", default=str(HERE / "strategy.json"))
    p.add_argument("--model", default=str(HERE / "model_MESZ26.json"))
    p.add_argument("--tol", type=float, default=1e-3)
    a = p.parse_args(argv)
    out = Path(a.out_dir)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    model = SignalModel.from_json(a.model)
    live = pd.read_csv(out / f"live_bars_{a.run_id}.csv")
    live = live[live["ok"] == True]  # noqa: E712
    res = compare(live, load_minute(a.minute), model, cfg, bool(model.meta["gate"]), a.tol)
    res["run_id"] = a.run_id
    (out / f"replay_check_{a.run_id}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"일치 {res['match']}/{res['live_bars']} ({res['match_ratio']}), 최대 차이 {res['max_abs_diff']}, 리플레이에 없음 {res['missing_in_replay']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
