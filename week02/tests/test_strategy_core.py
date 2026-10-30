"""strategy_core: 신호·손절·손익 순수 함수 테스트 (API 없이)."""
import json
from datetime import time
from decimal import Decimal as D
from pathlib import Path

import pytest

import strategy_core as sc

STRATEGY = Path(__file__).resolve().parents[1] / "strategy.json"


def test_day_return_pct():
    assert sc.day_return_pct(D("1000"), D("1030")) == D("3")
    assert sc.day_return_pct(D("1000"), D("990")) == D("-1")
    with pytest.raises(ValueError):
        sc.day_return_pct(D("0"), D("1"))


@pytest.mark.parametrize(
    "ret, side, reason",
    [
        ("0.4", "buy", "enter"),
        ("-1.8", "sell", "enter"),
        ("2.999", "buy", "enter"),
        ("3.0", None, "skip_threshold"),      # 경계: |r| < 3 만 진입
        ("-3.0", None, "skip_threshold"),
        ("8.2", None, "skip_threshold"),
        ("0", None, "skip_flat"),
    ],
)
def test_decide(ret, side, reason):
    d = sc.decide(D(ret), D("3.0"))
    assert (d.side, d.reason) == (side, reason)


def test_stop_price_and_hit():
    assert sc.stop_price("buy", D("1000"), D("10")) == D("990")
    assert sc.stop_price("sell", D("1000"), D("10")) == D("1010")
    assert sc.stop_hit("buy", D("990"), D("990")) is True      # 닿으면 발동
    assert sc.stop_hit("buy", D("990.1"), D("990")) is False
    assert sc.stop_hit("sell", D("1010"), D("1010")) is True
    assert sc.stop_hit("sell", D("1009.9"), D("1010")) is False


def test_pnl():
    assert sc.pnl_pt("buy", D("1000"), D("1003"), D("0.2")) == D("2.8")
    assert sc.pnl_pt("sell", D("1000"), D("1003"), D("0.2")) == D("-3.2")
    assert sc.pnl_won(D("2.8"), 1, D("10000")) == D("28000")
    assert sc.pnl_won(D("2.8"), 2, D("10000")) == D("56000")


def test_opposite():
    assert sc.opposite("buy") == "sell" and sc.opposite("sell") == "buy"
    with pytest.raises(ValueError):
        sc.opposite("x")


def test_config_from_file_has_no_missing_keys():
    cfg = sc.StrategyConfig.from_dict(json.loads(STRATEGY.read_text(encoding="utf-8")))
    assert cfg.sym == "A066C000" and cfg.qty == 1
    assert cfg.threshold_pct == D("3.0") and cfg.stop_pt == D("10.0")
    assert cfg.decision_time == time(15, 19, 50) and cfg.entry_time == time(15, 20)
    # 시각 순서가 맞아야 한다
    assert cfg.decision_time < cfg.entry_time < cfg.stop_until < cfg.exit_submit_auction < cfg.exit_retry_time < cfg.auction_fill_time < cfg.end_time
    assert cfg.one_stop_loss_won == D("100000")


def test_config_rejects_bad_values():
    base = json.loads(STRATEGY.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(base))
    bad["params"]["exit_mode"] = "later"
    with pytest.raises(ValueError):
        sc.StrategyConfig.from_dict(bad)
    bad = json.loads(json.dumps(base))
    bad["params"]["entry_time"] = "15:10:00"       # 판단 시각보다 이름
    with pytest.raises(ValueError):
        sc.StrategyConfig.from_dict(bad)
    bad = json.loads(json.dumps(base))
    del bad["params"]["stop_pt"]
    with pytest.raises(KeyError):
        sc.StrategyConfig.from_dict(bad)
