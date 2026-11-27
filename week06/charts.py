"""6주차 보고서 그림. 입력: results/ 의 실행 기록·호가 발췌·백테스트 거래별 손익. 출력: results/charts/, screenshots/."""
from __future__ import annotations
import json, sys
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

HERE = Path(__file__).resolve().parent
RES, SHOT = HERE / "results", HERE / "screenshots"
plt.rcParams["font.family"] = ["Malgun Gothic", "NanumGothic", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
BLUE, RED, GRAY, INK = "#2f6fd6", "#d64545", "#8a8f98", "#1f2328"


def save(fig, name):
    for d in (RES / "charts", SHOT):
        d.mkdir(parents=True, exist_ok=True)
    fig.savefig(RES / "charts" / name.split("-", 1)[1], dpi=150, bbox_inches="tight")
    fig.savefig(SHOT / name, dpi=150, bbox_inches="tight")
    plt.close(fig)


def book_at(rows, t):
    return [r for r in rows if r["t"] <= t][-1]


def draw_book(ax, b, title, eat_bid=None, note="", eat_ask=None):
    """HTS 호가창 모양: 위 매도호가(파랑), 아래 매수호가(빨강). eat_bid = {가격: 매도로 체결된 수량}, eat_ask = {가격: 매수로 체결된 수량}."""
    eat_bid, eat_ask = eat_bid or {}, eat_ask or {}
    rows = [("ask", b[f"ask_pric_{i}"], int(b[f"ask_qty_{i}"])) for i in range(5, 0, -1)] + \
           [("bid", b[f"bid_pric_{i}"], int(b[f"bid_qty_{i}"])) for i in range(1, 6)]
    mx = max(q for _, _, q in rows)
    ax.set_xlim(0, 3); ax.set_ylim(0, len(rows)); ax.axis("off")
    for k, (side, px, q) in enumerate(rows):
        y = len(rows) - 1 - k
        bg = "#eaf1fd" if side == "ask" else "#fdecec"
        ax.add_patch(plt.Rectangle((0, y), 3, 1, color=bg, ec="white", lw=1.5))
        ax.text(1.5, y + .5, px, ha="center", va="center", fontsize=12, color=INK, weight="bold")
        if side == "ask":
            ax.add_patch(plt.Rectangle((1.0 - .9 * q / mx, y + .2), .9 * q / mx, .6, color=BLUE, alpha=.35))
            ax.text(.08, y + .5, f"{q}", va="center", fontsize=11, color=BLUE)
            e = eat_ask.get(px)
            if e:
                ax.add_patch(plt.Rectangle((1.0 - .9 * e / mx, y + .2), .9 * e / mx, .6, fill=False, ec=INK, lw=2, ls="--"))
                ax.text(-.05, y + .5, f"매수 {e}계약 →", va="center", ha="right", fontsize=11, color=INK, weight="bold")
        else:
            ax.add_patch(plt.Rectangle((2.0, y + .2), .9 * q / mx, .6, color=RED, alpha=.35))
            ax.text(2.92, y + .5, f"{q}", va="center", ha="right", fontsize=11, color=RED)
            e = eat_bid.get(px)
            if e:
                ax.add_patch(plt.Rectangle((2.0, y + .2), .9 * e / mx, .6, fill=False, ec=INK, lw=2, ls="--"))
                ax.text(3.05, y + .5, f"← 매도 {e}계약", va="center", fontsize=11, color=INK, weight="bold")
    ax.text(0.5, len(rows) + .25, "매도 잔량", ha="center", fontsize=10, color=BLUE)
    ax.text(2.5, len(rows) + .25, "매수 잔량", ha="center", fontsize=10, color=RED)
    ax.set_title(title, fontsize=13, loc="left", pad=22)
    if note:
        ax.text(0, -.6, note, fontsize=10.5, color=INK, va="top")


def fig_book():
    rows = [json.loads(l) for l in open(RES / "book_20261125_1519.jsonl", encoding="utf-8")]
    b0, b1 = book_at(rows, "15:19:00.010"), book_at(rows, "15:19:05.010")
    fig, axs = plt.subplots(1, 2, figsize=(14, 6.2), gridspec_kw=dict(wspace=.55))
    draw_book(axs[0], b0, "① 15:19:00.010  첫 주문 직전 호가", {"1085.30": 10, "1085.20": 7},
              "매수 1·2호가 잔량 10+7 = 17계약 → 17계약만 매도\n평균 체결가 1085.26, 중간가 1085.40 대비 1.4틱")
    draw_book(axs[1], b1, "② 15:19:05.010  5초 뒤 호가", {"1085.50": 2, "1085.40": 1},
              "1호가 잔량이 다시 찼다 → 남은 3계약 매도\n평균 1085.47, 중간가 1085.55 대비 0.8틱")
    fig.suptitle("11/25 실제 호가창(코스닥150 선물 12월물, 5단계)과 20계약 매도", fontsize=14, x=.06, ha="left", y=1.02)
    fig.text(.06, -.08, "20계약을 한 번에 냈다면: 1085.30×10 + 1085.20×7 + 1085.10×3 → 평균 1085.24, 1.7틱. 나눠서 낸 쪽이 계약당 0.3틱(20계약 6,600원) 덜 밀렸다.",
             fontsize=10.5, color=INK)
    save(fig, "01-orderbook_1519.png")


def fig_backtest():
    s = pd.read_csv(RES / "backtest_trades_w06.csv")
    s["dt"] = pd.to_datetime(s.d.astype(str))
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6.6), sharex=True, gridspec_kw=dict(height_ratios=[2.2, 1]))
    a1.plot(s.dt, s.a20.cumsum() / 1e6, color=GRAY, lw=1.6, label="20계약 한 번에 주문")
    a1.plot(s.dt, s.b20.cumsum() / 1e6, color=BLUE, lw=1.8, label="20계약 호가 따라 나눠 주문")
    a1.set_ylabel("누적 손익(백만 원)"); a1.legend(frameon=False, loc="upper left"); a1.grid(alpha=.25)
    a2.plot(s.dt, (s.b20 - s.a20).cumsum() / 1e4, color=BLUE, lw=1.6)
    a2.axhline(0, color=GRAY, lw=.8); a2.set_ylabel("차이 누적(만 원)"); a2.grid(alpha=.25)
    a2.set_title("나눠 주문 − 한 번에 주문 (조각당 비용은 11월 호가 13일로 잰 값을 모든 날에 적용)", loc="left", fontsize=10.5)
    for a in (a1, a2):
        a.axvspan(pd.Timestamp("2025-01-01"), s.dt.max(), color="#f3f4f6", zorder=0)
    a1.text(pd.Timestamp("2025-01-20"), a1.get_ylim()[1] * .93, "표본 밖", color=GRAY)
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    a1.set_title("2주차 국내전략(15:19 진입 → 15:45 청산) 943건, 수수료·호가 충격 뺀 뒤", loc="left", fontsize=13)
    save(fig, "02-backtest_split.png")


