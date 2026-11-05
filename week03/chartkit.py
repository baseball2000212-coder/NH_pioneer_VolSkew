"""주차 공용 차트 도구 — 스크린샷 대신 제출하는 그래프를 같은 모양으로 그리기 위한 작은 도우미.

이 파일은 `_tools/templates/` 가 원본이고, 새 주차를 만들 때 `submission/chartkit.py` 로 복사됩니다(그 주 시점의 사본).
각 주차의 `charts.py` 가 이 도구를 불러 그 주의 그래프를 그립니다. 규칙:

- 그래프는 `results/` 의 기록만 읽어 그립니다(주문·API 호출 없음). 제출 폴더만으로 재현됩니다.
- 모든 그림 아래에 **실행 환경 표기**([백테스트] / [자체 시뮬레이션(종이 체결)] / [모의계좌 주문] / [연구]), 근거 파일, "화면 캡처 아님"을 적습니다.
  (우리 시뮬레이터의 체결을 모의계좌 체결처럼 보이게 그리지 않기 위해서입니다.)
- 계좌·키 정보는 그리지 않습니다(`results/` 에 이미 가려진 값만 읽음).
"""
from __future__ import annotations

from pathlib import Path

ENV_LABELS = {
    "backtest": "[백테스트]",
    "paper-sim": "[자체 시뮬레이션(종이 체결)]",
    "paper-order": "[모의계좌 주문]",
    "smoke": "[모의계좌 주문 스모크]",
    "research": "[연구(과거 데이터 분석)]",
    "rule": "[규칙 설명]",
}

# 색맹에서도 구분되는 팔레트
BLUE, ORANGE, GREEN, RED, GRAY, PURPLE = "#2a6fbb", "#d9731a", "#2e8b57", "#c0392b", "#8a8f98", "#7b4fa3"


def setup():
    """matplotlib 을 화면 없이(Agg) 쓰도록 맞추고 한글 글꼴·격자 스타일을 정합니다. pyplot 을 돌려줍니다."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.alpha": 0.25,
        "figure.dpi": 100, "savefig.dpi": 150,
    })
    return plt


def stamp(fig, env: str, sources: list[str], note: str = "") -> None:
    """그림 아래에 환경 표기·근거 파일·'화면 캡처 아님'을 적습니다."""
    label = ENV_LABELS.get(env, env)
    text = f"{label}  근거: {', '.join(sources)}  ·  이 그림은 results/ 의 기록으로 그린 그래프이며 화면 캡처가 아닙니다."
    if note:
        text += f"\n{note}"
    fig.text(0.01, 0.005, text, fontsize=7.5, color=GRAY, ha="left", va="bottom")


def save(fig, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path)
    import matplotlib.pyplot as plt

    plt.close(fig)
    return path


def cumulative(values: list[float]) -> list[float]:
    total, out = 0.0, []
    for v in values:
        total += v
        out.append(total)
    return out


def drawdown(cum: list[float]) -> list[float]:
    """누적 손익 곡선에서 직전 고점 대비 낙폭(0 또는 음수)."""
    peak, out = 0.0, []
    for c in cum:
        peak = max(peak, c)
        out.append(c - peak)
    return out


def money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def log_labels(ax, axis: str = "y") -> None:
    """로그 축 눈금을 `1e-4` 같은 글자로 보여 줍니다(수식 글꼴의 유니코드 마이너스 글리프 문제를 피함)."""
    from matplotlib.ticker import FuncFormatter, NullFormatter

    fmt = FuncFormatter(lambda v, _: f"{v:.0e}")
    target = ax.yaxis if axis == "y" else ax.xaxis
    target.set_major_formatter(fmt)
    target.set_minor_formatter(NullFormatter())


def mirror(path: Path | str, number: int, shots_dir: Path | str) -> Path:
    """그래프를 제출 폴더의 screenshots/ 에도 `NN-이름.png` 로 복사합니다(스크린샷 자리에 그래프를 넣는 규칙).

    번호 NN 은 그림마다 고정해 두세요(다시 그려도 같은 파일을 덮어쓰도록)."""
    import shutil

    shots = Path(shots_dir)
    shots.mkdir(parents=True, exist_ok=True)
    target = shots / f"{number:02d}-{Path(path).stem}.png"
    shutil.copy2(path, target)
    return target
