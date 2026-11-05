"""3주차 보고서용 그래프 — 스크린샷 대신 제출합니다. `results/` 의 기록만 읽습니다(주문·API 호출 없음).

  python charts.py plot            가능한 그래프를 모두 results/charts/ 에 그린다
  python charts.py plot --only timeline,backtest,gate,smoke,live

그림은 results/charts/ 에 그리고, 같은 그림을 screenshots/NN-이름.png 로도 복사한다(스크린샷 자리에 그래프).

그림                          환경              근거 파일
rule_timeline.png             [규칙 설명]       strategy.json
backtest_overview.png         [백테스트]        results/bt-nh-cost40-slip0|1_{daily,trades}.csv
gate_distribution.png         [연구]            data/gate_history_seed.csv, results/bt-nh-cost40-slip0_daily.csv
smoke_orders.png              [모의계좌 주문 스모크]  results/smoke-20261104-a.jsonl
live_<run_id>.png             [자체 시뮬레이션]/[모의계좌 주문]  results/<run_id>.jsonl, results/<run_id>_bars.csv.gz  (본 실행 뒤에만 생성)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from datetime import datetime
from pathlib import Path

import chartkit as ck

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CHARTS = RESULTS / "charts"
SHOTS = HERE / "screenshots"          # 스크린샷 자리에도 그래프를 복사해 넣는다(NN-이름.png)
NUMBER = {"rule_timeline": 1, "backtest_overview": 2, "gate_distribution": 3, "smoke_orders": 4}   # live_<run_id> 는 10부터


def read_csv(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def day(text: str):
    return datetime.strptime(text, "%Y-%m-%d")


# ------------------------------------------------------------------ 1. 하루 흐름
def plot_timeline(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    cfg = json.loads((HERE / "strategy.json").read_text(encoding="utf-8"))
    p = cfg["params"]
    to_min = lambda t: int(t.split(":")[0]) * 60 + int(t.split(":")[1])
    fig, ax = plt.subplots(figsize=(13, 4.8))
    rows = [
        ("모의 운영 시간", 7 * 60, 24 * 60, ck.GRAY, "07:00~24:00"),
        ("게이트: 중국장 변동성 집계", to_min(p["gate"]["window_start"]), to_min(p["gate"]["window_end"]), ck.PURPLE, "10:00~15:55"),
        ("규칙 2: 고저 범위 기록", to_min(p["rule2"]["range_start"]), to_min(p["rule2"]["range_end"]), ck.BLUE, "08:00~16:00"),
        ("규칙 2: 돌파 진입 가능", to_min(p["rule2"]["entry_start"]), to_min(p["rule2"]["entry_end"]), ck.ORANGE, "16:00~22:00"),
        ("규칙 2: 보유(손절 또는 23:50 청산)", to_min(p["rule2"]["entry_start"]), to_min(p["rule2"]["exit_time"]), ck.ORANGE, "~23:50"),
        ("규칙 3 (겨울): 22:30 진입 → 23:45 청산", 22 * 60 + 30, 23 * 60 + 45, ck.GREEN, "뉴욕 08:30~09:45"),
        ("규칙 3 (서머타임): 21:30 진입 → 22:45 청산", 21 * 60 + 30, 22 * 60 + 45, ck.GREEN, "뉴욕 08:30~09:45"),
    ]
    for i, (name, a, b, color, label) in enumerate(rows):
        y = len(rows) - i
        alpha = 0.35 if i in (4,) else 0.85
        ax.barh(y, b - a, left=a, height=0.55, color=color, alpha=alpha)
        if b - a >= 150:     # 막대가 충분히 길면 안에, 짧으면 오른쪽 밖에 적는다
            ax.text(a + 40, y, label, ha="left", va="center", fontsize=8.5, color="white" if alpha > 0.5 else "black")
        else:
            ax.text(a - 8, y, label, ha="right", va="center", fontsize=8.5, color="black")
    ax.set_yticks([len(rows) - i for i in range(len(rows))])
    ax.set_yticklabels([r[0] for r in rows], fontsize=9)
    ticks = list(range(7 * 60, 24 * 60 + 1, 120))
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t // 60:02d}:00" for t in ticks])
    ax.axvline(16 * 60, color=ck.RED, lw=1.5, ls="--")
    ax.text(16 * 60 + 4, len(rows) + 0.62, "16:00 게이트 판정", color=ck.RED, fontsize=9)
    ax.axvline(23 * 60 + 55, color=ck.GRAY, lw=1, ls=":")
    ax.text(23 * 60 + 50, 0.35, "23:55 정리", color=ck.GRAY, fontsize=8, ha="right")
    ax.set_xlim(7 * 60 - 5, 24 * 60 + 5)
    ax.set_ylim(0.2, len(rows) + 1)
    ax.set_title("하루 흐름 (한국시간) — 게이트 통과일에만 진입, 규칙 2 가 있으면 규칙 3 은 쉼", loc="left", fontsize=11)
    ck.stamp(fig, "rule", ["strategy.json"], "규칙 3 은 뉴욕 시계 기준이라 서머타임에는 한국시간으로 한 시간 당겨집니다.")
    return ck.save(fig, out / "rule_timeline.png")


# ------------------------------------------------------------------ 2. 백테스트 개요
def plot_backtest(results: Path = RESULTS, out: Path = CHARTS, run: str = "bt-nh-cost40-slip0", run1: str = "bt-nh-cost40-slip1") -> Path:
    plt = ck.setup()
    daily = read_csv(results / f"{run}_daily.csv")
    trades = read_csv(results / f"{run}_trades.csv")
    d1 = read_csv(results / f"{run1}_daily.csv")
    xs = [day(r["day"]) for r in daily]
    pnl = [float(r["pnl"]) for r in daily]
    cum = ck.cumulative(pnl)
    dd = ck.drawdown(cum)
    cum1 = ck.cumulative([float(r["pnl"]) for r in d1])
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.2))
    ax = axes[0][0]
    ax.plot(xs, cum, color=ck.BLUE, lw=1.8, label=f"슬리피지 0틱: {ck.money(cum[-1])}")
    ax.plot(xs, cum1, color=ck.ORANGE, lw=1.4, ls="--", label=f"슬리피지 1틱: {ck.money(cum1[-1])}")
    ax.fill_between(xs, dd, 0, color=ck.RED, alpha=0.25, label=f"낙폭(0틱): 최대 {ck.money(min(dd))}")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("누적 순손익과 낙폭 (비용 $4.0 포함)", loc="left", fontsize=10.5)
    ax.legend(fontsize=8, loc="upper left")
    ax = axes[0][1]
    for rule, color in ((2, ck.BLUE), (3, ck.GREEN)):
        vals = [float(t["usd"]) for t in trades if int(t["rule"]) == rule]
        if vals:
            ax.hist(vals, bins=24, alpha=0.6, color=color, label=f"규칙 {rule}: {len(vals)}건, 평균 {ck.money(sum(vals) / len(vals))}")
    ax.axvline(0, color="black", lw=0.8)
    ax.set_title("거래 1건당 순손익 분포", loc="left", fontsize=10.5)
    ax.set_xlabel("달러")
    ax.legend(fontsize=8)
    ax = axes[1][0]
    months: dict[str, float] = {}
    for x, v in zip(xs, pnl):
        months[x.strftime("%Y-%m")] = months.get(x.strftime("%Y-%m"), 0.0) + v
    keys = sorted(months)
    ax.bar(keys, [months[k] for k in keys], color=[ck.BLUE if months[k] >= 0 else ck.RED for k in keys])
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("월별 순손익", loc="left", fontsize=10.5)
    ax.tick_params(axis="x", labelrotation=45)
    ax = axes[1][1]
    rv = [(day(r["day"]), float(r["rv"]), float(r["threshold"]) if r["threshold"] else None, r["gate_pass"] == "True") for r in daily if float(r["rv"]) > 0]
    ax.scatter([a for a, b, c, g in rv if g], [b for a, b, c, g in rv if g], s=9, color=ck.GREEN, label="통과")
    ax.scatter([a for a, b, c, g in rv if not g], [b for a, b, c, g in rv if not g], s=14, color=ck.RED, label="탈락")
    th = [(a, c) for a, b, c, g in rv if c]
    ax.plot([a for a, c in th], [c for a, c in th], color=ck.GRAY, lw=1.2, label="67 백분위 임계값")
    ax.set_yscale("log")
    ck.log_labels(ax)
    ax.set_title("중국장 변동성(RV)과 게이트 판정", loc="left", fontsize=10.5)
    ax.tick_params(axis="x", labelrotation=45)
    ax.legend(fontsize=8)
    passed = sum(1 for r in daily if r["gate_pass"] == "True")
    ck.stamp(fig, "backtest", [f"{run}_daily.csv", f"{run}_trades.csv", f"{run1}_daily.csv"],
             f"NH MGCZ26 {daily[0]['day']}~{daily[-1]['day']} ({len(daily)}일, 게이트 통과 {passed}일). 전략을 고른 기간과 겹치는 표본 내 결과입니다.")
    return ck.save(fig, out / "backtest_overview.png")


# ------------------------------------------------------------------ 3. 게이트 이력 분포
def plot_gate(results: Path = RESULTS, out: Path = CHARTS, seed: Path | None = None, run: str = "bt-nh-cost40-slip0") -> Path:
    plt = ck.setup()
    seed = seed or HERE / "data" / "gate_history_seed.csv"
    rows = read_csv(seed)
    vals = [(day(r["day"]), float(r["cn_rv"])) for r in rows if float(r["cn_rv"]) > 0]
    logs = [math.log10(v) for d, v in vals]
    srt = sorted(v for d, v in vals)
    thr = srt[int(0.67 * (len(srt) - 1))]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    ax = axes[0]
    ax.hist(logs, bins=50, color=ck.GRAY, alpha=0.7, label=f"2020~2026-10 전체 ({len(vals)}일)")
    recent = [math.log10(v) for d, v in vals if d.year == 2026]
    ax.hist(recent, bins=30, color=ck.BLUE, alpha=0.6, label=f"2026년 ({len(recent)}일)")
    ax.axvline(math.log10(thr), color=ck.RED, lw=1.8, label=f"67 백분위 {thr:.2e}")
    ax.set_xlabel("log10(중국장 RV)")
    ax.set_title("중국장 변동성 분포 — 2026 은 오른쪽으로 치우침", loc="left", fontsize=10.5)
    ax.legend(fontsize=8)
    ax = axes[1]
    by_year: dict[int, list[float]] = {}
    for d, v in vals:
        by_year.setdefault(d.year, []).append(v)
    years = sorted(by_year)
    ax.boxplot([by_year[y] for y in years], tick_labels=[str(y) for y in years], showfliers=False)
    ax.axhline(thr, color=ck.RED, lw=1.5, ls="--")
    ax.set_yscale("log")
    ck.log_labels(ax)
    ax.set_title("연도별 분포 (붉은 선 = 전체 67 백분위, 게이트 통과일 비율은 2026 에서 가장 높음)", loc="left", fontsize=10)
    share = {y: sum(1 for v in by_year[y] if v >= thr) / len(by_year[y]) for y in years}
    ax.set_xlabel("  ".join(f"{y}: {share[y]:.0%}" for y in years) + "  (전체 67 백분위 이상 비율)", fontsize=8)
    ck.stamp(fig, "research", ["data/gate_history_seed.csv"],
             "시드는 팀원 GC 1분봉에서 같은 정의로 계산한 일별 값입니다. 임계값은 시간이 지나며 늘어나는 이력으로 매일 다시 계산하며, 여기서는 전체 기간 값 하나만 표시했습니다.")
    return ck.save(fig, out / "gate_distribution.png")


# ------------------------------------------------------------------ 4. 스모크 주문 타임라인
def plot_smoke(results: Path = RESULTS, out: Path = CHARTS, run: str = "smoke-20261104-a") -> Path:
    plt = ck.setup()
    rows = read_jsonl(results / f"{run}.jsonl")
    steps = {r["text"]: r for r in rows if r.get("name") == "step"}
    t0 = datetime.fromisoformat(rows[0]["ts"])
    sec = lambda r: (datetime.fromisoformat(r["ts"]) - t0).total_seconds()
    pts = []
    def add(name, key, label, color, marker):
        r = steps.get(name)
        if r and r.get(key) not in (None, "None", ""):
            pts.append((sec(r), float(r[key]), label, color, marker))
    add("quote", "last", "시세(매수 1호가 부근)", ck.GRAY, "o")
    add("limit_place", "price", "지정가 매수 접수", ck.BLUE, "^")
    add("limit_modify", "price", "정정(새 주문번호)", ck.ORANGE, "s")
    add("market_buy_fill", "price", "시장가 매수 체결", ck.GREEN, "P")
    add("market_close_fill", "price", "시장가 매도(청산) 체결", ck.RED, "X")
    cancel = steps.get("limit_cancel")
    fig, ax = plt.subplots(figsize=(12.5, 5))
    for x, y, label, color, marker in pts:
        ax.scatter([x], [y], s=110, color=color, marker=marker, zorder=3)
        ax.annotate(f"{label}\n{y:,.1f}", (x, y), textcoords="offset points", xytext=(6, 10 if y > 4010 else -30), fontsize=8.5, color=color)
    if cancel:
        ax.axvline(sec(cancel), color=ck.GRAY, ls=":", lw=1)
        ax.text(sec(cancel), ax.get_ylim()[0], " 취소 접수", fontsize=8, color=ck.GRAY, va="bottom")
    ax.set_xlabel("시작 후 경과 초")
    ax.set_ylabel("가격(MGCZ26)")
    ax.set_ylim(3950, 4075)
    ax.set_xlim(-0.3, max(p[0] for p in pts) + 1.3)
    ax.set_title("주문 스모크 — 지정가 접수·정정·취소 후 시장가 1계약 왕복", loc="left", fontsize=11)
    nos = [f"{k}: {steps[k].get('order_no', '')}" for k in ("limit_place", "limit_modify", "limit_cancel", "market_buy", "market_close") if k in steps]
    ck.stamp(fig, "smoke", [f"{run}.jsonl"], "주문번호(" + ", ".join(nos) + ") · 체결은 주문내역(order-history)으로 확인한 값입니다.")
    return ck.save(fig, out / "smoke_orders.png")


# ------------------------------------------------------------------ 5. 본 실행(종이/모의) 하루 그림
def plot_live(run: str, results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    recs = read_jsonl(results / f"{run}.jsonl")
    bars = read_csv_bars(results / f"{run}_bars.csv.gz")
    mode = next((r["mode"] for r in recs if r.get("mode")), "paper-sim")
    fig, ax = plt.subplots(figsize=(13.5, 5.6))
    if bars:
        ax.plot([b[0] for b in bars], [b[1] for b in bars], color=ck.GRAY, lw=1.0, label="MGCZ26 1분 종가")
    day0 = bars[0][0].date() if bars else None
    if day0:
        for (a, b, label, color) in ((10, 15.9167, "중국장 집계", ck.PURPLE), (8, 16, "규칙 2 범위", ck.BLUE)):
            ax.axvspan(datetime.combine(day0, datetime.min.time()).replace(hour=int(a), minute=int((a % 1) * 60), tzinfo=bars[0][0].tzinfo),
                       datetime.combine(day0, datetime.min.time()).replace(hour=int(b), minute=int((b % 1) * 60), tzinfo=bars[0][0].tzinfo),
                       color=color, alpha=0.07, label=label)
    gate = next((r for r in recs if r.get("name") == "gate"), None)
    for r in recs:
        if r.get("kind") == "fill":
            t = datetime.fromisoformat(r["ts"])
            enter = r.get("action") == "enter"
            ax.scatter([datetime.fromisoformat(r["bar_ts"]) if r.get("bar_ts") else t], [float(r["price"])], s=90, zorder=4,
                       marker="^" if enter else "v", color=ck.GREEN if enter else ck.RED)
    for r in recs:
        if r.get("kind") == "event" and r.get("name") == "replay" and r.get("missed"):
            for m in r["missed"]:
                ax.scatter([datetime.fromisoformat(m["entry_ts"])], [m["entry"]], s=90, marker="x", color=ck.ORANGE, zorder=4)
                ax.annotate("놓친 신호(주문 없음)", (datetime.fromisoformat(m["entry_ts"]), m["entry"]), textcoords="offset points", xytext=(6, 10), fontsize=8, color=ck.ORANGE)
    trades = [r for r in recs if r.get("kind") == "trade"]
    title = f"{run} — 거래 {len(trades)}건, 순손익 {ck.money(sum(float(t['usd']) for t in trades))}"
    title += f" · 게이트 {'통과' if gate and gate.get('gate_pass') else '탈락' if gate else '미판정'}"
    ax.set_title(title, loc="left", fontsize=11)
    ax.legend(fontsize=8, loc="best")
    ax.set_ylabel("가격")
    import matplotlib.dates as mdates

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=bars[0][0].tzinfo if bars else None))
    ax.set_xlabel(f"시각(KST) {day0 or ''}")
    ck.stamp(fig, mode, [f"{run}.jsonl", f"{run}_bars.csv.gz"],
             "삼각형: 진입(녹색)·청산(적색) 체결, ×: 시작 전에 놓친 신호. 체결 출처는 기록의 fill.source 를 따릅니다.")
    return ck.save(fig, out / f"live_{run}.png")


def read_csv_bars(path: Path) -> list[tuple]:
    if not path.exists():
        return []
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        return [(datetime.fromisoformat(r["ts"]), float(r["c"])) for r in csv.DictReader(handle)]


ALL = {"timeline": plot_timeline, "backtest": plot_backtest, "gate": plot_gate, "smoke": plot_smoke}


def plot_all(only: set[str] | None = None, results: Path = RESULTS, out: Path = CHARTS, shots: Path | None = None) -> list[Path]:
    made = []
    for name, fn in ALL.items():
        if only and name not in only:
            continue
        try:
            made.append(fn(results=results, out=out))
        except FileNotFoundError as exc:
            print(f"건너뜀 {name}: {exc.filename} 없음")
    if not only or "live" in only:
        for path in sorted(results.glob("*_bars.csv.gz")):
            run = path.name[: -len("_bars.csv.gz")]
            if (results / f"{run}.jsonl").exists():
                made.append(plot_live(run, results=results, out=out))
    if shots is not None:
        live = sorted(p for p in made if p.stem.startswith("live_"))
        for p in made:
            n = NUMBER.get(p.stem) or 10 + live.index(p)
            ck.mirror(p, n, shots)
    return made


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="보고서용 그래프 (results/ 의 기록만 읽음)")
    p.add_argument("command", choices=["plot"])
    p.add_argument("--only", help="timeline,backtest,gate,smoke,live 중 쉼표로")
    a = p.parse_args(argv)
    for path in plot_all(set(a.only.split(",")) if a.only else None, shots=SHOTS):
        print("그림:", path.relative_to(HERE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