def fig_run():
    t = [json.loads(l) for l in open(RES / "ticks-w06-a20-paper-order-20261125-145502.jsonl", encoding="utf-8")]
    df = pd.DataFrame([(pd.Timestamp(r["ts"]).tz_localize(None), float(r["price"])) for r in t
                       if r.get("api") == "KA" and r.get("price")], columns=["ts", "px"])
    df = df[(df.ts.dt.strftime("%H:%M") >= "15:12") & (df.ts.dt.strftime("%H:%M") <= "15:36")]
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.plot(df.ts, df.px, color=GRAY, lw=.8)
    d = df.ts.iloc[0].normalize()
    T = lambda s: d + pd.Timedelta(s)
    ax.scatter([T("15:19:00.010"), T("15:19:05.010")], [1085.30, 1085.50], s=[170, 60], color=BLUE, zorder=5)
    ax.annotate("매도 17계약", (T("15:19:00"), 1085.3), (T("15:13:30"), 1088.2), arrowprops=dict(arrowstyle="->", color=BLUE), color=BLUE)
    ax.annotate("5초 뒤 3계약", (T("15:19:05"), 1085.5), (T("15:21:30"), 1088.2), arrowprops=dict(arrowstyle="->", color=BLUE), color=BLUE)
    ax.axhline(1082.50, color=RED, lw=1, ls="--")
    ax.text(T("15:12:20"), 1080.9, "빨간 점선: 15:45 종가 단일가 1082.50 — 여기서 20계약을 한꺼번에 청산", color=RED, fontsize=10)
    ax.axvline(T("15:35:30"), color=INK, lw=.8, ls=":")
    ax.text(T("15:35:20"), ax.get_ylim()[1] - .6, "15:35:30\n청산 주문", ha="right", va="top", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M")); ax.grid(alpha=.25); ax.set_ylabel("가격(pt)")
    ax.set_title("11/25 모의계좌 실행: 전일 대비 −2.1% → 매도 20계약, 계약당 +2.38pt(+47.7만 원)", loc="left", fontsize=13)
    save(fig, "10-run_ord-20261125.png")


def load_ticks(name, t0, t1):
    t = [json.loads(l) for l in open(RES / name, encoding="utf-8")]
    df = pd.DataFrame([(pd.Timestamp(r["ts"]).tz_localize(None), float(r["price"])) for r in t
                       if r.get("api") == "KA" and r.get("price")], columns=["ts", "px"])
    hms = df.ts.dt.strftime("%H:%M:%S")
    return df[(hms >= t0) & (hms <= t1)]


def child_fills(name):
    """실행 기록의 조각 주문: (목적, 보낸 시각, 수량, 모의계좌 체결가, 호가창 기준 체결가)."""
    out = []
    for l in open(RES / name, encoding="utf-8"):
        r = json.loads(l)
        if r.get("name") == "child_fill":
            out.append((r["purpose"], pd.Timestamp(r["sent"]).tz_localize(None), int(r["got"]),
                        float(r["fill"]), float(r["shadow_book_fill"])))
    return out


def fig_book_1126():
    rows = [json.loads(l) for l in open(RES / "book_20261126_1519.jsonl", encoding="utf-8")]
    b0 = book_at(rows, "15:19:00.019")
    fig, ax = plt.subplots(figsize=(7.4, 6.2))
    draw_book(ax, b0, "15:19:00.019  첫 주문 직전 호가", note=
              "매도 1·2호가 잔량 13+23 = 36계약 ≥ 20계약 → 한 번에 20계약 매수\n"
              "호가창 기준 평균 1087.64 (1087.60×13 + 1087.70×7), 중간가 1087.50 대비 1.35틱",
              eat_ask={"1087.60": 13, "1087.70": 7})
    fig.suptitle("11/26 실제 호가창(코스닥150 선물 12월물, 5단계)과 20계약 매수", fontsize=14, x=.06, ha="left", y=1.02)
    save(fig, "11-orderbook_20261126_1519.png")


def fig_run_1126():
    df = load_ticks("ticks-w06-a20-paper-order-20261126-145502.jsonl", "15:12:00", "15:36:00")
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.plot(df.ts, df.px, color=GRAY, lw=.8)
    d = df.ts.iloc[0].normalize()
    T = lambda s: d + pd.Timedelta(s)
    ax.scatter([T("15:19:00.019")], [1087.80], s=[220], color=RED, zorder=5)
    ax.annotate("매수 20계약 (한 번에)", (T("15:19:00"), 1087.8), (T("15:12:20"), 1087.9), arrowprops=dict(arrowstyle="->", color=RED), color=RED, va="center")
    ax.axhline(1095.70, color=BLUE, lw=1, ls="--")
    ax.text(T("15:12:20"), 1096.0, "파란 점선: 15:45 종가 단일가 1095.70 — 여기서 20계약을 한꺼번에 청산", color=BLUE, fontsize=10)
    ax.axvline(T("15:35:30"), color=INK, lw=.8, ls=":")
    ax.text(T("15:35:20"), ax.get_ylim()[0] + .4, "15:35:30\n청산 주문", ha="right", va="bottom", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M")); ax.grid(alpha=.25); ax.set_ylabel("가격(pt)")
    ax.set_title("11/26 모의계좌 실행: 전일 대비 +0.50% → 매수 20계약, 계약당 +7.85pt(+157.0만 원)", loc="left", fontsize=13)
    save(fig, "12-run_ord-20261126.png")


def fig_drill_1126():
    name = "w06-drill-paper-order-20261126-103002.jsonl"
    df = load_ticks("ticks-" + name, "10:31:20", "10:32:20")
    kids = child_fills(name)
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.step(df.ts, df.px, where="post", color=GRAY, lw=.9)
    for purpose, color, label in (("entry", RED, "매수 조각"), ("stop", BLUE, "손절 매도 조각")):
        k = [c for c in kids if c[0] == purpose]
        ax.scatter([c[1] for c in k], [c[3] for c in k], s=[40 + 22 * c[2] for c in k], color=color, alpha=.85, zorder=5, label=label)
        for c in k:
            ax.text(c[1], c[3] + (.07 if purpose == "entry" else -.07), f"{c[2]}", ha="center",
                    va="bottom" if purpose == "entry" else "top", fontsize=9, color=color)
    d = df.ts.iloc[0].normalize()
    T = lambda s: d + pd.Timedelta(s)
    ax.axhline(1117.52, color=INK, lw=.9, ls="--")
    ax.text(T("10:31:20.5"), 1118.45, "검은 점선: 손절선 1117.52 (진입 평균 1117.82 − 0.3pt)", fontsize=9.5, color=INK, va="center")
    ax.axvline(T("10:32:03.070"), color=INK, lw=.8, ls=":")
    ax.text(T("10:32:03.5"), ax.get_ylim()[1] - .05, "10:32:03.070\n손절선 닿음", va="top", fontsize=9)
    ax.legend(frameon=False, loc="lower left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S")); ax.grid(alpha=.25); ax.set_ylabel("가격(pt)")
    ax.set_title("11/26 10:30 훈련(손절 0.3pt): 5초 간격 4번에 나눠 매수 20계약 → 손절 → 1초 간격 7번에 나눠 청산 (점 옆 숫자 = 계약 수)",
                 loc="left", fontsize=11.5)
    save(fig, "13-drill-20261126.png")


def fig_delay_1126():
    """같은 20계약 매수가 NH 에 0.04초 뒤 닿을 때와 0.40초 뒤(실제, 연결 새로 맺음) 닿을 때의 호가창."""
    rows = [json.loads(l) for l in open(RES / "book_20261126_1519.jsonl", encoding="utf-8")]
    b0, b1 = book_at(rows, "15:19:00.040"), book_at(rows, "15:19:00.400")
    fig, axs = plt.subplots(1, 2, figsize=(14, 6.2), gridspec_kw=dict(wspace=.55))
    draw_book(axs[0], b0, "① 15:19:00.04  데운 연결로 닿았다면", note=
              "1087.60×13 + 1087.70×7 → 평균 1087.635", eat_ask={"1087.60": 13, "1087.70": 7})
    draw_book(axs[1], b1, "② 15:19:00.40  실제로 닿은 때 (연결 새로 맺음)", note=
              "1087.60·1087.70 잔량은 이미 다른 매수가 가져감\n1087.80×18 + 1087.90×2 → 평균 1087.81", eat_ask={"1087.80": 18, "1087.90": 2})
    fig.suptitle("11/26 15:19 매수 20계약: 0.36초 차이로 달라진 호가창 (코스닥150 선물 12월물, 5단계)", fontsize=14, x=.06, ha="left", y=1.02)
    fig.text(.06, -.08, "0.36초 사이 매도 1호가가 1087.60 → 1087.80 으로 2틱 올랐다. 같은 20계약이 계약당 1.75틱(20계약 3.5만 원) 비싸게 체결된다.",
             fontsize=10.5, color=INK)
    save(fig, "14-orderbook_delay_20261126.png")


def fig_drill_1127():
    """11/27 10:40 훈련: 연결 유지를 넣은 실제 매매 코드로 나눠 매수 → 0.3pt 손절 → 나눠 청산."""
    name = "w06-drill-paper-order-20261127-104048.jsonl"
    df = load_ticks("ticks-" + name, "10:42:10", "10:43:25")
    kids = child_fills(name)
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.step(df.ts, df.px, where="post", color=GRAY, lw=.9)
    for purpose, color, label in (("entry", RED, "매수 주문"), ("stop", BLUE, "손절 매도 주문")):
        k = [c for c in kids if c[0] == purpose]
        ax.scatter([c[1] for c in k], [c[3] for c in k], s=[40 + 22 * c[2] for c in k], color=color, alpha=.85, zorder=5, label=label)
        for c in k:
            ax.text(c[1], c[3] + (.06 if purpose == "entry" else -.06), f"{c[2]}", ha="center",
                    va="bottom" if purpose == "entry" else "top", fontsize=9, color=color)
    d = df.ts.iloc[0].normalize()
    T = lambda s: d + pd.Timedelta(s)
    ax.axhline(1166.66, color=INK, lw=.9, ls="--")
    ax.text(T("10:42:11"), 1166.60, "검은 점선: 손절선 1166.66 (진입 평균 1166.96 − 0.3pt)", fontsize=9.5, color=INK, va="top")
    ax.axvline(T("10:43:12.642"), color=INK, lw=.8, ls=":")
    ax.text(T("10:43:10"), ax.get_ylim()[1] - .03, "10:43:12.642\n손절선 닿음", va="top", ha="right", fontsize=9)
    ax.legend(frameon=False, loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S")); ax.grid(alpha=.25); ax.set_ylabel("가격(pt)")
    ax.set_title("11/27 10:40 훈련(연결 유지 적용): 매수 17+3계약 → 손절 18+1+1계약, 주문 5건 모두 NH 접수 0.05~0.06초 (점 옆 숫자 = 계약 수)",
                 loc="left", fontsize=11)
    save(fig, "15-drill-20261127.png")


if __name__ == "__main__":
    figs = {"book": fig_book, "backtest": fig_backtest, "run": fig_run,
            "book26": fig_book_1126, "run26": fig_run_1126, "drill26": fig_drill_1126, "delay26": fig_delay_1126, "drill27": fig_drill_1127}
    which = sys.argv[1:] or list(figs)
    for w in which:
        figs[w]()
    print("그림 저장:", which)
