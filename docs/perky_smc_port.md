# Perky SMC Ultimate: source and Python strategy port

- TradingView link supplied: <https://www.tradingview.com/script/nqauG2wM-Perky-SMC-Ultimate-V3/>.
- The public source currently served at that link identifies itself as **Perky SMC Ultimate V5**, original script version 3.0, by **Perkytradess / Peak Ventures**.
- Published Pine source: [`scripts/pine/perky_smc_ultimate_v5.pine`](../scripts/pine/perky_smc_ultimate_v5.pine), retained with its Mozilla Public License 2.0 header and attribution.
- Python strategy: `adaptive_crypto/perky_smc.py`; it produces the completed-candle structure snapshot used by the chart, guide, position context and reachability assessment.
- Entry planning: `adaptive_crypto/pine_guidance.py` uses the Perky output for current long/short signals, score gates, stop, TP1, TP2 and TP3.
- `/api/smc-script` downloads the saved Perky source.

## Strategy behavior ported

pyPTA starts with the published **Filtered** defaults. The supported strategy inputs can be changed in Settings → Perky SMC strategy:

- Five-bar wick pivots and the default **Only Wicks** sweep mode.
- Five-bar internal and 50-bar swing structure pivots; internal bullish/bearish CHoCH events can qualify entries.
- Momentum shift after two opposite-colour candles.
- Ten score points: +2 sweep, then +1 each for last same-direction CHoCH, directional FVG, directional internal order block, London/New York kill zone, EMA alignment, non-exhausted RSI, at least two aligned dashboard timeframes, and volume above its 20-bar average.
- Minimum score 4; Filtered mode gates the entry types and applies a 25-bar cooldown, with expansion re-entry allowed from bar five.
- Structure stop using the latest swing anchor and 0.25 ATR buffer, clamped between 0.15 and 3 ATR from the Pine signal reference. ATR stops are also selectable.
- TP1, TP2 and TP3 use the nearest directional FVG, opposing internal order block and latest swing (with the source ATR fallbacks), sorted in the trade direction.

The plan stores the Pine signal reference separately from the live bid/ask execution reference. The guide only exposes a current plan when the source bar is completed, its signal passes the score/cooldown gates, a fresh quote follows the signal, the quote remains inside the bracket and the configured spread and optional reachability requirements pass. TP3 is the full paper exit; TP1 and TP2 are preserved as intermediate targets. No exchange orders are sent.

## Scope and limitations

The saved Pine file is the publisher's source copy. The Python module is a causal strategy port, not an execution of Pine Script and not a TradingView golden-data parity certification. It does not reproduce the indicator's visual layer, alerts, every user-selectable input, or all of the Pine internals. In particular, order-block and FVG bookkeeping is implemented from the default source behavior, and the four higher-timeframe EMA biases are built only when complete bars for those timeframes can be derived from the selected chart feed. A requested lower timeframe is unavailable from higher-timeframe candles; the default HTF agreement gate is zero, while available HTF alignment can still contribute one score point.

The Python feed excludes forming candles and rejects gaps rather than filling them. The Perky strategy snapshot is the sole SMC structure source for new entries, position context, liquidity display and optional reachability evidence. See [the price guide](SMC_PRICE_GUIDE.md).

The original source is governed by MPL-2.0. Preserve its copyright and license header when redistributing the saved Pine source.
