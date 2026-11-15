"""Run the real cache, simulation, and reporting pipeline offline."""

from dataclasses import replace
from html.parser import HTMLParser
from math import isclose
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pandas as pd

from .config_test import Config, STRATEGIES
from .main_test import run_backtest
from .market_data import DataError, HOUR, Period, discover_cache, load_history, save_history


def synthetic_history(config: Config, *, gap: bool = False) -> Path:
    period = Period.from_dates("2024-01-02", "2024-01-02")
    first = period.start - config.strategy.warmup_hours * HOUR
    timestamps = pd.date_range(first, period.end, freq="h", inclusive="left", name="timestamp_utc")
    breakouts = {0: 110, 2: 120, 4: 90, 6: 130, 12: 80, 16: 150, 18: 160, 21: 145, 23: 75}
    futures_events = {
        9: (1000, 1012, 998, 1009),
        13: (1000, 1020, 998, 1000),
    }
    if gap:
        futures_events[2] = (100, 110, 90, 100)
    candles = {}
    for market_number, name in enumerate(config.market.symbols):
        rows = []
        for timestamp in timestamps:
            hour = (timestamp - period.start) // HOUR
            if name == "kucoin_futures":
                opening, high, low, close = futures_events.get(hour, (1000, 1002, 998, 1000))
            else:
                opening = 100
                close = breakouts.get(hour, opening)
                high, low = max(opening, close) + 2, min(opening, close) - 2
            normal = 10.0 ** market_number
            confirms = hour in breakouts and market_number < (2 if hour == 16 else 3)
            rows.append((opening, high, low, close, normal * (4 if confirms else 1)))
        candles[name] = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=timestamps)

    funding = pd.Series(
        [0.001, 0.001, 0.002, -0.001, -0.001, 0.001],
        index=period.start + pd.to_timedelta([0, 300, 390, 480, 960, 1380], unit="min"), name="funding_rate",
    )
    metadata = {
        "fetched_at": period.start.isoformat(),
        "exchange_metadata": {
            "symbol": "XBTUSDTM", "isInverse": False, "settleCurrency": "USDT",
            "lotSize": 1, "tickSize": 0.1, "multiplier": 0.01, "initialMargin": 0.008,
            "maintainMargin": 0.004, "maxLeverage": 125, "marketMaxOrderQty": 1_000_000,
        },
    }
    return save_history(config, period, first, candles, funding, metadata)


class ReportHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.charts = 0
        self.external_resources = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if tag == "div" and "plotly-graph-div" in attributes.get("class", "").split():
            self.charts += 1
        if tag in ("script", "link", "img") and ("src" in attributes or "href" in attributes):
            self.external_resources.append(attributes)


def check_results(folder, summaries, results, history, config, *, open_at_end: bool = False):
    expected = {"summary.csv", "signals.csv", "config_snapshot.json", "report.html"}
    for strategy in config.strategy.selected:
        expected.update({f"trades_{strategy}.csv", f"equity_{strategy}.csv"})
    assert {file.name for file in folder.iterdir()} == expected, "Unexpected report files"
    assert len(pd.read_csv(folder / "summary.csv")) == len(results), "Missing summary rows"
    exported_signals = pd.read_csv(folder / "signals.csv")
    assert len(exported_signals) == sum(len(result.signals) for result in results), "Missing signal records"

    html = ReportHTML()
    html.feed((folder / "report.html").read_text(encoding="utf-8"))
    assert html.charts == 3 and not html.external_resources, "Charts must be embedded offline"
    assert {"trade-history", "trade-filter"} <= html.ids, "Trade table or filter missing"

    for summary, result in zip(summaries, results):
        assert result.trades, f"{result.strategy} produced no trades"
        assert {trade.side for trade in result.trades} == {"Long", "Short"}, "Both sides must trade"
        reasons = {trade.exit_reason for trade in result.trades}
        assert {"INITIAL_STOP", "TRAILING_STOP"} <= reasons, reasons
        assert result.max_open_trades >= 3, "Concurrent trades were not recorded"
        assert {"INSUFFICIENT_AVAILABLE_MARGIN", "NO_NEXT_EXECUTION_CANDLE"} <= {
            signal.rejection_reason for signal in result.signals
        }, "Expected rejected signals"
        assert all(signal.confirming_markets == 3 for signal in result.signals), "Volume decision was wrong"
        assert not any(signal.signal_candle_open == history.period.start + 16 * HOUR for signal in result.signals)
        assert all(signal.status in ("REJECTED", "FILLED") for signal in result.signals)
        assert result.funding_paid > 0 and result.funding_received > 0, "Funding did not flow both ways"
        overlapping = result.equity.query("long_contracts > 0 and short_contracts > 0")
        assert not overlapping.empty, "Opposite positions must coexist"
        larger_side = overlapping[["long_notional", "short_notional"]].max(axis=1)
        assert np.allclose(overlapping["used_initial_margin"], larger_side / config.account.leverage)
        assert np.isfinite(result.equity.select_dtypes(include="number")).all().all()
        assert len(result.equity) == 25, "Warm-up candles leaked into the equity series"
        ledger = pd.read_csv(folder / f"trades_{result.strategy}.csv")
        assert len(ledger) == len(result.trades) and ledger.trade_id.is_unique
        assert len(pd.read_csv(folder / f"equity_{result.strategy}.csv")) == len(result.equity)
        for trade in result.trades:
            futures_open = history.candles["kucoin_futures"].at[trade.entry_timestamp, "open"]
            assert trade.signal_timestamp == trade.entry_timestamp, "Entry missed the next open"
            assert trade.direction * (trade.entry_price - futures_open) > 0, "Entry did not use adverse KuCoin slippage"
            assert trade.contract_quantity >= history.contract.minimum_order_quantity
            assert isclose(trade.btc_quantity, trade.contract_quantity * history.contract.multiplier)
            assert isclose(trade.entry_fee, trade.entry_price * trade.btc_quantity * config.account.taker_fee_rate)
            if trade.exit_timestamp is not None:
                assert trade.exit_slippage > 0 and trade.exit_fee > 0
                assert isclose(trade.net_pnl, trade.gross_pnl - trade.entry_fee - trade.exit_fee + trade.funding_pnl)
        assert isclose(sum(trade.funding_pnl for trade in result.trades), summary["net_funding_pnl"], abs_tol=1e-9)
        cash = summary["gross_trading_pnl"] - summary["trading_fees"] + summary["net_funding_pnl"]
        assert isclose(summary["realized_net_pnl"], cash, abs_tol=1e-9)
        assert isclose(summary["net_pnl"], cash + summary["unrealized_pnl"], abs_tol=1e-9)
        assert (summary["open_trades"] > 0) == open_at_end, "End-of-period position policy was ignored"
        assert ("END_OF_BACKTEST" in reasons) != open_at_end, "Final trade exit status is incorrect"


