"""Download, validate, and cache public exchange history."""

from dataclasses import dataclass
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import sleep
from typing import Callable
from uuid import uuid4

import numpy as np
import pandas as pd
import requests

from .config_test import AccountSettings, Config, MarketSettings


HOUR = pd.Timedelta(hours=1)
FIVE_MINUTES = pd.Timedelta(minutes=5)
INTERVALS = {"1h": HOUR, "5m": FIVE_MINUTES}
HOURLY_MARKETS = ("binance_spot", "kucoin_futures")
DAY = pd.Timedelta(days=1)
MILLISECOND = pd.Timedelta(milliseconds=1)
CANDLE_COLUMNS = ("open", "high", "low", "close", "volume")
CACHE_VERSION = 1
KUCOIN_FUTURES = "https://api-futures.kucoin.com"
KUCOIN_UTA_KLINES = "https://api.kucoin.com/api/ua/v2/market/kline"
CONTRACT_URL = KUCOIN_FUTURES + "/api/v1/contracts/{symbol}"
FUNDING_URL = KUCOIN_FUTURES + "/api/v1/contract/funding-rates"
Progress = Callable[[str], None]


class DataError(ValueError):
    """Required history is unavailable, incomplete, or invalid."""


@dataclass(frozen=True)
class Period:
    start: pd.Timestamp
    end: pd.Timestamp  # Exclusive UTC boundary.

    @classmethod
    def from_dates(cls, first: str, last: str) -> "Period":
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (first, last)):
            raise DataError("Use YYYY-MM-DD for both dates.")
        start = pd.Timestamp(date.fromisoformat(first), tz="UTC")
        end = pd.Timestamp(date.fromisoformat(last) + timedelta(days=1), tz="UTC")
        period = cls(start, end)
        period.validate()
        return period

    def validate(self) -> None:
        if any(str(value.tz) != "UTC" or value != value.floor(FIVE_MINUTES) for value in (self.start, self.end)):
            raise DataError("Period boundaries must be UTC five-minute marks.")
        if self.end <= self.start:
            raise DataError("The end date must be on or after the start date.")
        if self.end > pd.Timestamp.now(tz="UTC").floor(FIVE_MINUTES):
            raise DataError("The period includes an unfinished or future UTC candle.")

    @property
    def last_date(self) -> date:
        return (self.end - FIVE_MINUTES).date()


