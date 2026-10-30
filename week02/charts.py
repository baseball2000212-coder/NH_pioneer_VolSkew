"""보고서용 차트 생성 — results/ 의 기록만 읽는다 (주문·API 호출 없음).

  python charts.py ticks --logs ../logs      원본 틱 로그에서 1초 단위 최종가 CSV 를 results/ 에 만든다 (작업 폴더에서만)
  python charts.py plot                      results/ 만으로 results/charts/*.png 를 그린다 (제출 폴더에서 재현)

차트는 [백테스트] 또는 [모의계좌 실행 기록(주문 없이 쉰 날)] 을 그린 것이며, 화면 캡처(screenshots/)가 아니다.
"""
import argparse
import csv
import json
from datetime import datetime, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CHARTS = RESULTS / "charts"
STRATEGY = HERE / "strategy.json"
LIVE_RUNS = ["ord-1028", "ord-1029", "ord-1030"]

BLUE, ORANGE, RED, GRAY = "#2a6fbb", "#d9731a", "#c0392b", "#8a8f98"


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": "Malgun Gothic", "axes.unicode_minus": False,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.alpha": 0.25, "figure.dpi": 100, "savefig.dpi": 150,
    })
    return plt


def params():
    return json.loads(STRATEGY.read_text(encoding="utf-8"))["params"]


def read_signal(run):
    for line in (RESULTS / f"{run}.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") == "signal":
            return row
    raise ValueError(f"{run}: signal 줄이 없다")


def extract_ticks(logs_dir):
    """틱 로그 → 15:00~15:20 구간 1초 단위 마지막 체결가."""
    RESULTS.mkdir(exist_ok=True)
    for run in LIVE_RUNS:
        last = {}
        with open(Path(logs_dir) / f"ticks-{run}.jsonl", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row.get("api") != "KA":
                    continue
                ts = datetime.fromisoformat(row["ts"])
                if time(15, 0) <= ts.time() < time(15, 20):
                    last[ts.strftime("%H:%M:%S")] = row["price"]
        out = RESULTS / f"live_ticks_1s_{run}.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time", "last"])
            w.writerows(sorted(last.items()))
        print(f"{out.name}: {len(last)}초")


def _hms(text):
    h, m, s = (int(x) for x in text.split(":"))
    return h * 3600 + m * 60 + s