def smoke() -> None:
    with TemporaryDirectory(prefix="momentum-smoke-") as staging:
        base = Path(staging)
        defaults = Config()
        config = replace(
            defaults, market=replace(defaults.market, data_directory=base / "history", results_directory=base / "results"),
            account=replace(defaults.account, initial_balance_usdt=230.0),
        )
        with patch("requests.Session.request", side_effect=AssertionError("Offline smoke run attempted HTTP")):
            cache = synthetic_history(config)
            choices = discover_cache(config)
            assert len(choices) == 1 and choices[0].path == cache, "Cache discovery failed"
            history = load_history(choices[0].path, config)
            assert all(frame.index.equals(history.candles["binance_spot"].index) for frame in history.candles.values())
            funding_path = cache / "funding.csv"
            original = funding_path.read_bytes()
            funding_path.write_bytes(b"\n".join(original.splitlines()[:-1]))
            try:
                load_history(cache, config)
            except DataError as error:
                assert "validated download" in str(error), str(error)
            else:
                raise AssertionError("Incomplete funding cache was accepted")
            funding_path.write_bytes(original)

            folder, summaries, results = run_backtest(config, history)
            assert tuple(result.strategy for result in results) == STRATEGIES
            assert len(results[1].trades) > len(results[0].trades), "Quantile-only breakout was not filled"
            check_results(folder, summaries, results, history, config)

            single_strategy = replace(
                config, strategy=replace(config.strategy, strategy_selection="body_quantiles", close_open_positions_at_end=False),
            )
            second_folder, summaries, results = run_backtest(single_strategy, history)
            assert folder != second_folder and folder.is_dir(), "A run overwrote existing results"
            check_results(second_folder, summaries, results, history, single_strategy, open_at_end=True)

            gap_history = load_history(synthetic_history(config, gap=True), config)
            _, summaries, results = run_backtest(config, gap_history)
            for summary, result in zip(summaries, results):
                assert result.liquidation_events == 1 and len(result.trades) == 1
                assert result.trades[0].exit_reason == "LIQUIDATION"
                assert result.trades[0].exit_phase == "OPEN" and summary["ending_equity"] == 0
                assert summary["liquidation_deficit_covered"] > 0
                assert result.equity["equity"].min() == 0

            no_fill = replace(config, strategy=replace(config.strategy, required_confirming_markets=4))
            empty_folder, summaries, results = run_backtest(no_fill, history)
            assert all(summary["ending_equity"] == 230.0 for summary in summaries)
            assert all(not result.trades and not result.signals for result in results)
            assert pd.read_csv(empty_folder / "trades_body_extremes.csv").empty
            empty_html = ReportHTML()
            empty_html.feed((empty_folder / "report.html").read_text(encoding="utf-8"))
            assert empty_html.charts == 2 and "trade-history" not in empty_html.ids


if __name__ == "__main__":
    try:
        smoke()
    except Exception:
        import traceback

        traceback.print_exc()
        print("FAIL: Offline backtest smoke run")
        raise SystemExit(1)
    print("PASS: Offline cache, both strategies, Cross Hedge, funding, exits, and report exports")