@dataclass(frozen=True)
class Contract:
    symbol: str
    multiplier: float
    lot_size: int
    minimum_order_quantity: int
    tick_size: float
    initial_margin_rate: float
    maintenance_margin_rate: float
    max_leverage: float
    market_max_order_quantity: int

    @classmethod
    def from_metadata(cls, raw: dict, symbol: str, settings: AccountSettings) -> "Contract":
        def quantity(value) -> int:
            number = int(value)
            if number != float(value) or number < 1:
                raise ValueError("Invalid contract quantity increment or limit.")
            return number

        try:
            if raw["symbol"] != symbol or raw["isInverse"] is not False or raw["settleCurrency"] != "USDT":
                raise ValueError("A matching linear USDT-settled contract is required.")
            lot = quantity(settings.lot_size or raw["lotSize"])
            minimum = quantity(settings.minimum_order_quantity or raw.get("minOrderQty") or lot)
            contract = cls(
                symbol, float(settings.contract_multiplier or raw["multiplier"]), lot,
                minimum, float(settings.tick_size or raw["tickSize"]),
                float(raw["initialMargin"]),
                float(raw["maintainMargin"] if settings.maintenance_margin_rate is None
                      else settings.maintenance_margin_rate),
                float(raw["maxLeverage"]), quantity(raw["marketMaxOrderQty"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise DataError(f"Invalid KuCoin Futures metadata for {symbol}: {error}") from error
        if (
            not all(isfinite(value) and value > 0 for value in
                    (contract.multiplier, contract.tick_size, contract.max_leverage))
            or not 0 <= contract.maintenance_margin_rate < 1
            or not 0 < contract.initial_margin_rate <= 1
            or max(lot, minimum) > contract.market_max_order_quantity
        ):
            raise DataError(f"Invalid KuCoin Futures trading limits for {symbol}.")
        if settings.leverage > contract.max_leverage:
            raise DataError(f"Configured leverage exceeds KuCoin's limit of {contract.max_leverage:g}.")
        return contract


@dataclass(frozen=True)
class History:
    period: Period
    candles: dict[tuple[str, str], pd.DataFrame]
    funding: pd.Series
    contract: Contract
    source: Path
    metadata_fetched_at: str
    cached_period: Period
    cached_candle_start: pd.Timestamp
    cached_hourly_start: pd.Timestamp


@dataclass(frozen=True)
class CachedPeriod:
    path: Path
    period: Period
    candle_start: pd.Timestamp
    hourly_start: pd.Timestamp
    timeframe: str
    funding_digest: str | None

    def covers(self, period: Period) -> bool:
        return self.period.start <= period.start and self.period.end >= period.end


def time_pages(start: pd.Timestamp, end: pd.Timestamp, span: pd.Timedelta):
    while start < end:
        stop = min(start + span, end)
        yield start, stop
        start = stop


def retry_delay(response: requests.Response) -> float:
    header = response.headers.get("Retry-After")
    try:
        if header:
            try:
                return float(header)
            except ValueError:
                return max(0, (pd.Timestamp(parsedate_to_datetime(header)) - pd.Timestamp.now(tz="UTC")).total_seconds())
        return float(response.headers.get("gw-ratelimit-reset", 0)) / 1000
    except (TypeError, ValueError, OverflowError):
        return 0


class PublicAPI:
    def __init__(self, session: requests.Session, settings: MarketSettings, progress: Progress):
        self.session, self.settings, self.progress = session, settings, progress

    def get(self, url: str, params: dict, *, kucoin: bool = False):
        delay = self.settings.retry_backoff
        for attempt in range(self.settings.retry_count + 1):
            wait = delay
            try:
                with self.session.get(url, params=params, timeout=self.settings.request_timeout) as response:
                    status = response.status_code
                    if status in (408, 418, 425, 429) or 500 <= status < 600:
                        failure = f"HTTP {status}"
                        wait = max(wait, retry_delay(response))
                    elif not response.ok:
                        detail = response.text[:240].strip().replace("\n", " ")
                        raise DataError(f"{url}: HTTP {status} {detail}")
                    else:
                        try:
                            payload = response.json()
                        except ValueError as error:
                            raise DataError(f"{url}: invalid JSON response.") from error
                        if not kucoin:
                            if not isinstance(payload, list):
                                raise DataError(f"{url}: expected a Binance row list, got {str(payload)[:240]}.")
                            return payload
                        if not isinstance(payload, dict):
                            raise DataError(f"{url}: invalid KuCoin response envelope.")
                        code = str(payload.get("code"))
                        if code == "200000" and "data" in payload:
                            return payload["data"]
                        if code not in ("429000", "200002", "500000"):
                            raise DataError(f"{url}: KuCoin error {code} {payload.get('msg', '')}")
                        failure = f"KuCoin error {code}"
                        wait = max(wait, retry_delay(response))
            except requests.RequestException as error:
                failure = str(error)
            if attempt == self.settings.retry_count or wait > self.settings.retry_max_delay:
                raise DataError(f"{url}: {failure}; request could not complete within retry settings.")
            self.progress(f"  {failure}. Retrying {url} in {wait:g}s.")
            sleep(wait)
            delay = min(delay * 2, self.settings.retry_max_delay)


def numeric_frame(
    rows, columns, source: str, *, unit: str | None = None, interval: pd.Timedelta | None = None,
) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=("timestamp_utc", *columns))
    try:
        timestamps = frame.pop("timestamp_utc")
        if unit:
            numeric = pd.to_numeric(timestamps)
            if not np.isfinite(numeric).all() or (numeric % 1 != 0).any():
                raise ValueError("Nonintegral exchange timestamp.")
            timestamps = pd.to_datetime(numeric, unit=unit, utc=True, errors="raise")
        else:
            timestamps = pd.to_datetime(timestamps, utc=True, errors="raise", format="ISO8601")
        frame.index = pd.DatetimeIndex(timestamps, name="timestamp_utc")
        frame = frame.apply(pd.to_numeric, errors="raise").astype(float)
    except (TypeError, ValueError, OverflowError) as error:
        raise DataError(f"{source}: invalid timestamp or numeric value: {error}") from error
    if frame.index.hasnans or not np.isfinite(frame.to_numpy()).all():
        raise DataError(f"{source}: missing or non-finite observation.")
    frame = frame.reset_index().drop_duplicates().set_index("timestamp_utc").sort_index()
    if frame.index.has_duplicates:
        duplicate = frame.index[frame.index.duplicated()][0]
        raise DataError(f"{source}: conflicting observations at {duplicate}.")
    if interval is not None:
        if (
            (frame.index != frame.index.floor(interval)).any()
            or (frame[["open", "low", "high", "close"]] <= 0).any().any()
            or (frame["volume"] < 0).any()
            or (frame["high"] < frame[["open", "low", "close"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "high", "close"]].min(axis=1)).any()
        ):
            raise DataError(f"{source}: invalid candle timestamp or OHLCV values.")
    return frame


def validate_candles(
    frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, source: str, step: pd.Timedelta,
) -> pd.DataFrame:
    expected = pd.date_range(start, end, freq=step, inclusive="left", name="timestamp_utc")
    missing = expected.difference(frame.index)
    if len(missing):
        dates = ", ".join(str(value) for value in missing[:3])
        raise DataError(f"{source}: missing {len(missing)} of {len(expected)} required candles, starting at {dates}.")
    return frame.loc[expected]


def missing_ranges(missing: pd.DatetimeIndex, step: pd.Timedelta):
    if len(missing):
        start = previous = missing[0]
        for timestamp in missing[1:]:
            if timestamp != previous + step:
                yield start, previous + step
                start = timestamp
            previous = timestamp
        yield start, previous + step


def millis(timestamp: pd.Timestamp) -> int:
    return (timestamp - pd.Timestamp(0, tz="UTC")) // MILLISECOND


def binance_page(api: PublicAPI, url: str, symbol: str, start: pd.Timestamp, end: pd.Timestamp, timeframe: str):
    step = INTERVALS[timeframe]
    rows = api.get(url, {
        "symbol": symbol, "interval": timeframe, "startTime": millis(start),
        "endTime": millis(end) - 1, "limit": (end - start) // step,
    })
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7 or int(row[6]) != int(row[0]) + step // MILLISECOND - 1:
            raise DataError(f"{url}: invalid Binance {timeframe} candle schema or close time.")
        result.append(row[:6])
    return result


def kucoin_spot_page(api: PublicAPI, url: str, symbol: str, start: pd.Timestamp, end: pd.Timestamp, timeframe: str):
    rows = api.get(url, {
        "symbol": symbol, "type": {"5m": "5min", "1h": "1hour"}[timeframe],
        "startAt": millis(start) // 1000, "endAt": millis(end) // 1000 - 1,
    }, kucoin=True)
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) < 7 for row in rows):
        raise DataError(f"{url}: invalid KuCoin Spot candle schema.")
    return [(row[0], row[1], row[3], row[4], row[2], row[5]) for row in rows]


