"""Read-only Parente 5/2 reference simulation and model inspection.

The fixed published model is exposed for verification only. There is no
training, conversion, candidate selection, or standalone backtest command.
"""
from __future__ import annotations

import argparse
import json

from .core import DataError, H4, validate_candles
from .neural import CLASSES, DEFAULT_MODEL, NeuralModel


def simulate(candles, predictions, *, fee=.001, slippage=.0005, stop_loss=.10, initial=1000.):
    """Buy/Hold/Sell long-only state machine; t's signal executes at t+1 open.

    Stops gap at the adverse opening price, apply fees/slippage, and take precedence.
    Final liquidation occurs at the last close, with no entry on the final signal.
    """
    import math
    if (len(predictions) != len(candles) or any(x not in {*CLASSES, None} for x in predictions)
            or not all(math.isfinite(v) for v in (fee, slippage, stop_loss, initial))
            or not 0 <= fee < .05 or not 0 <= slippage < .05 or not 0 < stop_loss <= .10 or initial <= 0):
        raise DataError("Invalid backtest inputs")
    if not candles:
        raise DataError("Backtest requires candles")
    validate_candles(candles, H4, candles[-1].end+1, fresh=False)
    cash, position, trades, curve, peak, drawdown = initial, None, [], [], initial, 0.
    def close(price, bar, reason):
        nonlocal cash, position
        fill = price*(1-slippage)
        cash = position["quantity"]*fill*(1-fee)
        # A low proves an intrabar stop only by candle close; OHLC has no
        # exact fill timestamp. Keep known opening/terminal executions distinct.
        intrabar = reason == "stop"
        closed_ms = bar.end if intrabar or reason == "end" else bar.t
        basis = "candle_close_confirmation" if intrabar else "final_candle_close" if reason == "end" else "candle_open"
        trades.append({**position, "exit": fill, "closed_ms": closed_ms,
                       "exit_time_exact": not intrabar, "exit_time_basis": basis,
                       "reason": reason, "pnl": cash-position["capital"]})
        position = None
    for i, bar in enumerate(candles):
        signal = predictions[i-1] if i else None
        stopped = False
        if position and bar.o <= position["stop"]:
            close(bar.o, bar, "gap_stop")
            stopped = True
        if position and signal == "SELL":
            close(bar.o, bar, "sell")
        if not position and not stopped and signal == "BUY":
            fill = bar.o*(1+slippage)
            position = {"entry": fill, "quantity": cash/(fill*(1+fee)), "capital": cash,
                        "stop": fill*(1-stop_loss), "opened_ms": bar.t}
            cash = 0.
        if position and bar.l <= position["stop"]:
            close(position["stop"], bar, "stop")
        if i == len(candles)-1 and position:
            close(bar.c, bar, "end")
        equity = cash if not position else position["quantity"]*bar.c*(1-slippage)*(1-fee)
        peak = max(peak, equity)
        drawdown = max(drawdown, 1-equity/peak)
        curve.append({"time_ms": bar.end, "equity": equity})
    return {"initial": initial, "final": cash, "roi": cash/initial-1, "max_drawdown": drawdown,
            "trade_count": len(trades), "trades": trades, "equity": curve,
            "fee_rate": fee, "slippage_rate": slippage, "stop_loss": stop_loss,
            "execution": "Next-open long-only, stop precedence, all available capital per asset; final close liquidation"}


def inspect_default_model():
    """Return the fixed Parente 5/2 metadata without the per-asset calibration."""
    model = NeuralModel(DEFAULT_MODEL)
    metadata = {key: value for key, value in model.metadata.items()
                if key not in {"volume_stats", "training_assets"}}
    return {**metadata, "model_id": model.identity}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect",),
                        help="Validate and inspect the fixed Parente model")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(inspect_default_model(), indent=2, allow_nan=False))
    except (DataError, OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()


