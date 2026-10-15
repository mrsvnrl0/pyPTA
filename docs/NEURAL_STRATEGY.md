# Parente 5/2 and combined paper execution

The dashboard has one neural model: the authors' published Parente 5/2. The original weights, reconstructed final-training scaler and frozen Binance volume statistics are retained. Inference is local; AI commentary connections do not supply neural trading decisions.

## Matching the trained inputs

Parente reads Binance **spot/USDT** OHLCV on completed UTC **4H** candles. Volume is Binance's base-asset kline volume, not quote turnover, a sum across venues or derivatives volume. The live provider validates the trading symbol, exchange clock, candle boundaries, continuity and latest completed bar. It checks feed health even when a completed-candle classification is cached. Failure withholds the current signal; another venue's candles are not substituted.

Before execution, the reading must identify the loaded Parente artifact, the exact latest completed 4H candle and its matching Binance input contract. BUY/HOLD/SELL must agree with the maximum of finite normalized class probabilities. The accepted classification is fixed for that decision candle across cache clears and restarts; a conflicting same-candle prediction withholds neural execution. Recorded SMC stops and targets remain monitored. A neural SELL exit requires a newer completed decision candle than the entry confirmation.

The model requires at least 100 consecutive completed 4H candles and frozen volume calibration for the asset. Calibration coverage is read from the validated model's `volume_stats`; BTC, ETH and SOL are default dashboard assets, not the complete calibrated list. All observations used for live inference must follow the model's calibration cutoff, 2022-12-04 15:59:59.999 UTC.

Each selected Settings pair controls its SMC candles and chart through the chosen Deribit-first or Binance feed; supported GEX levels must match that exact pair and price coordinate. Parente independently maps that pair's base asset to its trained Binance/USDT market. Home, paper execution and Positions use one applied model and shared classification cache. Inputs, candle close, probabilities and model identity remain inspectable. Probabilities choose the largest of BUY/HOLD/SELL; they are not probabilities of profit.

## Combined execution

Automatic entries require all of the following:

- A current qualified Perky SMC plan produced by the published Filtered defaults.
- Current Parente BUY on the latest completed Binance 4H candle.
- A fresh exact-pair price from the selected dashboard feed within the plan's stop/entry/target bracket, valid clocks and current monitoring history.
- Available paper cash and the enabled allocation/risk checks.

The Perky SMC plan fixes the recorded stop and TP1–TP3. TP1 and TP2 remain intermediate levels; TP3 is the full execution target. Completed stop-monitoring candles and current quotes continue to protect an open trade during neural outages. A stop precedes a target or neural exit on the same observation. If a completed candle touches stop and target, the stop wins because OHLC cannot establish their intrabar order. A gap below the stop uses the adverse opening price. Entry-containing candles do not retrospectively establish an exit order.

A TP3 hit closes the remaining quantity in full. A later eligible Parente SELL can close it before TP3. There is no TP1 partial exit. BUY uses the selected market's fresh price plus configured slippage; exits use its applicable fresh reference or monitored level minus the recorded slippage. Fees are charged on both sides using the trade's recorded rates. The Deribit index quote is a paper simulation reference, not an executable exchange order book.

With paper allocation limits enabled, position count, per-trade/total risk, allocation, floor, spread and minimum-order checks constrain entries. Disabling those limits allows available-cash sizing while retaining the SMC entry requirement, Parente confirmation and recorded SMC stop/target. Trades preserve their levels and costs when Settings changes. Durable setup/candle identities and transactionally stored notifications prevent repeated scans from duplicating an entry.

Paper alerts identify the pair, Parente artifact and UTC decision candle separately from execution time. Entry messages show class probabilities, recorded fee rate, entry fee and slippage. Exit messages distinguish SMC stop/target exits from a new Parente SELL, and show gross P/L, entry/exit fees and net P/L. A queued entry expires with its entry evidence and is cancelled if the authoritative trade has closed or been deleted; the exit record can still be delivered.

## Manual entries and actual holdings

Manual paper entries can be added between 4H closes, with an explicit price or the fresh Deribit index reference. Their cash budget includes the entry fee. They retain their recorded stop policy; a later completed Parente SELL can close them under the normal manual-entry chronology. Request IDs make retries idempotent. Manual creation neither backdates an order nor closes a recorded holding.

Positions applies the same Parente reading to each holding alongside SMC TP1 and SMC+GEX TP2. An adverse classification or confirmed adverse SMC structure change provides protective guidance, including when the holding is profitable. Saved stops and targets change through their explicit forms. Alerts respect each position's preferences; actual holdings stay open until an exit is recorded.

## Published model and feature mapping

Reference: Parente, Rizzuti and Trerotola, *A profitable trading algorithm for cryptocurrencies using a Neural Network model*, Expert Systems with Applications 238 (2024), 121806. [Research paper](https://doi.org/10.1016/j.eswa.2023.121806), [authors' source/model archive, version 2](https://figshare.com/articles/code/CryptoTrading_zip/22953377/2).

The network is 36 → 128 → 64 → 32 → 3, with LeakyReLU slope 0.01 and softmax. The original label order -1/0/1 maps to BUY/HOLD/SELL. The data-only NPZ contains the published Dense arrays; loading executes neither TensorFlow nor downloaded research scripts.

| Input group | Executable source definition |
| --- | --- |
| Z-score | Log returns with rolling 20-candle mean and sample standard deviation. |
| Oscillators | RSI(14)/100, ULTOSC(7,14,28)/100, BBANDS(5, 2σ) location and simple close percentage change. |
| Volume | Archived per-asset Binance base-volume mean/sample standard deviation, frozen. |
| MA ratios | Relative price/SMA21, SMA21/SMA50, SMA50/SMA100 and price/SMA50 differences. |
| Candlesticks | The original 23 TA-Lib pattern functions, in their original order, divided by 100. |
| Calendar | UTC weekday, month and hour divided by 4. |

The published artifact reconstructs StandardScaler on the source's final-training pool before balancing/splitting, excluding BTCUSDT/ETHUSDT/ALGOUSDT and retaining `pct_change < .24`. The published weights remain fixed. Calibration coverage for an asset is distinct from inclusion in that final-training pool. Frozen normalization, later market conditions and the SMC execution rules mean these paper results do not reproduce the paper's backtest.

Bundled NPZ SHA-256: `8907c6f395dc8cf385619f1cace6e27c3b23a04a820fe980cde65898cad56c87`. License and conversion provenance are retained in `adaptive_crypto/models/NOTICE.md` and the companion JSON.

## Offline inspection

```powershell
.\.venv\Scripts\python.exe -m adaptive_crypto.neural_tools inspect
```

Read-only artifact inspection and standalone Parente simulation are available through `adaptive_crypto.neural_tools --help`. The simulation uses next-open chronology and does not reproduce the combined live SMC strategy. There is no model training or model replacement interface.

## Position currency is independent of model currency

For BTC/USDC in main Settings, SMC and paper/manual positions use BTC/USDC prices, stops and targets, with quantity in BTC. Manual holding P/L is in USDC; paper cash, fees, risks and realized P/L are in USD. Parente still classifies the original Binance BTC/USDT completed four-hour candles. Only its BUY/HOLD/SELL classification confirms or exits the selected-pair plan. Model weights, feature scaling and frozen volume calibration are unchanged. Assets absent from the trained calibration can still show SMC analysis, but Parente remains unavailable rather than inventing a new calibration. DVOL has a separate Deribit-only pair list.