def kucoin_futures_page(api: PublicAPI, url: str, symbol: str, start: pd.Timestamp, end: pd.Timestamp, timeframe: str):
    rows = api.get(url, {
        "symbol": symbol, "granularity": INTERVALS[timeframe] // pd.Timedelta(minutes=1),
        "from": millis(start), "to": millis(end) - 1,
    }, kucoin=True)
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) < 6 for row in rows):
        raise DataError(f"{url}: invalid KuCoin Futures candle schema.")
    return [row[:6] for row in rows]


def kucoin_uta_futures_5m(
    api: PublicAPI, symbol: str, start: pd.Timestamp, end: pd.Timestamp, classic_volume: pd.Series,
) -> pd.DataFrame:
    data = api.get(KUCOIN_UTA_KLINES, {
        "symbol": symbol, "tradeType": "FUTURES", "klineType": "TRADE", "interval": "5min",
        "startAt": millis(start) // 1000, "endAt": millis(end) // 1000 - 1,
    }, kucoin=True)
    if not isinstance(data, dict) or (
        data.get("symbol"), data.get("tradeType"), data.get("klineType")
    ) != (symbol, "FUTURES", "TRADE"):
        raise DataError(f"KuCoin UTA V2: expected {symbol} Futures trade candles.")
    rows = data.get("list")
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) < 6 for row in rows):
        raise DataError("KuCoin UTA V2: invalid Futures candle schema.")
    frame = numeric_frame(
        [row[:6] for row in rows], CANDLE_COLUMNS, f"KuCoin UTA V2 {symbol} 5m",
        unit="s", interval=FIVE_MINUTES,
    )
    # UTA V2 reports twice the Classic volume, verified against KuCoin's trade archive.
    overlap = classic_volume.reindex(frame.index).dropna()
    reported = frame.loc[overlap.index, "volume"]
    if np.allclose(reported, 2 * overlap, rtol=1e-9, atol=0):
        return frame.assign(volume=frame["volume"] / 2)
    if np.allclose(reported, overlap, rtol=1e-9, atol=0):
        return frame
    raise DataError("KuCoin UTA V2: volumes disagree with Classic on overlapping candles.")


