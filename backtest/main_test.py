"""Run the interactive historical backtest as a script or module."""

if __name__ == "__main__" and not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "backtest"

from .backtest import simulate
from .config_test import CONFIG, Config, STRATEGY_NAMES
from .market_data import History, HOUR, Period, discover_cache, download_history, load_history
from .report import save_results


def run_backtest(config: Config, history: History):
    results = simulate(config, history)
    folder, summaries = save_results(config, history, results)
    print(f"\n{history.period.start.date()} through {history.period.last_date} UTC · {history.contract.symbol}")
    for summary in summaries:
        name = STRATEGY_NAMES[summary["strategy"]]
        print(
            f"  {name}: equity {summary['ending_equity']:,.2f} USDT, "
            f"return {summary['net_return_pct']:+.2f}%, "
            f"{summary['completed_trades']} closed, {summary['open_trades']} open, "
            f"{summary['rejected_entries']} rejected."
        )
    print(f"Results saved to {folder}\n")
    return folder, summaries, results


def select_history(config: Config) -> History | None:
    choices = discover_cache(config)
    if not choices:
        print(f"No compatible cached periods under {config.market.data_directory}.")
        return None
    print("\nCached UTC periods")
    for number, choice in enumerate(choices, start=1):
        warmup = (choice.period.start - choice.candle_start) // HOUR
        funding = "funding available" if choice.has_funding else "no funding"
        print(f"  {number}. {choice.period.start.date()} through {choice.period.last_date} "
              f"({warmup} warm-up hours, {funding}, {choice.path.name})")
    selection = input("Period number (0 to cancel): ").strip()
    if selection == "0":
        return None
    if not selection.isdecimal() or not 1 <= int(selection) <= len(choices):
        raise ValueError("Choose one of the displayed period numbers.")
    return load_history(choices[int(selection) - 1].path, config)


def main(config: Config = CONFIG) -> int:
    try:
        config.validate()
    except (TypeError, ValueError) as error:
        print(f"Configuration error: {error}")
        return 1
    market = config.market
    while True:
        print(
            f"\nMOMENTUM BREAKOUT BACKTEST\n\nSymbol: {market.symbol}\n"
            f"Timeframe: {market.timeframe.upper()}\nExecution: KuCoin Futures\n\n"
            "1. Download new historical data\n"
            "2. Use previously downloaded historical data\n"
            "0. Exit"
        )
        try:
            choice = input("\nSelect an option: ").strip()
            if choice == "0":
                return 0
            if choice == "1":
                first = input("Start date (YYYY-MM-DD UTC): ").strip()
                last = input("End date (YYYY-MM-DD UTC, inclusive): ").strip()
                history = download_history(config, Period.from_dates(first, last))
            elif choice == "2":
                history = select_history(config)
            else:
                print("Choose 0, 1, or 2.")
                continue
            if history is not None:
                run_backtest(config, history)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        except (OSError, ValueError, OverflowError) as error:
            print(f"Backtest error: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
