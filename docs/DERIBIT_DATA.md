# Selected market feeds and independent neural inputs

Settings accepts exact pairs listed in the Deribit or Binance catalogs. The Market data choice is `deribit_first` (prefer an active Deribit perpetual, then Deribit spot, then Binance spot only when the pair is absent) or `binance` (Binance spot directly). Enabled pairs may use different quote currencies. The single paper cash account is denominated in USD, with observed quote-to-USD conversion recorded separately for each entry and exit. A supported Deribit data outage never triggers fallback. No automatic stablecoin peg or currency relabelling is used. Parente is separate: it uses its trained Binance spot/USDT 4H inputs regardless of this selection.

## Candles and index prices

`public/get_tradingview_chart_data` supplies native perpetual or spot OHLCV; `volume` is base coins. Deribit does not offer a 240-minute resolution: 4H bars use UTC-aligned groups of four complete 60-minute bars, sum volume, first open, last close, maximum high and minimum low. All expected timestamps are required. SMC uses the configured timeframe; charts use those same completed candles. Parente uses completed Binance spot/USDT 4H candles with the original model normalization. Binance fallback uses public `exchangeInfo`, `time`, `klines` and `ticker/bookTicker`: completed native candles, base volume and current order-book quotes. Each selected feed verifies its exchange clock and complete candle boundaries. Current Deribit references use `public/get_index_price` for the selected pair when the matching index is listed, otherwise `public/ticker` for its exact instrument. Index bid/ask fields are equal reference values, explicitly synthetic and non-executable; spread is not observed. Fees/slippage are simulation settings.

## Native market prices and the USD paper account

Every position retains its exact native symbol and quote currency. Entry prices, exit prices, SMC stops, liquidity and targets are never relabelled as USD. A paper budget is USD cash. A fresh observed quote-to-USD rate converts that budget into native notional before quantity is calculated; the entry rate and its provenance are recorded. Closing a trade obtains and records a fresh exit conversion rate, so account proceeds, fees and realized P/L reflect both the native price change and any quote-currency change. Missing or stale conversion withholds the accounting mutation. USD has an identity rate of one; stablecoins require an observed rate rather than an assumed dollar peg. Recorded manual holdings show gross P/L in their own quote and do not spend paper cash.

## Options GEX

The options backend requests `public/get_instruments` with `currency=any` and discovers every active settlement market for each configured base. It groups contracts by `settlement_currency` and the strike/forward currency in `price_index`; inverse option `quote_currency` describes the premium currency and must not be used as the strike currency. The instrument catalogue is shared across assets and refreshed after 60 seconds; settlement summaries are shared for 30 seconds. No BTC/ETH/SOL list controls discovery. API options open interest is already base coins, including linear contract sizing. It must not be multiplied by `contract_size` again. Observed Deribit USD/USDC indices convert strikes and published expiry forwards into a common native USD or USDC coordinate. Missing summaries, duplicate contracts, stale stamps or missing forwards exclude the whole affected market. Partial coverage can be displayed but cannot select a GEX target. A base without listed options has no GEX substitute.

For any other selected quote currency, the complete options calculation first uses USD. A fresh observed quote-to-USD currency snapshot supplies USD per quote unit; its buy/sell midpoint gives an advisory coordinate conversion factor of `1 / midpoint`. This same factor scales every price (index, strikes, call/put walls, flips and curve prices) and every quote-denominated exposure (net, gross, strike call/put/net and curve GEX). Option counts, OI, IV, signs and dimensionless ratios remain unchanged. This is labelled a synthetic currency reference and records the full FX source path, rates and capture timestamps. No dollar peg is assumed. The options coordinate can therefore match an exact Binance fallback pair even when Deribit has no corresponding base/quote market. Directional bid/ask rates remain separate for actual paper-account costs and proceeds.

Converted GEX expires at the earliest included options/settlement-FX/native-coordinate FX deadline. Foreign-coordinate snapshots refresh before the 20-second FX lifetime ends and request a new rate rather than republishing a cached expiring rate. Missing, stale or mismatched currency traces exclude GEX from SMC target selection; the original Pine liquidity remains available.

With index S, strike K, expiry forward F, IV sigma and ACT/365 years T:

- d1 = (ln(F/K) + sigma² T/2) / (sigma sqrt(T)).
- Index-delta gamma = phi(d1) / (S sigma sqrt(T)), holding F/S fixed.
- Signed GEX = gamma × base-coin OI × S² × 0.01 × sign.
- Calls use +1 and puts -1. This is an assumed positioning proxy, not dealer inventory.
- Gross GEX sums absolute exposures; net GEX sums signed exposures.
- Call wall maximizes call gamma by strike; put wall maximizes absolute put gamma separately.
- Gamma flip is a numerical root of repriced net GEX over ±30% index, with fixed IV/OI/F/S. It may be absent or non-unique.

Units are selected-quote delta per 1% index move. Deribit's ticker gamma is a forward-price gamma: mapping to the index sensitivity multiplies it by F/S. Decimal rounding in ticker gamma limits direct comparison. GEX confluence remains available for existing-position guidance; new Perky SMC trade plans keep their Pine-derived TP1–TP3 regardless of GEX. Saved position levels change only through explicit forms.

## DVOL

`public/get_volatility_index_data` supplies the official BTC/ETH 30-day annualized implied-volatility index, in percentage points. No SOL substitute is invented. Backward continuation uses the next end timestamp. Missing intervals, stale observations and incomplete pagination are reported. The chart supports 24h, 7d, 30d and 90d.

Approximate one-standard-deviation movement = selected-pair index price × DVOL/100 × sqrt(days/365), for 1, 7 and 30 days. This is a volatility range, not a predicted target; fresh matching prices and DVOL are required.

## Connections

Public endpoints work without authentication. Optional Deribit credentials use read-only scopes and memory-only tokens. Saved encrypted settings override environment variables and named `keys/CLIENT_ID_DERIBIT.md`, `keys/CLIENT_SECRET_DERIBIT.md`. Explicit public mode overrides these fallbacks. Telegram similarly supports named project files. Keys are excluded from the source release. FlashAlpha is not used: its free equity GEX service does not provide this crypto coverage.

Sources: [Deribit chart API](https://docs.deribit.com/api-reference/market-data/public-get_tradingview_chart_data), [options OI definition](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency), [ticker greeks](https://docs.deribit.com/api-reference/market-data/public-ticker), [DVOL methodology](https://insights.deribit.com/exchange-updates/dvol-deribit-implied-volatility-index/).

## Independent DVOL configuration

`dvol-settings.json` stores DVOL pairs separately from the main asset list. Settings lists the intersection of active Deribit BTC/ETH markets and published matching price indices. Binance is never a DVOL fallback. Saving this section applies immediately. A BTC/USDC dashboard may therefore show ETH/USD DVOL independently; quote changes affect the reference-price ranges, not the official volatility series. Main account backup/restore and purge do not replace this independent configuration.