def plot_live():
    plt = _plt()
    thr = float(params()["threshold_pct"])
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4), sharey=False)
    for ax, run in zip(axes, LIVE_RUNS):
        sig = read_signal(run)
        prev, ret = float(sig["prev_close"]), float(sig["ret_pct"])
        xs, ys = [], []
        with open(RESULTS / f"live_ticks_1s_{run}.csv", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                xs.append(_hms(row["time"]) / 60 - 15 * 60)    # 15:00 기준 분
                ys.append(float(row["last"]))
        up, dn = prev * (1 + thr / 100), prev * (1 - thr / 100)
        ax.plot(xs, ys, color=BLUE, lw=1.4, label="체결가(1초)")
        ax.axhline(prev, color=GRAY, lw=1, ls="--", label=f"전일 종가 {prev:.2f}")
        ax.axhline(up, color=ORANGE, lw=1, label=f"±{thr:g}% 선")
        ax.axhline(dn, color=ORANGE, lw=1)
        lo, hi = min(min(ys), dn), max(max(ys), up)
        pad = (hi - lo) * 0.08
        ax.set_ylim(lo - pad, hi + pad)
        ax.axvspan(19 + 50 / 60, 20, color=RED, alpha=0.15)
        ax.scatter([19 + 50 / 60], [float(sig["price"])], color=RED, zorder=5)
        ax.annotate(f"15:19:50 판단\n{ret:+.2f}% → 쉼", (19 + 50 / 60, float(sig["price"])),
                    xytext=(-95, 28 if ret < 0 else -42), textcoords="offset points", fontsize=9,
                    arrowprops=dict(arrowstyle="->", color=RED), color=RED)
        ax.set_title(f"{run} · {sig['ts'][:10]}", fontsize=11)
        ax.set_xlim(0, 20)
        ax.set_xticks([0, 5, 10, 15, 20])
        ax.set_xticklabels(["15:00", "15:05", "15:10", "15:15", "15:20"])
        if run == LIVE_RUNS[0]:
            ax.legend(loc="best", fontsize=8, frameon=False)
    fig.suptitle("[모의계좌 실행 기록] 본 실행 3일 — 세 날 모두 |등락률| ≥ 3% 라 규칙대로 진입하지 않음 (출처: results/ord-*.jsonl, live_ticks_1s_*.csv)",
                 fontsize=11, y=1.02)
    fig.tight_layout()
    out = CHARTS / "live_skipdays.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_backtest():
    plt = _plt()
    res = json.loads((RESULTS / "backtest_result.json").read_text(encoding="utf-8"))
    train_end = res["assumptions"]["train_end"]
    with open(RESULTS / "backtest_trades.csv", encoding="utf-8") as f:
        trades = list(csv.DictReader(f))
    net = [float(t["net_pt"]) for t in trades]
    cum, peak, dd, run = [], 0.0, [], 0.0
    for v in net:
        run += v
        peak = max(peak, run)
        cum.append(run)
        dd.append(run - peak)
    idx = list(range(1, len(net) + 1))
    split = sum(1 for t in trades if t["date"] <= train_end)
    stops = [i for i, t in enumerate(trades, 1) if t["exit_reason"] == "stop"]

    fig, ax = plt.subplots(2, 2, figsize=(13, 8.2))
    a = ax[0][0]
    a.plot(idx, cum, color=BLUE, lw=1.8)
    a.fill_between(idx, dd, 0, color=RED, alpha=0.18, label="고점 대비 낙폭")
    a.axvline(split + 0.5, color=GRAY, ls="--", lw=1)
    a.text(split + 1.5, max(cum) * 0.93, "← 학습 | 검증 →", fontsize=8, color=GRAY, ha="center")
    a.scatter(stops, [cum[i - 1] for i in stops], color=RED, zorder=5, s=28, label="10pt 손절")
    a.set_title(f"누적 순손익(pt) — 거래 {len(net)}건, 합계 {cum[-1]:.1f}pt, 최대낙폭 {-min(dd):.1f}pt")
    a.set_xlabel("거래 순번")
    a.legend(frameon=False, fontsize=8, loc="center left")

    b = ax[0][1]
    lo, hi = int(min(net)) - 1, int(max(net)) + 2
    b.hist(net, bins=range(lo, hi + 1, 1), color=BLUE, edgecolor="white")
    b.axvline(0, color=GRAY, lw=1)
    m = res["main"]
    b.axvline(m["mean_pt"], color=ORANGE, lw=1.6, label=f"평균 {m['mean_pt']:+.2f}pt")
    b.set_title(f"거래당 순손익 분포 — 승률 {m['win_rate'] * 100:.1f}%, 손익비(PF) {m['profit_factor']:.2f}")
    b.set_xlabel("pt (비용 0.5pt 반영)")
    b.legend(frameon=False, fontsize=9)

    c = ax[1][0]
    keys = list(res["by_threshold_pct"])
    vals = [res["by_threshold_pct"][k]["mean_pt"] for k in keys]
    labels = ["제한없음" if float(k) >= 100 else f"{float(k):g}%" for k in keys]
    bars = c.bar(labels, vals, color=[ORANGE if k == "3.0" else BLUE for k in keys])
    for r, k in zip(bars, keys):
        c.text(r.get_x() + r.get_width() / 2, r.get_height() + 0.04,
               f"n={res['by_threshold_pct'][k]['n']}", ha="center", fontsize=8, color=GRAY)
    c.set_title("임계값별 평균 순손익(pt) — 3%(주황)가 채택값")
    c.set_xlabel("진입 허용 상한 |등락률|")

    d = ax[1][1]
    keys = list(res["by_exit_bar"])
    vals = [res["by_exit_bar"][k]["mean_pt"] for k in keys]
    bars = d.bar([f"{k[:2]}:{k[2:]}" for k in keys], vals, color=[ORANGE if k == "1534" else BLUE for k in keys])
    for r, v in zip(bars, vals):
        d.text(r.get_x() + r.get_width() / 2, v + 0.04, f"{v:+.2f}", ha="center", fontsize=9)
    d.set_title("청산 시각별 평균 순손익(pt) — 15:34 봉 종가(주황)로 운용")
    d.set_xlabel("청산에 쓴 분봉")

    fig.suptitle("[백테스트] 코스닥150 선물 장 막판 모멘텀(3% 규칙) — 외부 1분봉 243일, 비용 0.5pt, 손절 10pt (출처: results/backtest_result.json, backtest_trades.csv)",
                 fontsize=11, y=1.0)
    fig.tight_layout()
    out = CHARTS / "backtest_overview.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_timeline():
    plt = _plt()
    p = params()
    marks = [
        (p["decision_time"], "판단\n(3% 규칙)", BLUE),
        (p["entry_time"], "진입\n(시장가 1계약)", BLUE),
        (p["exit_submit_time_continuous"], "청산\n(시장가)", ORANGE),
        (p["stop_until"], "손절 감시 끝\n이후 주문 금지", RED),
    ]
    t0, t1 = _hms("15:19:00"), _hms("15:35:30")
    fig, ax = plt.subplots(figsize=(13, 2.9))
    ax.hlines(0, 0, (t1 - t0) / 60, color=GRAY, lw=3)
    ax.axvspan((_hms(p["entry_time"]) - t0) / 60, (_hms(p["stop_until"]) - t0) / 60, ymin=0.38, ymax=0.62,
               color=ORANGE, alpha=0.18)
    ax.text((_hms("15:27:00") - t0) / 60, 0.2, f"보유 구간 — {float(p['stop_pt']):g}pt 불리하면 즉시 손절", ha="center", fontsize=9, color=ORANGE)
    for i, (hms, label, color) in enumerate(marks):
        x = (_hms(hms) - t0) / 60
        up = 1 if i % 2 == 0 else -1
        ax.vlines(x, 0, 0.55 * up, color=color, lw=1.4)
        ax.scatter([x], [0], color=color, zorder=5)
        ax.text(x, 0.62 * up, f"{hms}\n{label}", ha="center", va="bottom" if up > 0 else "top", fontsize=9, color=color)
    ax.set_ylim(-1.5, 1.5)
    ax.set_xlim(-0.8, (t1 - t0) / 60 + 0.5)
    ax.set_yticks([])
    ax.grid(False)
    ax.spines["left"].set_visible(False)
    ticks = [0, 5, 10, 15]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"15:{19 + t:02d}" for t in ticks])
    ax.set_title("하루 흐름 (strategy.json 의 시각을 그대로 사용)", fontsize=11)
    fig.tight_layout()
    out = CHARTS / "rule_timeline.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("ticks")
    t.add_argument("--logs", required=True)
    sub.add_parser("plot")
    args = ap.parse_args(argv)
    if args.cmd == "ticks":
        extract_ticks(args.logs)
        return
    CHARTS.mkdir(parents=True, exist_ok=True)
    for fn in (plot_timeline, plot_backtest, plot_live):
        print(fn())


if __name__ == "__main__":
    main()
