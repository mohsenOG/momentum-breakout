"""Calculate performance metrics and export an offline report."""

from dataclasses import asdict, fields
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

from .backtest import Signal, StrategyResult, Trade
from .config_test import Config, STRATEGY_NAMES
from .market_data import FIVE_MINUTES, HOUR, History, write_json


METRICS = (
    ("starting_balance", "Starting balance", "money"),
    ("ending_equity", "Ending equity", "money"),
    ("net_pnl", "Net P&L", "money"),
    ("net_return_pct", "Net return", "percent"),
    ("gross_trading_pnl", "Gross realized trading P&L", "money"),
    ("realized_net_pnl", "Net realized cash P&L", "money"),
    ("unrealized_pnl", "Unrealized P&L", "money"),
    ("trading_fees", "Trading fees", "money"),
    ("slippage_cost", "Slippage and tick rounding", "money"),
    ("funding_paid", "Funding paid", "money"),
    ("funding_received", "Funding received", "money"),
    ("net_funding_pnl", "Net funding P&L", "money"),
    ("max_drawdown_pct", "Maximum equity drawdown", "percent"),
    ("completed_trades", "Completed trades", "integer"),
    ("winning_trades", "Winning trades", "integer"),
    ("losing_trades", "Losing trades", "integer"),
    ("win_rate_pct", "Win rate", "percent"),
    ("profit_factor", "Profit factor", "number"),
    ("average_net_pnl", "Average net P&L per completed trade", "money"),
    ("long_trades", "Long entries", "integer"),
    ("short_trades", "Short entries", "integer"),
    ("long_net_pnl", "Net P&L from completed Long trades", "money"),
    ("short_net_pnl", "Net P&L from completed Short trades", "money"),
    ("open_trades", "Open trades at end", "integer"),
    ("max_open_trades", "Maximum simultaneous trades", "integer"),
    ("max_margin_usdt", "Maximum initial margin used", "money"),
    ("max_margin_pct", "Maximum initial margin / equity", "percent"),
    ("rejected_entries", "Rejected entries", "integer"),
    ("liquidation_events", "Liquidation events", "integer"),
    ("liquidation_deficit_covered", "Deficit covered after forced exits", "money"),
)


def drawdown(equity: pd.Series) -> pd.Series:
    return (1 - equity / equity.cummax()) * 100


def summarize(config: Config, result: StrategyResult) -> dict:
    initial = config.account.initial_balance_usdt
    last = result.equity.iloc[-1]
    trades = result.trades
    closed = [trade for trade in trades if trade.exit_timestamp is not None]
    wins = [trade.net_pnl for trade in closed if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in closed if trade.net_pnl < 0]
    factor = sum(wins) / -sum(losses) if losses else ("Infinity" if wins else None)
    return {
        "strategy": result.strategy,
        "starting_balance": initial,
        "ending_equity": float(last.equity),
        "net_pnl": float(last.equity - initial),
        "net_return_pct": float(100 * (last.equity / initial - 1)),
        "gross_trading_pnl": sum(trade.gross_pnl for trade in closed),
        "realized_net_pnl": float(last.wallet_balance - initial),
        "unrealized_pnl": float(last.unrealized_pnl),
        "trading_fees": sum(trade.entry_fee + trade.exit_fee for trade in trades),
        "slippage_cost": sum(trade.entry_slippage + trade.exit_slippage for trade in trades),
        "funding_paid": result.funding_paid,
        "funding_received": result.funding_received,
        "net_funding_pnl": result.funding_received - result.funding_paid,
        "max_drawdown_pct": float(drawdown(result.equity["equity"]).max()),
        "completed_trades": len(closed),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": 100 * len(wins) / len(closed) if closed else None,
        "profit_factor": factor,
        "average_net_pnl": sum(trade.net_pnl for trade in closed) / len(closed) if closed else None,
        "long_trades": sum(trade.side == "Long" for trade in trades),
        "short_trades": sum(trade.side == "Short" for trade in trades),
        "long_net_pnl": sum(trade.net_pnl for trade in closed if trade.side == "Long"),
        "short_net_pnl": sum(trade.net_pnl for trade in closed if trade.side == "Short"),
        "open_trades": int(last.open_trades),
        "max_open_trades": result.max_open_trades,
        "max_margin_usdt": result.max_margin_usdt,
        "max_margin_pct": result.max_margin_pct,
        "rejected_entries": sum(signal.status == "REJECTED" for signal in result.signals),
        "liquidation_events": result.liquidation_events,
        "liquidation_deficit_covered": result.liquidation_deficit_covered,
    }


def display(value, kind: str = "number") -> str:
    if value is None:
        return "N/A"
    if value == "Infinity":
        return "∞"
    if kind == "integer":
        return f"{value:,d}"
    if kind == "percent":
        return f"{value:,.2f}%"
    if kind == "money":
        return f"{value:,.2f} USDT"
    return f"{value:,.2f}"


