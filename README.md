# pyPTA

pyPTA uses the configurable Perky SMC Ultimate V5 Filtered strategy with the published Parente 5/2 neural network for a local paper-trading dashboard. The SMC page shows completed-bar Perky signals, structure and brackets. Positions records your actual holdings, while Paper Trading maintains the simulated account. Settings controls Perky signals, currency pairs, paper limits and optional alert connections.

## Market inputs

The SMC Engine, charts, positions and paper prices share the **exact pair selected in Settings**. Market data can use Deribit first, with Binance spot for pairs absent from Deribit, or Binance spot directly. Active exchange catalogs validate available pairs. Deribit prefers the native perpetual, then native spot. A supported Deribit market with missing or stale candles remains unavailable; it never switches feeds to hide that failure. Enabled pairs may use different quote currencies. Paper cash, equity, fees, risk and realized results use one USD account; entry and exit each capture a fresh observed conversion between the native quote and USD. Defaults are BTC/USDC, ETH/USDC and SOL/USDC.

Live price and paper-fill references use the matching **Deribit composite index** when listed, otherwise the selected Deribit ticker or Binance spot order book. No currency is relabelled or assumed equal to another stablecoin. Composite index references have no executable bid/ask spread. Fees and configured slippage still apply.

**Deribit options GEX** combines complete coin-settled and USDC option chains where listed, in the selected quote currency. Open interest already represents base coins. Calls-positive/puts-negative is a transparent positioning assumption, not observed dealer inventory. Incomplete or stale coverage cannot guide GEX targets.

The **DVOL** page has its own pair list in Settings, saved in `dvol-settings.json`. Only BTC/ETH pairs with a listed Deribit market and matching Deribit index can be added. The official BTC/ETH volatility index is independent of quote currency; 1/7/30-day ranges use that pair's matching current Deribit index. Main pair changes, source changes and account maintenance leave this independent list unchanged.

**Parente 5/2 is the explicit input exception:** it uses its original Binance spot/USDT completed 4H candles, published weights, frozen feature scaler and frozen Binance volume calibration. It maps the selected base asset to its trained Binance pair; SMC, chart and paper references continue to use the selected dashboard pair. For example, a BTC/USDC position uses BTC/USDC entry, stop and targets while its Parente confirmation uses BTC/USDT candles. Manual holding P/L remains in USDC; paper account P/L is converted into USD using captured entry and exit conversion rates. No NN price is substituted into its position plan. No Deribit normalization or fine-tuned neural candidate is active.

## Combined paper strategy

Perky SMC Ultimate V5 is ported from its MPL-2.0 published Pine source. The user supplied a V3 URL; TradingView currently serves V5 at that link. pyPTA replays sweep, internal CHoCH, momentum, configurable score gates, cooldown, selected stop and three-target rules on completed candles. The chart, position context and reachability use the same Perky structure snapshot. Plans are market-quote references, not exchange orders.

An automatic trade requires a current qualified Perky SMC plan **and Parente BUY**. The plan supplies the entry bracket, stop and three Perky targets; the paper position uses TP3 as its full exit target while retaining TP1 and TP2 as intermediate levels. A recorded stop hit, target hit or later Parente SELL closes the position. Stops take priority when a completed candle touches both stop and target; there is no partial target exit. Paper allocation limits can be disabled without disabling SMC qualification or the recorded stop.

Manual paper entries and recorded holdings retain their existing independent workflows, request identities and alert preferences. Guidance never closes an actual holding: its exit is recorded explicitly on Positions. Paper records expose the origin, costs, source pair, SMC levels and neural decision.

See [Neural inputs and execution](docs/NEURAL_STRATEGY.md), [SMC price guide](docs/SMC_PRICE_GUIDE.md), [Perky Pine port](docs/perky_smc_port.md), [Deribit data, GEX and DVOL](docs/DERIBIT_DATA.md) and [SQLite persistence](docs/SQLITE_PERSISTENCE.md).

## AI commentary

Settings supports OpenAI, Google Gemini and DeepSeek, with a separate saved key, model and Home commentary prompt for each provider. For DeepSeek, use `deepseek-flash` or a model listed by your DeepSeek account, such as `deepseek-v4-pro`. Environment configuration uses `AI_PROVIDER=deepseek`, `DEEPSEEK_API_KEY` and optional `DEEPSEEK_MODEL`; the default model for a supplied DeepSeek key is `deepseek-flash`.

Home researches live news and public posts every 15 minutes with each provider's native web search and cites the sources it retrieves. DeepSeek uses the same saved key and model for its native search, with no extra SDK or key, and requests at most three searches in one API turn. Paused or incomplete responses are withheld without an automatic continuation; the dashboard validates reported search usage before publishing. The requested search count is not a provider billing guarantee. If native search omits inline links, the dashboard labels and links actual search sources without inventing which source supports each sentence. News and public-post coverage can be delayed or incomplete. Separate signal commentary stays search-free: it explains the deterministic supplied signal without browsing. AI commentary does not change SMC qualification, neural decisions or paper execution. Check credentials retrieves the account's model list without generating commentary.

## Run from source

For a new Windows checkout:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime-lock.txt
Copy-Item adaptive_crypto_settings.example.json adaptive_crypto_settings.json
.\.venv\Scripts\python.exe -B adaptive_crypto_dashboard.py --host 127.0.0.1 --port 5000
```

This clean installation starts with fresh settings and current-format databases. Earlier strategy settings and databases are not imported. Open `http://127.0.0.1:5000`; save Settings and apply them to activate changes. A process restart reloads edited Python code. The dashboard restart control reloads settings and monitoring within the already running process.

On Ubuntu, create a virtual environment, install `requirements-neural.txt`, and use its Python interpreter with the same dashboard command. Use `/opt/pyPTA` as the service working directory and preserve its deployment credential key and HTTPS configuration. `--once` performs one public-data scan without AI or Telegram delivery.

Python regression files and JavaScript browser checks are grouped in `tests/`. Create a maintainable source archive with `python tools/source_release.py --label parente-smc`; see [release verification](docs/RELEASE_BASELINE.md) for checks and Windows staging.
