"""Chronological KuCoin Futures simulation with independent Cross Hedge accounts."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite

import pandas as pd

from .config_test import AccountSettings, Config
from .market_data import Contract, FIVE_MINUTES, History, HOUR, Progress


@dataclass
class Signal:
    strategy: str
    side: str
    signal_timestamp: pd.Timestamp
    signal_candle_open: pd.Timestamp
    breakout_level: float
    reference_close: float
    atr: float
    confirming_markets: int
    binance_spot_volume_ratio: float
    binance_futures_volume_ratio: float
    kucoin_spot_volume_ratio: float
    kucoin_futures_volume_ratio: float
    status: str = "PENDING"
    rejection_reason: str = ""
    trade_id: str = ""


@dataclass
class Trade:
    trade_id: str
    strategy: str
    side: str
    signal_timestamp: pd.Timestamp
    entry_timestamp: pd.Timestamp
    entry_price: float
    contract_quantity: int
    btc_quantity: float
    position_notional: float
    allocated_margin: float
    leverage: float
    initial_stop: float
    current_trailing_stop: float
    entry_fee: float
    entry_slippage: float
    extreme_since_entry: float
    exit_timestamp: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    exit_phase: str = ""
    exit_fee: float = 0.0
    exit_slippage: float = 0.0
    funding_pnl: float = 0.0
    gross_pnl: float | None = None
    net_pnl: float | None = None
    unrealized_pnl: float = 0.0

    @property
    def direction(self) -> int:
        return 1 if self.side == "Long" else -1

    def pnl_at(self, price: float) -> float:
        return self.direction * (price - self.entry_price) * self.btc_quantity


@dataclass(frozen=True)
class Valuation:
    long_contracts: int
    short_contracts: int
    long_notional: float
    short_notional: float
    used_initial_margin: float
    maintenance_margin: float
    estimated_closing_fees: float
    available_margin: float
    wallet_balance: float
    unrealized_pnl: float
    equity: float


@dataclass
class StrategyResult:
    strategy: str
    trades: list[Trade]
    signals: list[Signal]
    equity: pd.DataFrame
    funding_paid: float
    funding_received: float
    max_open_trades: int
    max_margin_usdt: float
    max_margin_pct: float
    liquidation_events: int
    liquidation_deficit_covered: float


def price_tick(price: Decimal, tick_size: float, *, up: bool) -> float:
    tick = Decimal(str(tick_size))
    ticks = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return float(max(1, ticks) * tick)


class Account:
    def __init__(self, settings: AccountSettings, contract: Contract):
        self.settings = settings
        self.contract = contract
        self.wallet = settings.initial_balance_usdt
        self.initial_margin_rate = max(1 / settings.leverage, contract.initial_margin_rate)
        self.open_trades: list[Trade] = []
        self.trades: list[Trade] = []
        self.funding_paid = 0.0
        self.funding_received = 0.0
        self.liquidation_events = 0
        self.liquidation_deficit_covered = 0.0
        self.max_open_trades = 0
        self.max_margin_usdt = 0.0
        self.max_margin_pct = 0.0

    def value(self, price: float) -> Valuation:
        long = short = 0
        unrealized = 0.0
        for trade in self.open_trades:
            if trade.direction == 1:
                long += trade.contract_quantity
            else:
                short += trade.contract_quantity
            unrealized += trade.pnl_at(price)
        contract_value = price * self.contract.multiplier
        long_notional, short_notional = long * contract_value, short * contract_value
        larger_side = max(long_notional, short_notional)
        initial = larger_side * self.initial_margin_rate
        equity = self.wallet + unrealized
        return Valuation(
            long, short, long_notional, short_notional, initial,
            larger_side * self.contract.maintenance_margin_rate,
            (long_notional + short_notional) * self.settings.taker_fee_rate,
            max(0.0, equity - initial), self.wallet, unrealized, equity,
        )

    def observe(self, value: Valuation) -> None:
        self.max_open_trades = max(self.max_open_trades, len(self.open_trades))
        self.max_margin_usdt = max(self.max_margin_usdt, value.used_initial_margin)
        if value.equity > 0:
            self.max_margin_pct = max(self.max_margin_pct, 100 * value.used_initial_margin / value.equity)

    def market_fill(self, reference: float, direction: int) -> float:
        slipped = Decimal(str(reference)) * (1 + direction * Decimal(str(self.settings.slippage_rate)))
        return price_tick(slipped, self.contract.tick_size, up=direction == 1)

    def enter(self, signal: Signal, timestamp: pd.Timestamp, opening_price: float, stop_multiplier: float) -> None:
        settings, contract = self.settings, self.contract
        value = self.value(opening_price)
        if value.equity <= 0:
            reject(signal, "ACCOUNT_EQUITY_EXHAUSTED")
            return
        if settings.max_open_trades is not None and len(self.open_trades) >= settings.max_open_trades:
            reject(signal, "MAX_OPEN_TRADES")
            return
        direction = 1 if signal.side == "Long" else -1
        price = self.market_fill(opening_price, direction)
        budget = (
            settings.margin_per_trade_usdt if settings.position_size_mode == "fixed_margin"
            else value.equity * settings.position_size_fraction_of_equity
        )
        target = Decimal(str(budget)) * Decimal(str(settings.leverage))
        lot_value = Decimal(str(price)) * Decimal(str(contract.multiplier)) * contract.lot_size
        quantity = int(target // lot_value) * contract.lot_size
        if quantity < contract.minimum_order_quantity:
            reject(signal, "BELOW_MINIMUM_ORDER_QUANTITY")
            return
        if quantity > contract.market_max_order_quantity:
            reject(signal, "ABOVE_MARKET_MAX_ORDER_QUANTITY")
            return

        btc = quantity * contract.multiplier
        notional = btc * price
        fee = notional * settings.taker_fee_rate
        long = value.long_notional + (btc * opening_price if direction == 1 else 0)
        short = value.short_notional + (btc * opening_price if direction == -1 else 0)
        post_fill_equity = value.equity - fee + direction * (opening_price - price) * btc
        larger_side = max(long, short)
        maintenance = larger_side * contract.maintenance_margin_rate + (long + short) * settings.taker_fee_rate
        if post_fill_equity < larger_side * self.initial_margin_rate or post_fill_equity <= maintenance:
            reject(signal, "INSUFFICIENT_AVAILABLE_MARGIN")
            return

        stop = price_tick(
            Decimal(str(price)) - direction * Decimal(str(signal.atr)) * Decimal(str(stop_multiplier)),
            contract.tick_size, up=direction == -1,
        )
        trade = Trade(
            f"{signal.strategy}-{len(self.trades) + 1:06d}", signal.strategy, signal.side,
            signal.signal_timestamp, timestamp, price, quantity, btc, notional,
            notional * self.initial_margin_rate, settings.leverage, stop, stop,
            fee, direction * (price - opening_price) * btc, opening_price,
        )
        self.wallet -= fee
        self.trades.append(trade)
        self.open_trades.append(trade)
        self.observe(self.value(opening_price))
        signal.status, signal.trade_id = "FILLED", trade.trade_id

    def exit(self, trade: Trade, timestamp: pd.Timestamp, reference: float, reason: str, phase: str) -> None:
        price = self.market_fill(reference, -trade.direction)
        trade.exit_timestamp = timestamp
        trade.exit_price = price
        trade.exit_reason = reason
        trade.exit_phase = phase
        trade.exit_fee = price * trade.btc_quantity * self.settings.taker_fee_rate
        trade.exit_slippage = trade.direction * (reference - price) * trade.btc_quantity
        trade.gross_pnl = trade.pnl_at(price)
        trade.net_pnl = trade.gross_pnl - trade.entry_fee - trade.exit_fee + trade.funding_pnl
        self.wallet += trade.gross_pnl - trade.exit_fee
        self.open_trades.remove(trade)

    def settle_funding(self, price: float, rate: float) -> None:
        net_contracts = sum(trade.direction * trade.contract_quantity for trade in self.open_trades)
        if not net_contracts:
            return
        amount = -net_contracts * self.contract.multiplier * price * rate
        self.wallet += amount
        self.funding_paid += max(0.0, -amount)
        self.funding_received += max(0.0, amount)
        larger_side = [trade for trade in self.open_trades if trade.direction * net_contracts > 0]
        same_side_quantity = sum(trade.contract_quantity for trade in larger_side)
        for trade in larger_side:
            trade.funding_pnl += amount * trade.contract_quantity / same_side_quantity

    def risk_buffer(self, value: Valuation) -> float:
        return value.equity - value.maintenance_margin - value.estimated_closing_fees

    def cover_deficit(self) -> None:
        if self.wallet < 0 and not self.open_trades:
            self.liquidation_deficit_covered -= self.wallet
            self.wallet = 0.0

    def liquidate(self, timestamp: pd.Timestamp, price: float, phase: str) -> None:
        self.liquidation_events += 1
        for trade in self.open_trades.copy():
            self.exit(trade, timestamp, price, "LIQUIDATION", phase)
        self.cover_deficit()

    def check_risk(self, timestamp: pd.Timestamp, price: float, phase: str) -> None:
        value = self.value(price)
        self.observe(value)
        if self.open_trades and self.risk_buffer(value) <= 0:
            self.liquidate(timestamp, price, phase)

    def opening_stops(self, timestamp: pd.Timestamp, price: float) -> None:
        for trade in self.open_trades.copy():
            if trade.direction * (price - trade.current_trailing_stop) <= 0:
                self.exit(trade, timestamp, price, stop_reason(trade), "OPEN")
        self.cover_deficit()
        self.check_risk(timestamp, price, "OPEN")

    def intrabar_leg(self, start: float, end: float, timestamp: pd.Timestamp) -> None:
        if start == end or not self.open_trades:
            return
        direction = 1 if end > start else -1
        stops = [
            trade for trade in self.open_trades
            if trade.direction == -direction
            and min(start, end) <= trade.current_trailing_stop <= max(start, end)
        ]
        stops.sort(key=lambda trade: direction * trade.current_trailing_stop)
        current = start
        for trade in (*stops, None):
            target = trade.current_trailing_stop if trade else end
            target_value = self.value(target)
            end_buffer = self.risk_buffer(target_value)
            if self.open_trades and end_buffer <= 0:
                start_buffer = self.risk_buffer(self.value(current))
                # Equity, maintenance, and the fee reserve are linear between exits.
                fraction = start_buffer / (start_buffer - end_buffer) if start_buffer > 0 else 0
                price = current + (target - current) * fraction
                self.observe(self.value(price))
                self.liquidate(timestamp, price, "INTRABAR")
                return
            self.observe(target_value)
            if trade:
                self.exit(trade, timestamp, target, stop_reason(trade), "INTRABAR")
                self.cover_deficit()
                self.check_risk(timestamp, target, "INTRABAR")
                if not self.open_trades:
                    return
            current = target

    def update_stops(self, high: float, low: float, atr: float, multiplier: float) -> None:
        for trade in self.open_trades:
            long = trade.direction == 1
            trade.extreme_since_entry = (
                max(trade.extreme_since_entry, high) if long else min(trade.extreme_since_entry, low)
            )
            raw_stop = Decimal(str(trade.extreme_since_entry)) - (
                trade.direction * Decimal(str(atr)) * Decimal(str(multiplier))
            )
            stop = price_tick(raw_stop, self.contract.tick_size, up=not long)
            trade.current_trailing_stop = (
                max(trade.current_trailing_stop, stop) if long else min(trade.current_trailing_stop, stop)
            )


def stop_reason(trade: Trade) -> str:
    return "INITIAL_STOP" if trade.current_trailing_stop == trade.initial_stop else "TRAILING_STOP"


def reject(signal: Signal, reason: str) -> None:
    signal.status, signal.rejection_reason = "REJECTED", reason


def indicators(config: Config, history: History) -> tuple[pd.DataFrame, pd.DataFrame]:
    settings = config.strategy
    market = config.market
    reference = history.candles["binance_spot", market.timeframe]
    hourly_reference = history.candles["binance_spot", market.indicator_timeframe]
    hourly_futures = history.candles["kucoin_futures", market.indicator_timeframe]
    body_high = hourly_reference[["open", "close"]].max(axis=1).rolling(settings.breakout_lookback)
    body_low = hourly_reference[["open", "close"]].min(axis=1).rolling(settings.breakout_lookback)
    previous_futures_close = hourly_futures["close"].shift()
    true_range = pd.concat([
        hourly_futures["high"] - hourly_futures["low"],
        (hourly_futures["high"] - previous_futures_close).abs(),
        (hourly_futures["low"] - previous_futures_close).abs(),
    ], axis=1).max(axis=1, skipna=False)
    hourly_atr = true_range.rolling(settings.atr_period).mean()
    hourly_atr.index += HOUR
    # Entries and trailing-stop updates use the ATR available at the 5m close.
    atr = hourly_atr.reindex(reference.index + FIVE_MINUTES, method="ffill").set_axis(reference.index)
    common = pd.DataFrame({
        "reference_close": reference["close"],
        "previous_reference_close": reference["close"].shift(),
        "atr": atr,
    })
    for name in market.symbols:
        volume = history.candles[name, market.timeframe]["volume"]
        average = volume.shift().rolling(settings.volume_lookback).mean()
        common[f"{name}_volume_ratio"] = volume / average.where(average > 0)
    levels = pd.DataFrame(index=hourly_reference.index)
    if "body_extremes" in settings.selected:
        levels["body_extremes_resistance"] = body_high.max()
        levels["body_extremes_support"] = body_low.min()
    if "body_quantiles" in settings.selected:
        levels["body_quantiles_resistance"] = body_high.quantile(settings.upper_quantile, interpolation="linear")
        levels["body_quantiles_support"] = body_low.quantile(settings.lower_quantile, interpolation="linear")
    # Keep the level active at each 5m candle's open for both prices in the crossing.
    levels.index += HOUR
    return common, levels.reindex(reference.index, method="ffill")


def simulate(config: Config, history: History, progress: Progress = print) -> list[StrategyResult]:
    config.validate()
    common, levels = indicators(config, history)
    candles = history.candles["kucoin_futures", config.market.timeframe].join(common).join(levels)
    candles = candles.loc[(candles.index >= history.period.start) & (candles.index < history.period.end)]
    return [simulate_strategy(config, history, strategy, candles, progress) for strategy in config.strategy.selected]


def simulate_strategy(
    config: Config, history: History, strategy: str, candles: pd.DataFrame, progress: Progress,
) -> StrategyResult:
    account = Account(config.account, history.contract)
    settings = config.strategy
    rates = iter(history.funding.items()) if config.account.funding_enabled else iter(())
    settlement = next(rates, None)
    pending = None
    signals: list[Signal] = []
    equity = [{
        "timestamp_utc": history.period.start, **vars(account.value(float(candles["open"].iloc[0]))),
        "open_trades": 0,
    }]
    progress(f"Simulating {strategy} over {len(candles):,} five-minute candles.")
    for index, row in enumerate(candles.itertuples(), start=1):
        start, end = row.Index, row.Index + FIVE_MINUTES
        if settlement and settlement[0] == start:
            account.settle_funding(row.open, settlement[1])
            settlement = next(rates, None)
        account.check_risk(start, row.open, "OPEN")
        account.opening_stops(start, row.open)
        if pending is not None:
            account.enter(pending, start, row.open, settings.initial_stop_atr_multiplier)
            pending = None
            account.opening_stops(start, row.open)

        while settlement and settlement[0] < end:
            account.settle_funding(row.open, settlement[1])
            account.check_risk(settlement[0], row.open, "FUNDING")
            settlement = next(rates, None)

        opening_value = account.value(row.open)
        first, second = (
            (row.low, row.high) if opening_value.long_contracts >= opening_value.short_contracts
            else (row.high, row.low)
        )
        for first_price, last_price in ((row.open, first), (first, second), (second, row.close)):
            account.intrabar_leg(first_price, last_price, end)
        account.observe(account.value(row.close))
        account.update_stops(row.high, row.low, row.atr, settings.trailing_stop_atr_multiplier)

        resistance = getattr(row, f"{strategy}_resistance")
        support = getattr(row, f"{strategy}_support")
        side, level = "", 0.0
        if row.reference_close > resistance >= row.previous_reference_close:
            side, level = "Long", resistance
        elif row.reference_close < support <= row.previous_reference_close:
            side, level = "Short", support
        if side:
            ratios = tuple(getattr(row, f"{market}_volume_ratio") for market in config.market.symbols)
            count = sum(isfinite(ratio) and ratio > settings.volume_threshold for ratio in ratios)
            if count >= settings.required_confirming_markets:
                pending = Signal(strategy, side, end, start, level, row.reference_close, row.atr, count, *ratios)
                signals.append(pending)
                if index == len(candles):
                    reject(pending, "NO_NEXT_EXECUTION_CANDLE")

        if index == len(candles):
            for trade in account.open_trades.copy():
                if settings.close_open_positions_at_end:
                    account.exit(trade, end, row.close, "END_OF_BACKTEST", "CLOSE")
                else:
                    trade.unrealized_pnl = trade.pnl_at(row.close)
            account.cover_deficit()
        equity.append({
            "timestamp_utc": end, **vars(account.value(row.close)), "open_trades": len(account.open_trades),
        })
        if index % 1000 == 0 or index == len(candles):
            progress(f"  {strategy}: {index:,}/{len(candles):,} candles, {len(account.trades):,} fills.")

    return StrategyResult(
        strategy, account.trades, signals, pd.DataFrame(equity), account.funding_paid, account.funding_received,
        account.max_open_trades, account.max_margin_usdt, account.max_margin_pct,
        account.liquidation_events, account.liquidation_deficit_covered,
    )