MARKETS = {
    "binance_spot": ("https://data-api.binance.vision/api/v3/klines", 1000, "ms", binance_page),
    "binance_futures": ("https://fapi.binance.com/fapi/v1/klines", 1500, "ms", binance_page),
    "kucoin_spot": ("https://api.kucoin.com/api/v1/market/candles", 1500, "s", kucoin_spot_page),
    # The classic endpoint returned at most 200 rows per request in the public API check.
    "kucoin_futures": (KUCOIN_FUTURES + "/api/v1/kline/query", 200, "ms", kucoin_futures_page),
}


def recover_kucoin_futures_5m(
    api: PublicAPI, symbol: str, frame: pd.DataFrame, expected: pd.DatetimeIndex,
    fetch_classic: Callable[[pd.Timestamp, pd.Timestamp], pd.DataFrame],
) -> pd.DataFrame:
    step = FIVE_MINUTES
    first, stop = expected[0], expected[-1] + step
    # At most four hours of missing candles per request, plus adjacent bars.
    span = min(48, max(1, len(expected) - 2)) * step
    for name, fetch in (
        ("KuCoin Classic", fetch_classic),
        ("KuCoin UTA V2", lambda start, end: kucoin_uta_futures_5m(api, symbol, start, end, frame["volume"])),
    ):
        missing = expected.difference(frame.index)
        for gap_start, gap_end in missing_ranges(missing, step):
            for left, right in time_pages(gap_start, gap_end, span):
                query_start = max(first, left - step)
                query_end = min(stop, right + step)
                if query_start == first and query_end == stop:
                    if left > first:
                        query_start = left
                    elif right < stop:
                        query_end = right
                api.progress(f"Retrying {symbol} 5m gap {left} through {right - step} via {name}.")
                try:
                    recovered = fetch(query_start, query_end)
                    if ((recovered.index < query_start) | (recovered.index >= query_end)).any():
                        raise DataError("Candle timestamps fall outside the requested interval.")
                except DataError as error:
                    api.progress(f"  {name} could not recover the gap: {error}")
                    continue
                # Overlap is for the request only. Preserve already validated rows.
                rows = recovered.loc[recovered.index.isin(missing) & (recovered.index >= left) & (recovered.index < right)]
                if not rows.empty:
                    frame = pd.concat((frame, rows)).sort_index()
    return frame


