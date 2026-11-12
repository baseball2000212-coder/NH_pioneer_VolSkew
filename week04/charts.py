"""4주차 보고서용 그래프 — 스크린샷 대신 제출합니다. `results/` 의 기록만 읽습니다(주문·API 호출 없음).

  python charts.py plot                         가능한 그래프를 모두 results/charts/ 에 그린다
  python charts.py plot --only hedge_timeline,pnl_attribution
  python charts.py plot --run ord-0113          본 실행(run_id)의 그래프 run_<run_id>.png 를 그린다 (그 실행 뒤에만 가능)

그림은 results/charts/ 에 그리고, 같은 그림을 screenshots/NN-이름.png 로도 복사한다(번호는 그림마다 고정).

그림                    환경             근거 파일                                        보여 주는 것
hedge_timeline.png      [백테스트]       results/bt-1110-2229-l0.jsonl                    ES 경로·MES 보유·델타 어긋남·헤지 시점 (11/10 CPI 밤)
pnl_attribution.png     [백테스트]       results/bt-1110-2229-l0.jsonl                    손익을 감마·세타·베가·델타로 분해한 누적 곡선 (베가 손실)
hedge_rule_compare.png  [백테스트]       results/bt-1110-2229-*_summary.json              헤지 규칙·체결 지연별 순손익 (규칙 간 우열은 잡음)
parity_spread.png       [백테스트]       results/bt-1110-2229-l0.jsonl                    풋-콜 패리티 잔차(콜-풋+K-MES) 분포와 거래비용 폭
run_<run_id>.png        [자체 시뮬레이션]/[모의계좌 주문]  results/<run_id>.jsonl          본 실행 하루의 같은 그림 (그 실행 뒤에만 생성)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import chartkit as ck

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CHARTS = RESULTS / "charts"
SHOTS = HERE / "screenshots"
NUMBER = {"hedge_timeline": 1, "pnl_attribution": 2, "hedge_rule_compare": 3, "parity_spread": 4}   # run_<run_id> 는 10부터
BASE_RUN = "bt-1110-2229-l0"


def read_jsonl(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_run(run_id: str, results: Path = RESULTS) -> dict:
    rows = read_jsonl(results / f"{run_id}.jsonl")
    ts = lambda r: datetime.fromisoformat(r["ts"]).replace(tzinfo=None)
    return {
        "rows": rows,
        "mode": rows[0]["mode"],
        "states": [r for r in rows if r["kind"] == "event" and r.get("name") == "state"],
        "hedges": [r for r in rows if r["kind"] == "signal" and r.get("reason") == "delta_mismatch"],
        "entry": next((r for r in rows if r["kind"] == "signal" and r.get("reason") == "enter_straddle"), None),
        "fills": [r for r in rows if r["kind"] == "fill"],
        "summary": next((r for r in rows if r["kind"] == "run" and r.get("phase") == "summary"), None),
        "ts": ts,
    }


# ------------------------------------------------------------------ 1. 헤지 타임라인
def plot_hedge_timeline(results: Path = RESULTS, out: Path = CHARTS, run_id: str = BASE_RUN, name: str = "hedge_timeline") -> Path:
    plt = ck.setup()
    d = load_run(run_id, results)
    t = [d["ts"](r) for r in d["states"]]
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(11, 7.5), sharex=True, gridspec_kw={"height_ratios": [2.2, 1.2, 1.2]})
    a1.plot(t, [r["F"] for r in d["states"]], color=ck.BLUE, lw=1.4, label="ES 호가 가운데 값")
    if d["entry"]:
        a1.axvline(d["ts"](d["entry"]), color=ck.GRAY, ls="--", lw=1)
        a1.annotate(f"진입 K={d['entry']['strike']:.0f} (콜+풋 매수)", (d["ts"](d["entry"]), d["states"][0]["F"]), fontsize=8, color=ck.GRAY, xytext=(4, 6), textcoords="offset points")
    for h in d["hedges"]:
        a1.axvline(d["ts"](h), color=ck.ORANGE, lw=1)
        a1.annotate(f"MES {'+' if h['order'] > 0 else ''}{h['order']}", (d["ts"](h), max(r["F"] for r in d["states"])), fontsize=8, color=ck.ORANGE, xytext=(3, -10), textcoords="offset points")
    a1.set_ylabel("ES (pt)")
    a1.set_title(f"ES 경로와 헤지 시점 · run_id {run_id}", loc="left")
    a2.step(t, [r["mes"] for r in d["states"]], where="post", color=ck.ORANGE)
    a2.set_ylabel("MES 보유(계약)")
    a3.plot(t, [r["mismatch"] for r in d["states"]], color=ck.PURPLE, lw=1.1)
    a3.axhline(1, color=ck.GRAY, ls=":", lw=1)
    a3.axhline(-1, color=ck.GRAY, ls=":", lw=1)
    a3.set_ylabel("델타 어긋남(MES계약)")
    a3.set_xlabel("시각 (KST, 시뮬레이션 시계)" if d["mode"] == "backtest" else "시각 (KST)")
    ck.stamp(fig, d["mode"], [f"{run_id}.jsonl"], note="점선: 헤지 band(±1계약). 어긋남이 band 안에서는 주문하지 않는다.")
    path = ck.save(fig, out / f"{name}.png")
    return path


# ------------------------------------------------------------------ 2. 손익 분해
def plot_pnl_attribution(results: Path = RESULTS, out: Path = CHARTS, run_id: str = BASE_RUN, name: str = "pnl_attribution") -> Path:
    plt = ck.setup()
    d = load_run(run_id, results)
    t = [d["ts"](r) for r in d["states"]]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.8), gridspec_kw={"width_ratios": [2.2, 1]})
    for key, color, label in (("gamma", ck.GREEN, "감마(+)"), ("theta", ck.RED, "세타(-)"), ("vega", ck.ORANGE, "베가(IV 변화)"), ("delta", ck.GRAY, "남은 델타")):
        a1.plot(t, [r["attr"][key] for r in d["states"]], color=color, lw=1.5, label=label)
    a1.plot(t, [r["pnl_net"] for r in d["states"]], color=ck.BLUE, lw=2, ls="--", label="순손익(수수료·호가폭 반영, 평가)")
    a1.axhline(0, color="black", lw=0.6)
    a1.set_ylabel("누적 $")
    a1.set_xlabel("시각 (KST, 시뮬레이션 시계)" if d["mode"] == "backtest" else "시각 (KST)")
    a1.set_title("손익 분해: 감마가 번 만큼 베가(IV 하락)와 세타가 가져간다", loc="left")
    a1.legend(fontsize=8, loc="upper left")
    s = d["summary"]
    if s:
        parts = [("감마", s["attr"]["gamma"]), ("세타", s["attr"]["theta"]), ("베가", s["attr"]["vega"]), ("델타", s["attr"]["delta"]),
                 ("수수료", -s["fees"]), ("순손익(청산)", s["pnl_net"])]
        colors = [ck.GREEN, ck.RED, ck.ORANGE, ck.GRAY, ck.PURPLE, ck.BLUE]
        a2.bar([p[0] for p in parts], [p[1] for p in parts], color=colors)
        for i, (_, v) in enumerate(parts):
            a2.annotate(ck.money(v), (i, v), ha="center", va="bottom" if v >= 0 else "top", fontsize=8)
        a2.axhline(0, color="black", lw=0.6)
        a2.set_title("최종(청산 시점)", loc="left")
        a2.tick_params(axis="x", labelsize=8)
    ck.stamp(fig, d["mode"], [f"{run_id}.jsonl"], note="분해는 구간 시작 그릭스 × 구간 변화의 누적(근사). 순손익은 청산 호가 기준이라 분해 합과 호가 비용만큼 다르다.")
    return ck.save(fig, out / f"{name}.png")


# ------------------------------------------------------------------ 3. 헤지 규칙·지연 비교
def plot_hedge_rule_compare(results: Path = RESULTS, out: Path = CHARTS) -> Path:
    plt = ck.setup()
    load = lambda rid: json.loads((results / f"{rid}_summary.json").read_text(encoding="utf-8"))
    rules = [("헤지 안 함", "bt-1110-2229-nohedge"), ("band 0.5", "bt-1110-2229-b0p5"), ("band 1 (기본)", "bt-1110-2229-l0"),
             ("band 2", "bt-1110-2229-b2"), ("band 3", "bt-1110-2229-b3")]
    lat = [("지연 0초", "bt-1110-2229-l0"), ("5초", "bt-1110-2229-l5"), ("10초", "bt-1110-2229-l10")]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.8))
    for ax, items, title in ((a1, rules, "헤지 band 별 순손익 (지연 0초)"), (a2, lat, "체결 지연별 순손익 (band 1)")):
        vals = [load(r)["engine_summary"]["pnl_net"] for _, r in items]
        bars = ax.bar([n for n, _ in items], vals, color=[ck.BLUE if v >= 0 else ck.RED for v in vals])
        for b, v, (_, r) in zip(bars, vals, items):
            ax.annotate(f"{ck.money(v)}\n(헤지 {load(r)['hedges']['n']}회)", (b.get_x() + b.get_width() / 2, v), ha="center", va="bottom" if v >= 0 else "top", fontsize=8)
        ax.axhline(0, color="black", lw=0.6)
        ax.set_title(title, loc="left")
        ax.tick_params(axis="x", labelsize=8)
        ax.set_ylabel("순손익 $ (청산 시점)")
    ck.stamp(fig, "backtest", ["bt-1110-2229-{nohedge,b0p5,l0,b2,b3,l5,l10}_summary.json"],
             note="하루(11/10 22:29~23:40) 한 경로라 규칙 간 우열은 판정할 수 없다. 체결 지연 5~10초만으로 부호가 바뀐다.")
    return ck.save(fig, out / "hedge_rule_compare.png")


# ------------------------------------------------------------------ 4. 패리티 잔차
def plot_parity_spread(results: Path = RESULTS, out: Path = CHARTS, run_id: str = BASE_RUN, name: str = "parity_spread") -> Path:
    plt = ck.setup()
    d = load_run(run_id, results)
    pts = [(d["ts"](r), r["parity"]["resid"], r["parity"]["half_cost"]) for r in d["states"] if r.get("parity")]
    if not pts:
        raise FileNotFoundError(f"{run_id}.jsonl 에 패리티 기록이 없음")
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.6), gridspec_kw={"width_ratios": [2.2, 1]})
    a1.plot([p[0] for p in pts], [p[1] for p in pts], color=ck.PURPLE, lw=1)
    a1.fill_between([p[0] for p in pts], [-p[2] for p in pts], [p[2] for p in pts], color=ck.GRAY, alpha=0.25, label="± 거래비용(세 다리 반호가폭 합)")
    a1.axhline(0, color="black", lw=0.6)
    a1.set_title("풋-콜 패리티 잔차 = (콜 - 풋 + 행사가) - MES (pt)", loc="left")
    a1.set_ylabel("pt")
    a1.set_xlabel("시각 (KST, 시뮬레이션 시계)" if d["mode"] == "backtest" else "시각 (KST)")
    a1.legend(fontsize=8)
    a2.hist([p[1] for p in pts], bins=30, color=ck.PURPLE, alpha=0.8)
    a2.set_title("분포", loc="left")
    a2.set_xlabel("잔차 (pt)")
    s = d["summary"]["parity"] if d["summary"] else None
    note = "신호·주문에는 쓰지 않고 기록만 한다(스프레드 진단)."
    if s:
        note += f" 전 구간(매 2초 {s['n']}개): 평균 {s['mean']}, 표준편차 {s['std']}, 최소 {s['min']}, 최대 {s['max']}, 거래비용 초과 {s['beyond_cost']}개."
    ck.stamp(fig, d["mode"], [f"{run_id}.jsonl"], note=note)
    return ck.save(fig, out / f"{name}.png")


PLOTS = {"hedge_timeline": plot_hedge_timeline, "pnl_attribution": plot_pnl_attribution,
         "hedge_rule_compare": plot_hedge_rule_compare, "parity_spread": plot_parity_spread}


def plot_live(run_id: str, results: Path = RESULTS, out: Path = CHARTS, shots: Path = SHOTS, number: int = 10) -> list[Path]:
    """본 실행(paper-sim / paper-order)의 그림 두 장: 타임라인과 손익 분해. 번호는 number, number+1."""
    made = [plot_hedge_timeline(results, out, run_id, f"run_{run_id}_timeline"), plot_pnl_attribution(results, out, run_id, f"run_{run_id}_attribution")]
    return [ck.mirror(p, number + i, shots) for i, p in enumerate(made)]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="보고서용 그래프 (results/ 의 기록만 읽음)")
    p.add_argument("command", choices=["plot"])
    p.add_argument("--only", help="쉼표로 구분한 그림 이름")
    p.add_argument("--run", help="본 실행 run_id (results/<run_id>.jsonl 필요)")
    p.add_argument("--number", type=int, default=10, help="--run 그림의 첫 번호(기본 10)")
    a = p.parse_args(argv)
    if a.run:
        for path in plot_live(a.run, number=a.number):
            print("그림:", path.relative_to(HERE))
        return 0
    names = a.only.split(",") if a.only else list(PLOTS)
    for name in names:
        try:
            path = PLOTS[name]()
            print("그림:", path.relative_to(HERE), "→", ck.mirror(path, NUMBER[name], SHOTS).relative_to(HERE))
        except FileNotFoundError as exc:
            print(f"건너뜀 {name}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
