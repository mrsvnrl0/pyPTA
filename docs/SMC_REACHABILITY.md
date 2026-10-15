# SMC target reachability

This framework is dashboard logic alongside the Perky SMC strategy port. It evaluates structural support using the same Perky SMC completed-candle structure snapshot; its statuses are not calibrated target-hit probabilities. It does not assume that price movement is deterministic or that inducement increases probability exponentially.

## Data and chronology

The shared selected SMC pair and exchange feed supply both chart candles and higher-timeframe 4H candles. Daily history contains only six consecutive completed UTC 4H bars per day. The first partial day is discarded. Missing bars, missing latest candles, stale/future data and mismatched symbols make the assessment unavailable. Parente continues its original Binance spot/USDT input contract. No currencies are silently substituted.

Higher-timeframe swings and bias use the same causal Perky engine with a separately editable swing length. Only confirmed swings define the dealing range and its 50% equilibrium. A neutral or incomplete range is reported explicitly.

## Evidence

- **Confirmed target:** an existing confirmed untaken SMC liquidity price ahead of the current price. No new target prices are generated.
- **Bias:** higher-timeframe bullish/bearish structure compared with target direction.
- **Premium/discount:** entry depth measured from the favourable edge of the confirmed range. At 50%, longs require discount and shorts premium. Prices outside the range fail this check.
- **ATR envelope:** directional distance from current price to target divided by Wilder higher-timeframe ATR. The default limit is 2 ATR; this is an editable heuristic, without an estimated arrival time.
- **Balance:** last completed higher-timeframe candle inside its predecessor, or the configured multi-bar range no wider than the configured ATR multiple. Targets beyond that observed boundary receive a caution.
- **DOL cycle:** a recent external sweep favours ERL → IRL. A valid internal FVG/OB interaction followed by a same-direction completed BOS/CHoCH favours IRL → ERL. After a new external sweep, the internal interaction must occur again before an external draw is supported. No event produces an Unconfirmed cycle.
- **IRL:** dashboard FVGs use a completed three-bar wick gap and a minimum chart-ATR width. Interaction requires the configured penetration depth; a closed-through invalidation removes the gap. Current Perky order blocks are internal zones.
- **Target classification:** higher-timeframe range boundary/beyond = ERL; a target inside an open FVG/OB = IRL zone; another swing inside the range = Internal swing liquidity. Internal swing pools are not mislabelled as FVGs.
- **Opposing blocks:** active/tested opposite-side order blocks before the target are competing barriers.
- **Inducement:** intermediate untaken swing pools and equal-high/low clusters are shown as context only, without adding a probability boost. Equal clusters require distinct pivot timestamps.

All numerical thresholds, evidence windows, bias timeframe and optional bias/cycle requirements are editable under Settings → SMC target reachability. `Supported` requires all required checks to pass; `Mixed` has at least half passing; `Limited` has fewer. Counts measure agreement between heuristics, not statistical confidence. Missing core inputs yield Unavailable.

## Workflow

Advisory mode adds current assessments to Home, SMC Chart, position TP1/TP2 and active paper exits. Qualified Telegram messages include the assessment and concerns. Perky TP3 is checked against the configured minimum exit distance; reachability controls new qualification only in Require mode. GEX remains separate market context. Existing saved stops/targets are never moved; no positions are closed by the advisory assessment.

Off disables assessments. Require mode additionally filters final-exit candidates and qualification, then rechecks the actual executable price and slippage-adjusted paper fill. It remains a structural heuristic, not a guarantee. Saved positions receive updated assessments without forced exits. Assessments expire at the candle boundary or 30-second quote freshness bound and their old evidence is hidden in the browser.

Perky strategy source: https://www.tradingview.com/script/nqauG2wM-Perky-SMC-Ultimate-V3/ .
Research context supplied by the user: https://innercircletrader.net/tutorials/ict-daily-bias-explained/ and https://tradingwyckoff.com/en/smart-money-concepts/. These narrative conventions do not establish a calibrated statistical target-hit model.