def save_candle_page(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".page-", dir=path.parent) as staging:
        temporary = Path(staging) / path.name
        frame.to_csv(temporary, index_label="timestamp_utc")
        temporary.replace(path)


def download_candles(
    api: PublicAPI, market: str, symbol: str, start: pd.Timestamp, end: pd.Timestamp, timeframe: str,
) -> pd.DataFrame:
    url, limit, unit, fetch = MARKETS[market]
    step = INTERVALS[timeframe]
    source = f"{market} {symbol} {timeframe}"
    recover_gaps = market == "kucoin_futures" and timeframe == "5m"
    directory = api.settings.data_directory / ".pages" / market / symbol / timeframe

    def fetch_frame(first: pd.Timestamp, stop: pd.Timestamp) -> pd.DataFrame:
        try:
            rows = fetch(api, url, symbol, first, stop, timeframe)
        except (TypeError, ValueError, OverflowError) as error:
            raise DataError(f"{source} at {first}: {error}") from error
        return numeric_frame(rows, CANDLE_COLUMNS, source, unit=unit, interval=step)

    parts = []
    for first, stop in time_pages(start, end, limit * step):
        expected = pd.date_range(first, stop, freq=step, inclusive="left", name="timestamp_utc")
        path = directory / f"{millis(first)}_{millis(stop)}.csv"
        partial = path.with_suffix(".partial.csv")
        frame = None
        complete_from_cache = False
        if api.settings.cache_enabled:
            for candidate in (path, partial) if recover_gaps else (path,):
                if candidate.is_file():
                    try:
                        cached = read_csv(candidate, CANDLE_COLUMNS, step=step)
                        if not cached.index.isin(expected).all() or cached.empty:
                            raise DataError("Unexpected candle timestamps in the page cache.")
                        if candidate == path:
                            cached = validate_candles(cached, first, stop, source, step)
                    except DataError as error:
                        api.progress(f"Skipping cached page {candidate.name}: {error}")
                    else:
                        frame = cached
                        complete_from_cache = candidate == path
                        api.progress(
                            f"Resuming {source} from {first} through {stop - step} "
                            f"({len(frame)}/{len(expected)} candles cached)."
                        )
                        break
        if frame is None:
            api.progress(f"Downloading {source} from {first} through {stop - step}.")
            downloaded = fetch_frame(first, stop)
            frame = downloaded.loc[downloaded.index.isin(expected)]
            if api.settings.cache_enabled and recover_gaps and 0 < len(frame) < len(expected):
                save_candle_page(partial, frame)
        if recover_gaps and len(frame) < len(expected):
            original_count = len(frame)
            frame = recover_kucoin_futures_5m(api, symbol, frame, expected, fetch_frame)
            if api.settings.cache_enabled and original_count < len(frame) < len(expected):
                save_candle_page(partial, frame)
        page = validate_candles(frame, first, stop, source, step)
        if api.settings.cache_enabled and not complete_from_cache:
            save_candle_page(path, page)
        parts.append(page)
    return pd.concat(parts)


def validate_funding(funding: pd.Series, period: Period, max_gap_hours: int) -> pd.Series:
    funding = funding.loc[(funding.index >= period.start) & (funding.index < period.end)]
    timestamps = [period.start, *funding.index, period.end]
    for previous, current in zip(timestamps, timestamps[1:]):
        if current - previous > max_gap_hours * HOUR:
            raise DataError(f"KuCoin Futures funding history has a gap from {previous} to {current}.")
    if funding.empty:
        raise DataError("KuCoin Futures returned no funding settlements for this period.")
    return funding


