# Momentum Breakout Backtest

Historical BTC/USDT backtesting on 1H candles. Binance Spot provides the breakout reference. Binance Spot, Binance USDT-M Futures, KuCoin Spot, and KuCoin USDT-M Futures provide volume confirmation. All simulated orders use KuCoin Futures `XBTUSDTM`.

## Setup and run

Requires Python 3.11 or newer. Install dependencies from the repository root, then run the script from `backtest/`.

```sh
python -m pip install -r requirements.txt
cd backtest
python main_test.py
```

A virtual environment is optional. To use one, run `python -m venv .venv` from the repository root and activate it before installing dependencies. Use `.\.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` in a POSIX shell.

From the repository root, `python -m backtest.main_test` also launches the menu.

- Choose **1** to enter inclusive start and end dates in UTC. The downloader fetches the four candle series, KuCoin Futures settlements when enabled, and contract metadata. It includes prior candles for indicator warm-up and requires every candle in the requested range.
- Choose **2** to select a saved period. The replay only reads local files. A missing candle, insufficient warm-up, or incomplete required funding stops the run with an error. Keep the date-stamped folder under `backtest/history/` with its canonical CSVs, `manifest.json`, and `contract.json`. Option 1 reuses a compatible folder when it covers the full request.
- Edit the settings in `backtest/config_test.py`. Set `strategy_selection` to `body_extremes`, `body_quantiles`, or `both`. Each strategy starts with its own balance. The configuration also controls sizing, stop distances, costs, funding, paths, and report outputs.
- Each run writes a new folder under `backtest/results/`. Open `report.html` in a browser. The CSV exports contain strategy summaries, trades, equity and margin, and signal outcomes. `config_snapshot.json` records the effective configuration and contract details.

## Public data sources

| Series | HTTP GET |
| --- | --- |
| Binance Spot candles | `https://data-api.binance.vision/api/v3/klines` |
| Binance USDT-M Futures candles | `https://fapi.binance.com/fapi/v1/klines` |
| KuCoin Spot candles | `https://api.kucoin.com/api/v1/market/candles` |
| KuCoin Futures candles | `https://api-futures.kucoin.com/api/v1/kline/query` |
| KuCoin Futures settlements | `https://api-futures.kucoin.com/api/v1/contract/funding-rates` |
| KuCoin Futures contract metadata | `https://api-futures.kucoin.com/api/v1/contracts/XBTUSDTM` |

Exchange history limits still apply. Funding checks use the returned settlement timestamps and reject gaps longer than `max_funding_gap_hours`. The default eight-hour upper bound permits shorter schedules. A missing settlement inside that bound may be indistinguishable from a schedule change in the public response. The cache records the funding file checksum to detect incomplete copies.

## Simulation assumptions

- Signals use completed Binance Spot candle bodies and prior-window levels. Body Quantiles uses linear interpolation between sorted values. Each venue’s volume is compared with its own prior average. A qualifying signal is dated at candle close and fills at the next KuCoin Futures open. A signal on the final candle is recorded as rejected.
- At each open the simulator settles funding, checks liquidation, processes gap stops, and attempts the pending entry in that order. Initial stops apply immediately. Between the open and close, the price path visits the low first when net Long or flat and the high first when net Short. It then visits the other extreme and the close. Intrabar exits use the candle close as their timestamp upper bound and record an `exit_phase`. Trailing stops use closed-candle ATR and take effect on the next candle.
- Entry, stop, liquidation, and final fills use taker fees and adverse slippage and tick rounding. The trade ledger records fees and slippage separately. Final open trades are closed by default. If final closing is disabled, the ledger reports their unrealized P&L separately.
- Funding uses the KuCoin Futures candle open as the settlement mark proxy. A settlement exactly at an open is applied to carried positions before any fills. An off-hour settlement uses that hour’s open and is processed before intrabar price events, whose timing is unknown. The account pays or receives on net exposure. The simulator allocates that net amount across virtual trades on the larger side in proportion to their contract quantities.
- Cross Hedge is the supported account mode. Initial and maintenance margin use the larger marked side. Equity includes unrealized P&L. Available initial margin is equity less used initial margin, floored at zero. A new entry must cover its fee, opening slippage, and projected margin. Contract constraints are taken from the cached metadata and configuration. The metadata is a download-time snapshot, so it can differ from historical exchange settings.
- Liquidation is estimated from hourly trade prices and the assumed intrabar path. When equity reaches estimated maintenance margin plus closing fees, the simulator closes the account’s positions and records the event. A deficit after exits is recorded separately and the wallet is floored at zero. Peak margin usage includes simulated intrabar marks. Drawdown uses hourly equity marks and the starting balance.
