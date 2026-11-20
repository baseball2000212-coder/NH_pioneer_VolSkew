"""5주차 보고서용 그래프 — 스크린샷 대신 제출합니다. `results/` 의 기록만 읽습니다(주문·API 호출 없음).

  python charts.py plot                      백테스트 그래프를 모두 results/charts/ 에 그리고 screenshots/NN-이름.png 로 복사
  python charts.py plot --only t2_chart
  python charts.py plot --run sim-1118       본 실행(run_id)의 그래프 run_<run_id>.png 를 그린다 (그 실행 뒤에만 가능)

그림                    환경          근거 파일                                              보여 주는 것
01-data_quality         [백테스트]    backtest_*.json, bars_MESZ26_5m.csv.gz                일별 5분봉 수(월물 유동성 변화)와 IS/OOS 구간, 수집 요약
02-t2_chart             [백테스트]    bars_MESZ26_5m.csv.gz, backtest_*.json                Hotelling T² 관리도(OOS)와 관리 상한·이탈 점
03-candidates           [백테스트]    backtest_*.json                                       사전 등록 후보 8개의 CV 점수와 OOS 거래당 순손익(CI)
04-oos_equity           [백테스트]    trades_backtest_MESZ26_oos.csv, backtest_*.json       선택 후보 OOS 누적 순손익·낙폭, 총이익-비용-순손익 분해
05-gate_effect          [백테스트]    backtest_*.json                                       T² 안/밖 신호의 성과, 기준선(z 평균회귀) 비교
06-cross_assets         [백테스트]    backtest_*.json                                       MES·MNQ·MGC·6E 같은 절차의 OOS 거래당 순손익
07-svm_confusion        [백테스트]    backtest_*.json                                       SVM 3분류 혼동행렬과 계수
run_<run_id>.png        [자체 시뮬레이션]/[모의계좌 주문]  <run_id>.jsonl, live_bars_<run_id>.csv  본 실행 하루 가격·진입·청산·T² (그 실행 뒤에만 생성, 번호 10~)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import chartkit as ck

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CHARTS = RESULTS / "charts"
SHOTS = HERE / "screenshots"
NUMBER = {"data_quality": 1, "t2_chart": 2, "candidates": 3, "oos_equity": 4, "gate_effect": 5, "cross_assets": 6, "svm_confusion": 7}
SYM = "MESZ26"


def latest_backtest(results: Path = RESULTS) -> tuple[dict, str]:
    path = sorted(results.glob("backtest_*.json"))[-1]
    return json.loads(path.read_text(encoding="utf-8")), path.name


def read_bars(results: Path = RESULTS) -> pd.DataFrame:
    return pd.read_csv(results / f"bars_{SYM}_5m.csv.gz", index_col=0, parse_dates=True)


def err(ci: list[float], mean: float) -> list[float]:
    return [max(mean - ci[0], 0), max(ci[1] - mean, 0)]


def plot_data_quality(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    r, name = latest_backtest(results)
    bars = read_bars(results)
    per_day = bars.groupby(bars.index.normalize()).size()
    sp = r["assumptions"]["split"]
    q = r["data"]["quality"]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), gridspec_kw={"height_ratios": [2.2, 1]})
    a1.bar(per_day.index, per_day.values, color=ck.BLUE, width=0.9)
    a1.axvspan(pd.Timestamp(sp["is_start"]), pd.Timestamp(sp["is_end"]) + pd.Timedelta(days=1), color=ck.GREEN, alpha=0.12, label="in-sample")
    a1.axvspan(pd.Timestamp(sp["oos_start"]), pd.Timestamp(sp["oos_end"]) + pd.Timedelta(days=1), color=ck.ORANGE, alpha=0.12, label="out-of-sample")
    a1.set_ylabel("하루 5분봉 수(완성된 봉)")
    a1.set_title(f"{SYM} 5분봉 수집 결과: 9월 이후에만 유동적 (이전은 후행월물이라 성김)", loc="left")
    a1.legend(loc="upper left")
    coll = r.get("collection", {}).get("symbols", {})
    labels, pages, rows = [], [], []
    for s, v in coll.items():
        if "error" in v:
            continue
        labels.append(s)
        pages.append(v["pages"])
        rows.append(v["rows"] / 1000)
    a2.bar(labels, rows, color=ck.PURPLE)
    for i, (p, k) in enumerate(zip(pages, rows)):
        a2.annotate(f"{p}쪽 · 중복 {coll[labels[i]]['duplicate_rows']}", (i, k), ha="center", va="bottom", fontsize=8)
    a2.set_ylabel("1분봉(천 개)")
    a2.set_title(f"차트 API 나눠 받기 결과 (쪽당 {min(v['min_rows_per_page'] for v in coll.values() if 'error' not in v)}~"
                 f"{max(v['max_rows_per_page'] for v in coll.values() if 'error' not in v)}줄, 8/1~)", loc="left")
    ck.stamp(fig, "backtest", [name, f"bars_{SYM}_5m.csv.gz"], note=f"1분봉 간격 정확히 1분 {q['one_minute_gap_ratio']:.1%}, 2~5분 공백 {q['gaps_2_to_5_min_ratio']:.1%}, 5분 초과 공백 {q['gaps_over_5_min']}건(휴장·주말 포함).")
    return ck.save(fig, out / "data_quality.png")


def plot_t2_chart(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    r, name = latest_backtest(results)
    bars = read_bars(results)
    ucl = r["selected_detail"]["t2_ucl"]
    sp = r["assumptions"]["split"]
    oos = bars[(bars.index >= pd.Timestamp(sp["oos_start"])) & bars.t2.notna()]
    ratio = r["selected_detail"]["t2_out_of_control_ratio"]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), gridspec_kw={"height_ratios": [2, 1]})
    a1.plot(oos.index, oos.t2, color=ck.BLUE, lw=0.7)
    out_pts = oos[oos.t2 > ucl]
    a1.scatter(out_pts.index, out_pts.t2, color=ck.RED, s=8, label="관리 상한 초과")
    a1.axhline(ucl, color=ck.ORANGE, ls="--", lw=1.2, label=f"UCL = IS T² 95% 분위수 ({ucl:.1f})")
    a1.set_yscale("log")
    ck.log_labels(a1)
    a1.set_ylabel("T² (로그 축)")
    a1.set_title("Hotelling T² 관리도 — out-of-sample 구간", loc="left")
    a1.legend(loc="upper left")
    a2.hist([bars[bars.index < pd.Timestamp(sp["oos_start"])].t2.dropna().clip(upper=ucl * 4),
             oos.t2.clip(upper=ucl * 4)], bins=60, color=[ck.GREEN, ck.ORANGE], label=["in-sample", "out-of-sample"], density=True)
    a2.axvline(ucl, color=ck.RED, ls="--")
    a2.set_xlabel(f"T² (UCL×4 에서 절단) · 관리 이탈 비율 IS {ratio['is']:.1%} → OOS {ratio['oos']:.1%}")
    a2.legend()
    ck.stamp(fig, "backtest", [f"bars_{SYM}_5m.csv.gz", name], note="OOS 에서 이탈 비율이 늘어난 것은 평소 상태의 분포가 이동했다는 뜻(분포 변화).")
    return ck.save(fig, out / "t2_chart.png")


def plot_candidates(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    r, name = latest_backtest(results)
    cands = r["candidates"]
    ids = [c["id"] for c in cands]
    cv = [c["cv"]["score"] for c in cands]
    oos = [c["oos_live_opposite"].get("net_mean_usd", 0) for c in cands]
    errs = [err(c["oos_live_opposite"]["net_mean_ci95_usd"], c["oos_live_opposite"]["net_mean_usd"]) for c in cands]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5), sharey=True)
    colors = [ck.RED if i == r["selected"] else ck.BLUE for i in ids]
    a1.barh(ids, cv, color=colors)
    a1.set_title("in-sample CV: 거래당 순손익($) — 선택 기준", loc="left")
    a2.barh(ids, oos, xerr=[[e[0] for e in errs], [e[1] for e in errs]], color=colors, capsize=3)
    a2.set_title("out-of-sample: 거래당 순손익($) ±95% CI — 보고용", loc="left")
    for a in (a1, a2):
        a.axvline(0, color=ck.GRAY, lw=1)
        a.invert_yaxis()
    ck.stamp(fig, "backtest", [name], note=f"빨강 = CV 로 선택한 후보 ({r['selected']}). 거래 창 21:30~23:40, 체결 모델 opposite(비관). 선택은 OOS 를 보기 전에 했다.")
    return ck.save(fig, out / "candidates.png")


def plot_oos_equity(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    r, name = latest_backtest(results)
    tr = pd.read_csv(results / f"trades_backtest_{SYM}_oos.csv", parse_dates=["entry_ts", "exit_ts"])
    cum = ck.cumulative(tr.net_usd.tolist())
    st = r["selected_detail"]["oos_trades"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5), gridspec_kw={"width_ratios": [2.3, 1]})
    a1.plot(tr.exit_ts, cum, color=ck.BLUE, lw=1.4, label="누적 순손익($)")
    a1.fill_between(tr.exit_ts, ck.drawdown(cum), 0, color=ck.RED, alpha=0.25, label="낙폭")
    a1.set_title(f"선택 후보 {r['selected']} OOS 누적 순손익 (n={st['n']})", loc="left")
    a1.legend(loc="lower left")
    gross, net = st["gross_mean_usd"], st["net_mean_usd"]
    a2.bar(["총이익", "비용", "순손익"], [gross, net - gross, net], color=[ck.GREEN, ck.GRAY, ck.RED if net < 0 else ck.GREEN])
    a2.axhline(0, color="black", lw=0.8)
    for i, v in enumerate([gross, net - gross, net]):
        a2.annotate(f"{v:+.2f}", (i, v), ha="center", va="bottom" if v >= 0 else "top", fontsize=9)
    a2.set_title("거래당 평균($): 총이익 - 비용 = 순", loc="left")
    ck.stamp(fig, "backtest", [f"trades_backtest_{SYM}_oos.csv", name], note=f"승률 {st['win_rate']:.0%}, 손익비 {st['payoff']}, 최대 낙폭 {st['max_drawdown_usd']:.0f}달러. 수수료 4달러(왕복)·호가 폭 1틱×2 가정.")
    return ck.save(fig, out / "oos_equity.png")


def plot_gate_effect(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    r, name = latest_backtest(results)
    g = r["selected_detail"]["gate_effect_oos_ungated_trades"]
    base = r["baseline_z_mr"]["live_opposite"]["oos"]
    sel = r["selected_detail"]["oos_trades"]
    rows = [("SVM · T² 안(게이트 통과)", g["in_control"]), ("SVM · T² 밖(게이트가 막은 것)", g["out_of_control"]),
            ("선택 후보(게이트 적용)", sel), ("기준선 z-score 평균회귀", base)]
    rows = [(n, s) for n, s in rows if s.get("n")]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    means = [s["net_mean_usd"] for _, s in rows]
    errs = [err(s["net_mean_ci95_usd"], s["net_mean_usd"]) for _, s in rows]
    ax.barh([f"{n}\n(n={s['n']})" for n, s in rows], means, xerr=[[e[0] for e in errs], [e[1] for e in errs]],
            color=[ck.GREEN, ck.ORANGE, ck.BLUE, ck.GRAY][: len(rows)], capsize=3)
    ax.axvline(0, color="black", lw=0.8)
    ax.invert_yaxis()
    ax.set_title("OOS 거래당 순손익($) ±95% CI — 게이트는 가설과 반대로 좋은 쪽을 막았다(표본 작아 유의하지 않음)", loc="left", fontsize=10)
    ck.stamp(fig, "backtest", [name], note="T² 안/밖은 같은 SVM 신호(게이트 없음)를 T² 상태로 나눈 것. 선택은 사전 등록 규칙대로 유지하고 이 결과는 향후 과제로만 둔다.")
    return ck.save(fig, out / "gate_effect.png")


def plot_cross_assets(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    r, name = latest_backtest(results)
    items = [(SYM, r["selected_detail"]["oos_trades"])] + [(s, v["oos_live_opposite"]) for s, v in r["cross_assets"].items()]
    items = [(s, v) for s, v in items if v.get("n")]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    means = [v["net_mean_usd"] for _, v in items]
    errs = [err(v["net_mean_ci95_usd"], v["net_mean_usd"]) for _, v in items]
    ax.bar([f"{s}\n(n={v['n']})" for s, v in items], means, yerr=[[e[0] for e in errs], [e[1] for e in errs]], color=ck.BLUE, capsize=3)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("같은 절차(각자 IS 로 학습·후보 재선택 없음) — OOS 거래당 순손익($)", loc="left")
    ck.stamp(fig, "backtest", [name], note="MNQ·MGC·6E 수수료는 MES 와 같다고 가정(미확인), 호가 폭은 1회 관측값. 비교용이며 주문은 MES 에서만 낸다.")
    return ck.save(fig, out / "cross_assets.png")


def plot_svm_confusion(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    import numpy as np
    plt = ck.setup()
    r, name = latest_backtest(results)
    d = r["selected_detail"]
    c = d["classification_oos_live_window"]
    m = np.array([[c["confusion"][f"true{t}"][f"pred{p}"] for p in (-1, 0, 1)] for t in (-1, 0, 1)])
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.8), gridspec_kw={"width_ratios": [1, 1.5]})
    a1.imshow(m / m.sum(axis=1, keepdims=True), cmap="Blues", vmin=0, vmax=0.6)
    labs = ["숏", "관망", "롱"]
    a1.set_xticks(range(3), labs)
    a1.set_yticks(range(3), labs)
    a1.grid(False)
    for i in range(3):
        for j in range(3):
            a1.text(j, i, f"{m[i, j]}\n({m[i, j] / m[i].sum():.0%})", ha="center", va="center", fontsize=9)
    a1.set_xlabel("예측")
    a1.set_ylabel("실제 라벨")
    a1.set_title(f"혼동행렬 OOS: 정확도 {c['accuracy']:.1%} (최빈 클래스 {c['majority_baseline']:.1%})", loc="left", fontsize=10)
    feats = list(next(iter(d["svm_coef"].values())).keys())
    width = 0.27
    for k, (cls, coef) in enumerate(sorted(d["svm_coef"].items(), key=lambda x: int(x[0]))):
        a2.bar(np.arange(len(feats)) + (k - 1) * width, [coef[f] for f in feats], width, label={"-1": "숏", "0": "관망", "1": "롱"}[cls])
    a2.set_xticks(range(len(feats)), feats)
    a2.axhline(0, color="black", lw=0.8)
    a2.legend()
    a2.set_title("선형 SVM 계수(표준화 입력, 클래스별 one-vs-rest)", loc="left", fontsize=10)
    ck.stamp(fig, "backtest", [name], note="정확도가 우연(최빈 클래스)보다 약간 높은 정도 → 비용을 넘는 엣지가 없다는 §7 결론과 일치.")
    return ck.save(fig, out / "svm_confusion.png")


PLOTS = {"data_quality": plot_data_quality, "t2_chart": plot_t2_chart, "candidates": plot_candidates, "oos_equity": plot_oos_equity,
         "gate_effect": plot_gate_effect, "cross_assets": plot_cross_assets, "svm_confusion": plot_svm_confusion}


def plot_live(run_id: str, number: int = 10, results: Path = RESULTS, out: Path = CHARTS) -> list[Path]:
    """본 실행(run_id) 그림: 5분봉 종가·진입/청산 표시와 T². 기록은 results/<run_id>.jsonl, results/live_bars_<run_id>.csv."""
    plt = ck.setup()
    rows = [json.loads(line) for line in (results / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    mode = rows[0]["mode"]
    bars = pd.read_csv(results / f"live_bars_{run_id}.csv", parse_dates=["bar"])
    fills = [r for r in rows if r["kind"] == "fill"]
    trades = [r for r in rows if r["kind"] == "trade"]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [2.2, 1]})
    a1.plot(bars.bar, bars.close, color=ck.BLUE, lw=1.2, label="5분봉 종가")
    for f in fills:
        t = pd.Timestamp(f["ts"]).tz_localize(None) if pd.Timestamp(f["ts"]).tzinfo is None else pd.Timestamp(f["ts"]).tz_convert("Asia/Seoul").tz_localize(None)
        a1.scatter([t], [f["px"]], color=ck.GREEN if f.get("role") == "entry" else ck.RED, s=40, zorder=5)
    a1.set_title(f"{run_id}: 가격과 체결 (초록=진입, 빨강=청산) · 거래 {len(trades)}건, 순손익 ${sum(t.get('net_usd', 0) for t in trades):.2f}", loc="left")
    a2.plot(bars.bar, bars.t2, color=ck.PURPLE, lw=0.9)
    a2.set_yscale("log")
    ck.log_labels(a2)
    a2.set_ylabel("T²")
    ck.stamp(fig, mode, [f"{run_id}.jsonl", f"live_bars_{run_id}.csv"], note="체결 출처: " + ("mock-account(모의계좌 주문내역)" if mode == "paper-order" else "paper-sim(자체 가상 체결)"))
    path = ck.save(fig, out / f"run_{run_id}.png")
    return [path, ck.mirror(path, number, SHOTS)]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="보고서용 그래프 (results/ 의 기록만 읽음)")
    p.add_argument("command", choices=["plot"])
    p.add_argument("--only", help="쉼표로 구분한 그림 이름")
    p.add_argument("--run", help="본 실행 run_id (results/<run_id>.jsonl 필요)")
    p.add_argument("--number", type=int, default=10, help="--run 그림의 번호(기본 10)")
    a = p.parse_args(argv)
    if a.run:
        for path in plot_live(a.run, number=a.number):
            print("그림:", path.relative_to(HERE))
        return 0
    for name in (a.only.split(",") if a.only else list(PLOTS)):
        try:
            path = PLOTS[name]()
            print("그림:", path.relative_to(HERE), "→", ck.mirror(path, NUMBER[name], SHOTS).relative_to(HERE))
        except FileNotFoundError as exc:
            print(f"건너뜀 {name}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
