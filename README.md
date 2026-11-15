# Momentum Breakout Backtest

Historical BTC/USDT backtesting with 1H support and resistance, 5M breakouts, and four-market 5M volume confirmation. All fills and stop checks use KuCoin Futures `XBTUSDTM` 5M candles. ATR uses completed KuCoin Futures 1H candles.

## Setup and run

Requires Python 3.11 or newer. Install dependencies from the repository root, then run the script from `backtest/`.

```sh
python -m pip install -r requirements.txt
cd backtest
python main_test.py
```

A virtual environment is optional. To use one, run `python -m venv .venv` from the repository root and activate it before installing dependencies. Use `.\.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` in a POSIX shell.

From the repository root, `python -m backtest.main_test` also launches the menu.

- Choose **1** to enter inclusive start and end dates in UTC. The downloader prepares four 5M candle series, two 1H candle series, KuCoin Futures funding when enabled, and contract metadata. It reuses valid cached data and downloads missing ranges, including warm-up candles.
- Choose **2** to replay a complete saved period offline. Keep its date-stamped folder under `backtest/history/` with the CSVs, `manifest.json`, and `contract.json`. A missing candle, insufficient warm-up, or incomplete required funding stops the run with an error. For an hourly-only cache, choose **1** to reuse its Binance Spot and KuCoin Futures hourly candles and fetch the missing 5M data. Choose **3** to exit.
- Edit the settings in `backtest/config_test.py`. Set `strategy_selection` to `body_extremes`, `body_quantiles`, or `both`. Each strategy starts with its own balance. The configuration also controls sizing, stop distances, costs, funding, paths, and report outputs.
- Each run writes a new folder under `backtest/results/`. Open `report.html` in a browser. The CSV exports contain strategy summaries, trades, equity and margin, and signal outcomes. `config_snapshot.json` records the effective configuration and contract details.

## Public data sources

| Series | Resolution | HTTP GET |
| --- | --- | --- |
| Binance Spot candles | 5M and 1H | `https://data-api.binance.vision/api/v3/klines` |
| Binance USDT-M Futures candles | 5M | `https://fapi.binance.com/fapi/v1/klines` |
| KuCoin Spot candles | 5M | `https://api.kucoin.com/api/v1/market/candles` |
| KuCoin Futures candles | 5M and 1H | `https://api-futures.kucoin.com/api/v1/kline/query` |
| KuCoin Futures settlements | Settlement timestamps | `https://api-futures.kucoin.com/api/v1/contract/funding-rates` |
| KuCoin Futures contract metadata | Snapshot | `https://api-futures.kucoin.com/api/v1/contracts/XBTUSDTM` |

Exchange history limits still apply. Funding checks use the returned settlement timestamps and reject gaps longer than `max_funding_gap_hours`. The default eight-hour upper bound permits shorter schedules. A missing settlement inside that bound may be indistinguishable from a schedule change in the public response. The cache records the funding file checksum to detect incomplete copies.

## Simulation assumptions

- Levels use the previous 20 completed Binance Spot 1H candle bodies by default. Body Quantiles uses linear interpolation. Each 5M candle uses the levels active at its open for both prices in the crossing. For example, the 09:55 candle uses the 09:00 levels and the 10:00 candle uses the updated levels.
- Each market compares its completed 5M volume with its own prior 20 candles. By default, at least three markets must exceed 1.5 times their averages. A qualifying signal fills at the next KuCoin Futures 5M open. The final 5M candle can produce a recorded signal with no next open for execution.
- At each 5M open the simulator settles funding, checks liquidation, processes gap stops, and attempts the pending entry in that order. Initial stops apply immediately. Between the open and close, the price path visits the low first when net Long or flat and the high first when net Short. It then visits the other extreme and the close. Intrabar exits use the 5M close as their timestamp upper bound and record an `exit_phase`. Entries and trailing stop updates use the most recently completed 1H ATR. A trailing stop update takes effect on the following 5M candle.
- Entry, stop, liquidation, and final fills use taker fees and adverse slippage and tick rounding. The trade ledger records fees and slippage separately. Final open trades are closed by default. If final closing is disabled, the ledger reports their unrealized P&L separately.
- Funding uses the KuCoin Futures candle open as the settlement mark proxy. A settlement exactly at an open is applied to carried positions before any fills. A settlement between 5M opens uses the current 5M candle’s opening price and is processed before intrabar price events, whose timing is unknown. The account pays or receives on net exposure. The simulator allocates that net amount across virtual trades on the larger side in proportion to their contract quantities.
- Cross Hedge is the supported account mode. Initial and maintenance margin use the larger marked side. Equity includes unrealized P&L. Available initial margin is equity less used initial margin, floored at zero. A new entry must cover its fee, opening slippage, and projected margin. Contract constraints are taken from the cached metadata and configuration. The metadata is a download-time snapshot, so it can differ from historical exchange settings.
- Liquidation is estimated from 5M trade prices and the assumed intrabar path. When equity reaches estimated maintenance margin plus closing fees, the simulator closes the account’s positions and records the event. A deficit after exits is recorded separately and the wallet is floored at zero. Peak margin usage includes simulated intrabar marks. Drawdown uses 5M equity marks and the starting balance.