def download_funding(api: PublicAPI, symbol: str, period: Period, max_gap_hours: int) -> pd.Series:
    rows = []
    # One day keeps every documented cadence below the response size, including hourly settlements.
    for first, stop in time_pages(period.start, period.end, DAY):
        api.progress(f"Downloading KuCoin Futures funding for {symbol} on {first.date()}.")
        page = api.get(FUNDING_URL, {
            "symbol": symbol, "from": millis(first), "to": millis(stop) - 1,
        }, kucoin=True)
        try:
            if not isinstance(page, list) or any(row["symbol"] != symbol for row in page):
                raise ValueError("Unexpected funding schema or contract.")
            rows.extend((row["timepoint"], row["fundingRate"]) for row in page)
        except (TypeError, KeyError, ValueError) as error:
            raise DataError(f"KuCoin Futures funding at {first}: {error}") from error
    funding = numeric_frame(rows, ("funding_rate",), "KuCoin Futures funding", unit="ms")["funding_rate"]
    return validate_funding(funding, period, max_gap_hours)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            raise ValueError("Expected a JSON object.")
        return content
    except (OSError, ValueError) as error:
        raise DataError(f"{path}: {error}") from error


def read_manifest(path: Path, config: Config) -> CachedPeriod:
    manifest = read_json(path / "manifest.json")
    timeframe = manifest.get("timeframe")
    market = config.market
    if (
        manifest.get("version") != CACHE_VERSION or manifest.get("symbols") != market.symbols
        or timeframe not in (market.timeframe, market.indicator_timeframe)
    ):
        raise DataError(f"{path.name}: cache version, timeframe, or symbols do not match the configuration.")
    try:
        period = Period(pd.Timestamp(manifest["start"]), pd.Timestamp(manifest["end"]))
        period.validate()
        candle_start = pd.Timestamp(manifest["candle_start"])
        hourly_start = pd.Timestamp(
            manifest["hourly_candle_start"] if timeframe == market.timeframe else manifest["candle_start"]
        )
        for start, step in ((candle_start, INTERVALS[timeframe]), (hourly_start, HOUR)):
            if str(start.tz) != "UTC" or start != start.floor(step) or start > period.start:
                raise ValueError("Invalid warm-up start.")
    except (KeyError, TypeError, ValueError) as error:
        raise DataError(f"{path.name}: invalid cache coverage: {error}") from error
    return CachedPeriod(path, period, candle_start, hourly_start, timeframe, manifest.get("funding_sha256"))


def discover_cache(config: Config, progress: Progress = print, *, include_hourly: bool = False) -> list[CachedPeriod]:
    choices = []
    for manifest in sorted(config.market.data_directory.glob("*/manifest.json"), reverse=True):
        try:
            choice = read_manifest(manifest.parent, config)
        except DataError as error:
            progress(f"Skipping cache: {error}")
        else:
            if choice.timeframe == config.market.timeframe or include_hourly:
                choices.append(choice)
    return choices


def read_csv(path: Path, columns, *, step: pd.Timedelta | None = None) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, float_precision="round_trip")
        if tuple(frame.columns) != ("timestamp_utc", *columns):
            raise ValueError("CSV columns do not match the canonical format.")
        return numeric_frame(frame.itertuples(index=False, name=None), columns, str(path), interval=step)
    except (OSError, ValueError) as error:
        raise DataError(f"{path}: {error}") from error


def candle_ranges(config: Config, period: Period):
    market, strategy = config.market, config.strategy
    five_minute_start = period.start - strategy.volume_lookback * FIVE_MINUTES
    hour_start = period.start.floor(HOUR) - strategy.warmup_hours * HOUR
    ranges = {
        (name, market.timeframe): (five_minute_start, period.end)
        for name in market.symbols
    }
    ranges.update({
        (name, market.indicator_timeframe): (hour_start, period.end.floor(HOUR))
        for name in HOURLY_MARKETS
    })
    return ranges