def utc(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else "—"


def charts(results: list[StrategyResult]) -> list[tuple[str, str]]:
    figures = [go.Figure(), go.Figure(), go.Figure()]
    colors = {"body_extremes": "#6ee7ce", "body_quantiles": "#b4a7ff"}
    for result in results:
        name = STRATEGY_NAMES[result.strategy]
        line = {"color": colors[result.strategy], "width": 2}
        equity = result.equity
        for figure, series in zip(figures, (equity["equity"], -drawdown(equity["equity"]))):
            figure.add_scatter(x=equity["timestamp_utc"], y=series, name=name, mode="lines", line=line)
        closed = sorted(
            (trade for trade in result.trades if trade.exit_timestamp is not None),
            key=lambda trade: trade.exit_timestamp,
        )
        if closed:
            figures[2].add_scatter(
                x=[equity["timestamp_utc"].iloc[0], *(trade.exit_timestamp for trade in closed)],
                y=[0, *pd.Series([trade.net_pnl for trade in closed]).cumsum()],
                name=name, mode="lines", line={**line, "shape": "hv"},
            )
    panels = []
    for figure, label, unit in zip(
        figures, ("Account equity", "Equity drawdown", "Cumulative completed trade P&L"), ("USDT", "%", "USDT"),
    ):
        if not figure.data:
            continue
        figure.update_layout(
            template="plotly_dark", paper_bgcolor="#111a29", plot_bgcolor="#111a29",
            font={"color": "#d2deee"}, height=310, margin={"l": 62, "r": 18, "t": 20, "b": 45},
            legend={"orientation": "h", "y": 1.10}, hovermode="x unified",
        )
        figure.update_yaxes(title=unit, gridcolor="#27364c", zerolinecolor="#536680")
        figure.update_xaxes(gridcolor="#27364c")
        panels.append((label, figure.to_html(
            full_html=False, include_plotlyjs=False,
            config={"displaylogo": False, "responsive": True},
        )))
    return panels


def render_html(
    output: Path, config: Config, history: History, results: list[StrategyResult], summaries: list[dict], created,
) -> None:
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(("html",)),
        trim_blocks=True, lstrip_blocks=True,
    )
    environment.filters.update(display=display, utc=utc)
    account = config.account
    sizing = (
        f"{account.margin_per_trade_usdt:g} USDT target margin per entry"
        if account.position_size_mode == "fixed_margin"
        else f"{account.position_size_fraction_of_equity:.2%} of equity per entry"
    )
    settings = {
        "Trading period (UTC)": f"{history.period.start.date()} to {history.period.last_date}",
        "Cached 5m range (UTC)": (
            f"{utc(history.cached_candle_start)} to {utc(history.cached_period.end - FIVE_MINUTES)}"
        ),
        "Cached 1h range (UTC)": (
            f"{utc(history.cached_hourly_start)} to {utc(history.cached_period.end.floor(HOUR) - HOUR)}"
        ),
        "Signals and fills": config.market.timeframe.upper(),
        "Levels and ATR": config.market.indicator_timeframe.upper(),
        "Position mode": "Cross Margin · Hedge Mode",
        "Leverage": f"{account.leverage:g}×",
        "Position sizing": sizing,
        "Taker fee per fill": f"{account.taker_fee_rate:.3%}",
        "Slippage per fill": f"{account.slippage_rate:.3%} + adverse tick rounding",
        "Historical funding": "Enabled" if account.funding_enabled else "Disabled",
        "Contract metadata fetched (UTC)": utc(pd.Timestamp(history.metadata_fetched_at)),
    }
    trades = sorted((trade for result in results for trade in result.trades), key=lambda trade: trade.entry_timestamp)
    signals = sorted(
        (signal for result in results for signal in result.signals if signal.status == "REJECTED"),
        key=lambda signal: signal.signal_timestamp,
    )
    html = environment.get_template("report_template.html").render(
        symbol=config.market.symbol, contract=history.contract.symbol, created=utc(created),
        settings=settings, summaries=summaries, metrics=METRICS, names=STRATEGY_NAMES,
        trades=trades, rejected=signals, charts=charts(results), plotly_js=get_plotlyjs(),
    )
    output.write_text(html, encoding="utf-8")


def save_results(
    config: Config, history: History, results: list[StrategyResult],
) -> tuple[Path, list[dict]]:
    created = pd.Timestamp.now(tz="UTC")
    name = f"{created.strftime('%Y%m%dT%H%M%S%fZ')}_{config.strategy.strategy_selection}_{uuid4().hex[:8]}"
    root = config.market.results_directory
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    summaries = [summarize(config, result) for result in results]
    with TemporaryDirectory(prefix=".report-", dir=root) as staging:
        output = Path(staging) / name
        output.mkdir()
        if config.report.generate_summary_csv:
            pd.DataFrame(summaries).to_csv(output / "summary.csv", index=False)
        for result in results:
            if config.report.generate_trade_csv:
                columns = [field.name for field in fields(Trade)]
                pd.DataFrame([asdict(trade) for trade in result.trades], columns=columns).to_csv(
                    output / f"trades_{result.strategy}.csv", index=False,
                )
            if config.report.generate_equity_csv:
                equity = result.equity.assign(drawdown_pct=drawdown(result.equity["equity"]))
                equity.to_csv(output / f"equity_{result.strategy}.csv", index=False)
        signal_rows = [asdict(signal) for result in results for signal in result.signals]
        pd.DataFrame(signal_rows, columns=[field.name for field in fields(Signal)]).to_csv(
            output / "signals.csv", index=False,
        )
        write_json(output / "config_snapshot.json", {
            "created_utc": created.isoformat(),
            "configuration": asdict(config),
            "period_start_utc": history.period.start.isoformat(),
            "period_end_exclusive_utc": history.period.end.isoformat(),
            "first_candle_utc": history.candles["kucoin_futures", config.market.timeframe].index[0].isoformat(),
            "first_hourly_candle_utc": history.candles["kucoin_futures", config.market.indicator_timeframe].index[0].isoformat(),
            "history_directory": str(history.source),
            "cache_first_candle_utc": history.cached_candle_start.isoformat(),
            "cache_first_hourly_candle_utc": history.cached_hourly_start.isoformat(),
            "cache_end_exclusive_utc": history.cached_period.end.isoformat(),
            "contract_metadata_fetched_utc": history.metadata_fetched_at,
            "effective_contract": asdict(history.contract),
            "quantile_interpolation": "linear",
        })
        if config.report.generate_html:
            render_html(output / "report.html", config, history, results, summaries, created)
        output.rename(destination)
    return destination, summaries
