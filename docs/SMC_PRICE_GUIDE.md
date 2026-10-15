# SMC price guide

New SMC entry plans use the causal Python strategy port of **Perky SMC Ultimate V5**. The chart, market guide and paper setup consume the same completed candles from the selected pair and market source. The published Pine source can be downloaded from the SMC chart. See [the Perky port notes](perky_smc_port.md).

## Perky entry plan

pyPTA starts with the published Filtered defaults on closed candles: liquidity sweeps, internal CHoCH and momentum shifts; a minimum confluence score of 4/10; and the 25-bar cooldown with its five-bar expansion re-entry rule. Settings can adjust supported strategy gates, score components, pivot lengths and stop mode. TP1, TP2 and TP3 use the script's FVG, opposing order-block and swing references, sorted by distance. pyPTA presents the signal candle's Pine reference price separately from the current executable bid or ask used for its proposed market entry.

Plans require a fresh quote after the signal candle, a valid ordered stop/target bracket, and a spread inside the configured limit. They remain market-quote references; no exchange order is sent. Existing positions retain their saved levels. Perky structure context can show adverse completed-bar breaks for review.

The `smc_min_exit_distance_pct` setting filters a Perky plan using TP3 as the final target. Target reachability can be advisory or required by the user's settings. GEX alignment remains available as separate context and no longer selects or moves Perky targets.

Automatic paper entry requires a current long Perky plan plus Parente BUY. A paper position stores TP1 and TP2 as intermediate levels and TP3 as its full exit target. Stop, target or a later Parente SELL closes the paper position; stops have priority when a completed candle touches both stop and target. There is no partial target exit.

## Shared market inputs

The selected SMC pair and exchange feed supply chart candles, the Perky signal calculation and current quotes. Settings supports Deribit-first or Binance spot. Deribit four-hour bars aggregate complete UTC hourly candles; Binance supplies native four-hour candles. Gaps, stale/future data, missing latest candles and malformed timestamps withhold analysis. Paper account values remain USD; entry and exit capture fresh conversion rates when the pair uses another quote currency. Parente continues to use its original Binance spot/USDT 4H input. See [market source definitions](DERIBIT_DATA.md).

## Target reachability

See [the reachability framework](SMC_REACHABILITY.md). Its swing range, liquidity-cycle and block/FVG evidence comes from the Perky structure snapshot and completed market candles; it is a configurable heuristic, not a probability or arrival-time estimate.