def candle_path(directory: Path, market: str, timeframe: str) -> Path:
    # Keep the original CSV names for the two hourly series so they can be reused.
    name = market if timeframe == "1h" else f"{market}_{timeframe}"
    return directory / f"{name}.csv"


def load_contract(path: Path, config: Config) -> tuple[dict, Contract]:
    metadata = read_json(path / "contract.json")
    raw = metadata.get("exchange_metadata")
    if not isinstance(raw, dict) or not metadata.get("fetched_at"):
        raise DataError(f"{path.name}: missing contract metadata or metadata timestamp.")
    return metadata, Contract.from_metadata(raw, config.market.kucoin_futures_symbol, config.account)


def load_funding(cached: CachedPeriod, period: Period, max_gap_hours: int) -> pd.Series:
    funding_path = cached.path / "funding.csv"
    if not cached.funding_digest:
        raise DataError(f"{cached.path.name}: no historical funding was downloaded for this cache.")
    try:
        digest = sha256(funding_path.read_bytes()).hexdigest()
    except OSError as error:
        raise DataError(f"Cannot read cached funding at {funding_path}: {error}") from error
    if digest != cached.funding_digest:
        raise DataError(f"{cached.path.name}: cached funding file differs from the validated download.")
    rates = read_csv(funding_path, ("funding_rate",))["funding_rate"]
    return validate_funding(rates, period, max_gap_hours)


def load_history(path: Path, config: Config, period: Period | None = None) -> History:
    cached = read_manifest(path, config)
    if cached.timeframe != config.market.timeframe:
        raise DataError(f"{path.name}: 5m data is required. Use option 1 to fill in the missing series.")
    period = period or cached.period
    period.validate()
    if not cached.covers(period):
        raise DataError(f"{path.name}: cache does not cover {period.start} to {period.end} (exclusive).")
    candles = {}
    for (market, timeframe), (start, end) in candle_ranges(config, period).items():
        step = INTERVALS[timeframe]
        file = candle_path(path, market, timeframe)
        candles[market, timeframe] = validate_candles(read_csv(file, CANDLE_COLUMNS, step=step), start, end, str(file), step)
    metadata, contract = load_contract(path, config)
    funding = (
        load_funding(cached, period, config.account.max_funding_gap_hours) if config.account.funding_enabled
        else pd.Series(dtype=float, name="funding_rate", index=pd.DatetimeIndex([], tz="UTC"))
    )
    return History(
        period, candles, funding, contract, path, metadata["fetched_at"],
        cached.period, cached.candle_start, cached.hourly_start,
    )


def save_history(
    config: Config, period: Period, candles: dict[tuple[str, str], pd.DataFrame],
    funding: pd.Series | None, metadata: dict,
) -> Path:
    root = config.market.data_directory
    root.mkdir(parents=True, exist_ok=True)
    name = f"{period.start.date()}_{period.last_date}_{uuid4().hex[:8]}"
    destination = root / name
    with TemporaryDirectory(prefix=".download-", dir=root) as staging:
        directory = Path(staging) / name
        directory.mkdir()
        for (market, timeframe), frame in candles.items():
            frame.to_csv(candle_path(directory, market, timeframe), index_label="timestamp_utc")
        digest = None
        if funding is not None:
            funding_path = directory / "funding.csv"
            funding.to_csv(funding_path, index_label="timestamp_utc", header=True)
            digest = sha256(funding_path.read_bytes()).hexdigest()
        market = config.market
        write_json(directory / "contract.json", metadata)
        write_json(directory / "manifest.json", {
            "version": CACHE_VERSION, "symbols": market.symbols, "timeframe": market.timeframe,
            "start": period.start.isoformat(), "end": period.end.isoformat(),
            "candle_start": candles["kucoin_futures", market.timeframe].index[0].isoformat(),
            "hourly_candle_start": candles["kucoin_futures", market.indicator_timeframe].index[0].isoformat(),
            "funding_sha256": digest,
        })
        load_history(directory, config)
        directory.rename(destination)
    return destination


