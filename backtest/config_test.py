"""Edit the defaults in this module to configure the backtest."""

from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
import re


PACKAGE_DIRECTORY = Path(__file__).resolve().parent
STRATEGIES = ("body_extremes", "body_quantiles")
STRATEGY_NAMES = {
    "body_extremes": "Body Extremes",
    "body_quantiles": "Body Quantiles",
}


@dataclass(frozen=True)
class MarketSettings:
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    binance_spot_symbol: str = "BTCUSDT"
    binance_futures_symbol: str = "BTCUSDT"
    kucoin_spot_symbol: str = "BTC-USDT"
    kucoin_futures_symbol: str = "XBTUSDTM"
    data_directory: Path = PACKAGE_DIRECTORY / "history"
    results_directory: Path = PACKAGE_DIRECTORY / "results"
    request_timeout: float = 15.0
    retry_count: int = 4
    retry_backoff: float = 1.0
    retry_max_delay: float = 30.0
    cache_enabled: bool = True

    @property
    def symbols(self) -> dict[str, str]:
        return {
            "binance_spot": self.binance_spot_symbol,
            "binance_futures": self.binance_futures_symbol,
            "kucoin_spot": self.kucoin_spot_symbol,
            "kucoin_futures": self.kucoin_futures_symbol,
        }


@dataclass(frozen=True)
class StrategySettings:
    strategy_selection: str = "both"
    breakout_lookback: int = 20
    upper_quantile: float = 0.90
    lower_quantile: float = 0.10
    volume_lookback: int = 20
    volume_threshold: float = 1.5
    required_confirming_markets: int = 3
    atr_period: int = 14
    initial_stop_atr_multiplier: float = 2.0
    trailing_stop_atr_multiplier: float = 2.0
    close_open_positions_at_end: bool = True

    @property
    def selected(self) -> tuple[str, ...]:
        return STRATEGIES if self.strategy_selection == "both" else (self.strategy_selection,)

    @property
    def warmup_hours(self) -> int:
        return max(self.breakout_lookback, self.volume_lookback, self.atr_period)


@dataclass(frozen=True)
class AccountSettings:
    initial_balance_usdt: float = 1000.0
    leverage: float = 3.0
    position_mode: str = "hedge"
    margin_mode: str = "cross"
    position_size_mode: str = "fixed_margin"
    margin_per_trade_usdt: float = 100.0
    position_size_fraction_of_equity: float = 0.10
    max_open_trades: int | None = None
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0006
    slippage_rate: float = 0.0005
    funding_enabled: bool = True
    max_funding_gap_hours: int = 8

    # None uses KuCoin contract metadata. The minimum defaults to the lot size
    # when the exchange does not return a separate minimum quantity.
    contract_multiplier: float | None = None
    lot_size: int | None = None
    minimum_order_quantity: int | None = None
    tick_size: float | None = None
    maintenance_margin_rate: float | None = None


@dataclass(frozen=True)
class ReportSettings:
    generate_html: bool = True
    generate_summary_csv: bool = True
    generate_trade_csv: bool = True
    generate_equity_csv: bool = True


@dataclass(frozen=True)
class Config:
    market: MarketSettings = field(default_factory=MarketSettings)
    strategy: StrategySettings = field(default_factory=StrategySettings)
    account: AccountSettings = field(default_factory=AccountSettings)
    report: ReportSettings = field(default_factory=ReportSettings)

    def validate(self) -> None:
        market, strategy, account = self.market, self.strategy, self.account
        if not isinstance(market.data_directory, Path) or not isinstance(market.results_directory, Path):
            raise ValueError("data_directory and results_directory must be pathlib.Path values.")
        for group, names in (
            (market, ("cache_enabled",)),
            (strategy, ("close_open_positions_at_end",)),
            (account, ("funding_enabled",)),
            (self.report, ("generate_html", "generate_summary_csv", "generate_trade_csv", "generate_equity_csv")),
        ):
            for name in names:
                if type(getattr(group, name)) is not bool:
                    raise ValueError(f"{name} must be True or False.")
        if market.timeframe != "1h":
            raise ValueError("Only the 1h timeframe is supported.")
        if not market.symbol.strip() or any(
            not re.fullmatch(r"[A-Z0-9-]+", symbol) for symbol in market.symbols.values()
        ):
            raise ValueError("Set a display symbol and valid exchange symbols.")
        if strategy.strategy_selection not in (*STRATEGIES, "both"):
            raise ValueError("strategy_selection must be both, body_extremes, or body_quantiles.")
        if (account.position_mode, account.margin_mode) != ("hedge", "cross"):
            raise ValueError("The supported combination is position_mode='hedge' and margin_mode='cross'.")
        if account.position_size_mode not in ("fixed_margin", "percentage_of_equity"):
            raise ValueError("position_size_mode must be fixed_margin or percentage_of_equity.")

        for name, value, minimum in (
            ("retry_count", market.retry_count, 0),
            ("breakout_lookback", strategy.breakout_lookback, 1),
            ("volume_lookback", strategy.volume_lookback, 1),
            ("atr_period", strategy.atr_period, 1),
            ("required_confirming_markets", strategy.required_confirming_markets, 1),
            ("max_funding_gap_hours", account.max_funding_gap_hours, 1),
            ("max_open_trades", account.max_open_trades, 1),
            ("lot_size", account.lot_size, 1),
            ("minimum_order_quantity", account.minimum_order_quantity, 1),
        ):
            if value is None and name in ("max_open_trades", "lot_size", "minimum_order_quantity"):
                continue
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}.")
        if strategy.required_confirming_markets > len(market.symbols):
            raise ValueError("required_confirming_markets cannot exceed four.")

        for name, value in (
            ("request_timeout", market.request_timeout),
            ("retry_backoff", market.retry_backoff),
            ("retry_max_delay", market.retry_max_delay),
            ("volume_threshold", strategy.volume_threshold),
            ("initial_stop_atr_multiplier", strategy.initial_stop_atr_multiplier),
            ("trailing_stop_atr_multiplier", strategy.trailing_stop_atr_multiplier),
            ("initial_balance_usdt", account.initial_balance_usdt),
            ("margin_per_trade_usdt", account.margin_per_trade_usdt),
            ("contract_multiplier", account.contract_multiplier),
            ("tick_size", account.tick_size),
        ):
            if value is None and name in ("contract_multiplier", "tick_size"):
                continue
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite.")
        if not isfinite(account.leverage) or account.leverage < 1:
            raise ValueError("leverage must be finite and at least 1.")
        if not 0 < account.position_size_fraction_of_equity <= 1:
            raise ValueError("position_size_fraction_of_equity must be greater than 0 and at most 1.")
        if not 0 <= strategy.lower_quantile < strategy.upper_quantile <= 1:
            raise ValueError("Quantiles must be ordered and within [0, 1].")
        for name in ("maker_fee_rate", "taker_fee_rate", "slippage_rate", "maintenance_margin_rate"):
            value = getattr(account, name)
            if value is None and name == "maintenance_margin_rate":
                continue
            if not 0 <= value < 1:
                raise ValueError(f"{name} must be in [0, 1).")
        if market.retry_backoff > market.retry_max_delay:
            raise ValueError("retry_backoff cannot exceed retry_max_delay.")


CONFIG = Config()
