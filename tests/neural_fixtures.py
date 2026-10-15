"""Shared offline fixtures for the current combined strategy."""
import importlib.util
import math

from adaptive_crypto.core import Candle, H4, M5, Rules

ASSETS = {"BTC": {"symbol": "BTC/USDC", "price_decimals": 2}}
NOW = (1_800_000_000_000 // H4) * H4 + 1000
RULES = Rules(strategy_model="neural_network", fee_rate=.001, paper_floor=0,
              risk_per_trade=.02, max_total_risk=.05)
NUMERIC_AVAILABLE = all(importlib.util.find_spec(name) is not None
                        for name in ("numpy", "pandas", "talib"))


def bars(now=NOW, count=220, interval=H4):
    end = now // interval * interval
    return [Candle(end - (count - i) * interval, 100 + math.sin(i / 7),
                   103 + math.sin(i / 7), 97 + math.sin(i / 7),
                   100 + math.sin(i / 7) + math.cos(i / 3) * .5,
                   1000 + i % 17 * 45, interval) for i in range(count)]


def quote(now=NOW, price=100.):
    return {"bid": price, "ask": price + .01, "asof_ms": now}


def qualified_market(now=NOW, identity="pine:fixture", *, tp1=110., tp2=None):
    """A qualified Pine SMC long proposal with immutable SMC stop/targets."""
    plan = {"id": identity, "key": identity, "setup_key": "block:1", "source": "pine_smc",
            "side": "long", "stop": 90., "target": tp1, "tp1": tp1, "tp2": tp2,
            "exit_price": tp2 or tp1, "signal_end": now // M5 * M5 - 1,
            "expires_ms": now + 30000, "checks": [{"status": "pass", "key": "pine_rejection"}]}
    return {"source": "pine_smc", "guide": {"status": "qualified", "expires_ms": now + 30000},
            "suggestion": plan, "suggestions": [plan]}


class FakeModel:
    identity = "a" * 64
    trained_through_ms = 0
    metadata = {"trained_through_ms": 0}

    def __init__(self, label="BUY"):
        self.label, self.calls = label, 0

    def predict(self, candles, asset):
        self.calls += 1
        self.asset = asset
        return {"label": self.label,
                "probabilities": {s: .8 if s == self.label else .1 for s in ("BUY", "HOLD", "SELL")},
                "signal_end": candles[-1].end, "model_id": self.identity}