def reuse_candles(
    api: PublicAPI, caches: list[CachedPeriod], market: str, symbol: str,
    start: pd.Timestamp, end: pd.Timestamp, timeframe: str,
) -> pd.DataFrame:
    step = INTERVALS[timeframe]
    remaining = [(start, end)]
    parts = []
    for cached in caches:
        if timeframe == "1h":
            first, stop = cached.hourly_start, cached.period.end.floor(HOUR)
        elif cached.timeframe == timeframe:
            first, stop = cached.candle_start, cached.period.end
        else:
            continue
        if not any(max(left, first) < min(right, stop) for left, right in remaining):
            continue
        file = candle_path(cached.path, market, timeframe)
        try:
            frame = read_csv(file, CANDLE_COLUMNS, step=step)
        except DataError as error:
            api.progress(f"Skipping cached candles: {error}")
            continue
        gaps = []
        for left, right in remaining:
            overlap_start, overlap_end = max(left, first), min(right, stop)
            if overlap_start >= overlap_end:
                gaps.append((left, right))
                continue
            try:
                part = validate_candles(frame, overlap_start, overlap_end, str(file), step)
            except DataError as error:
                api.progress(f"Skipping cached candles: {error}")
                gaps.append((left, right))
                continue
            parts.append(part)
            api.progress(f"Reusing {market} {timeframe} from {overlap_start} through {overlap_end - step}.")
            if left < overlap_start:
                gaps.append((left, overlap_start))
            if overlap_end < right:
                gaps.append((overlap_end, right))
        remaining = gaps
        if not remaining:
            break
    parts.extend(download_candles(api, market, symbol, first, stop, timeframe) for first, stop in remaining)
    return pd.concat(parts).sort_index()


def download_history(config: Config, period: Period, progress: Progress = print) -> History:
    config.validate()
    period.validate()
    market, account = config.market, config.account
    caches = discover_cache(config, progress, include_hourly=True) if market.cache_enabled else []
    covering = [cached for cached in caches if cached.covers(period)]
    for cached in covering:
        if cached.timeframe == market.timeframe and (cached.funding_digest or not account.funding_enabled):
            try:
                history = load_history(cached.path, config, period)
            except DataError as error:
                progress(f"Cache could not cover the full run: {error}")
            else:
                progress(f"Using cached market data in {cached.path}.")
                return history

    metadata = funding = None
    for cached in covering:
        if metadata is None:
            try:
                metadata, _ = load_contract(cached.path, config)
            except DataError as error:
                progress(f"Skipping cached contract metadata: {error}")
        if account.funding_enabled and funding is None and cached.funding_digest:
            try:
                funding = load_funding(cached, period, account.max_funding_gap_hours)
            except DataError as error:
                progress(f"Skipping cached funding: {error}")
        if metadata is not None and (funding is not None or not account.funding_enabled):
            break

    with requests.Session() as session:
        api = PublicAPI(session, market, progress)
        symbol = market.kucoin_futures_symbol
        if metadata is None:
            progress(f"Downloading KuCoin Futures contract metadata for {symbol}.")
            raw = api.get(CONTRACT_URL.format(symbol=symbol), {}, kucoin=True)
            if not isinstance(raw, dict):
                raise DataError(f"Invalid KuCoin Futures metadata for {symbol}.")
            Contract.from_metadata(raw, symbol, account)
            metadata = {"fetched_at": pd.Timestamp.now(tz="UTC").isoformat(), "exchange_metadata": raw}
        candles = {
            (name, timeframe): reuse_candles(api, caches, name, market.symbols[name], start, end, timeframe)
            for (name, timeframe), (start, end) in candle_ranges(config, period).items()
        }
        if account.funding_enabled and funding is None:
            funding = download_funding(api, symbol, period, account.max_funding_gap_hours)
    destination = save_history(config, period, candles, funding, metadata)
    progress(f"Saved market data in {destination}.")
    return load_history(destination, config)
