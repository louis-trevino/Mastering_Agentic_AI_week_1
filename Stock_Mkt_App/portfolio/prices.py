"""Market data from yfinance: closes, dividends, splits, currencies, FX."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import yfinance as yf

BASE_CURRENCY = "USD"

# Yahoo quotes some exchanges in minor units: currency -> (major currency, divisor).
MINOR_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ILA": ("ILS", 100), "ZAc": ("ZAR", 100)}


class PriceDataError(RuntimeError):
    """yfinance returned nothing usable."""


@dataclass
class MarketData:
    """Everything indexed by trading date, one column per ticker.

    closes / dividends are split-adjusted and already converted to USD.
    fx holds the native->USD multiplier per ticker (incl. minor-unit divisor),
    used to convert prices typed into the CSV.
    """
    closes: pd.DataFrame
    dividends: pd.DataFrame
    splits: pd.DataFrame
    fx: pd.DataFrame
    currencies: dict[str, str]


def fetch_market_data(tickers: tuple[str, ...], start: pd.Timestamp, end: pd.Timestamp) -> MarketData:
    """Download and USD-convert market data. Unknown tickers are simply absent."""
    raw = _download(list(tickers), start, end, actions=True)
    missing = [t for t in tickers if t not in raw.get("Close", pd.DataFrame()).columns]
    if missing:  # one retry: transient failures are common with Yahoo
        retry = _download(missing, start, end, actions=True)
        raw = {k: pd.concat([raw.get(k), retry.get(k)], axis=1) for k in ("Close", "Dividends", "Stock Splits")
               if k in raw or k in retry}
    if "Close" not in raw or raw["Close"].empty:
        raise PriceDataError("No price data returned by yfinance. Check the tickers and your connection.")

    closes = raw["Close"].sort_index()
    found = list(closes.columns)
    dividends = _frame(raw.get("Dividends"), closes.index, found)
    splits = _frame(raw.get("Stock Splits"), closes.index, found)

    currencies = {t: _currency(t) for t in found}
    fx = _fx_multipliers(currencies, closes.index, start, end)
    return MarketData(
        closes=closes * fx,
        dividends=dividends * fx,
        splits=splits,
        fx=fx,
        currencies=currencies,
    )


def merge_market(base: MarketData, extra: MarketData) -> MarketData:
    """Add the tickers of `extra` (e.g. a benchmark) that `base` doesn't have."""
    new = [t for t in extra.closes.columns if t not in base.closes.columns]
    if not new:
        return base

    def join(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
        return pd.concat([a, b[new]], axis=1).sort_index()

    return MarketData(
        closes=join(base.closes, extra.closes),
        dividends=join(base.dividends, extra.dividends).fillna(0.0),
        splits=join(base.splits, extra.splits).fillna(0.0),
        fx=join(base.fx, extra.fx).ffill().bfill(),
        currencies={**base.currencies, **{t: extra.currencies[t] for t in new}},
    )


def _download(tickers: list[str], start: pd.Timestamp, end: pd.Timestamp, actions: bool = False) -> dict[str, pd.DataFrame]:
    data = yf.download(
        tickers,
        start=start.strftime("%Y-%m-%d"),
        end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),  # end is exclusive
        auto_adjust=False,  # Close stays split-adjusted but not dividend-adjusted
        actions=actions,
        progress=False,
        threads=False,  # threaded downloads race on yfinance's sqlite cache ("database is locked")
    )
    if data is None or data.empty:
        return {}
    out = {}
    for field in ("Close", "Dividends", "Stock Splits"):
        if field not in data.columns.get_level_values(0):
            continue
        frame = data[field]
        if isinstance(frame, pd.Series):  # defensive: single ticker without MultiIndex
            frame = frame.to_frame(name=tickers[0])
        frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
        frame.columns = [str(c) for c in frame.columns]
        out[field] = frame
    if "Close" not in out:
        return {}
    out["Close"] = out["Close"].dropna(axis=1, how="all")
    for field in ("Dividends", "Stock Splits"):  # keep only tickers that actually priced
        if field in out:
            out[field] = out[field].reindex(columns=out["Close"].columns)
    return out


def _frame(frame: pd.DataFrame | None, index: pd.Index, columns: list[str]) -> pd.DataFrame:
    """Align an event frame (dividends/splits) to the close grid, zeros elsewhere."""
    if frame is None:
        return pd.DataFrame(0.0, index=index, columns=columns)
    return frame.reindex(index=index, columns=columns).fillna(0.0)


def _currency(ticker: str) -> str:
    try:
        return yf.Ticker(ticker).fast_info["currency"] or BASE_CURRENCY
    except Exception:
        return BASE_CURRENCY  # assume USD if Yahoo won't say


def _fx_multipliers(currencies: dict[str, str], index: pd.Index, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Per-ticker native->USD multiplier on each trading date."""
    major = {t: MINOR_UNITS.get(c, (c, 1)) for t, c in currencies.items()}
    pairs = sorted({f"{m}{BASE_CURRENCY}=X" for m, _ in major.values() if m.upper() != BASE_CURRENCY})
    rates = pd.DataFrame(index=index)
    if pairs:
        fetched = _download(pairs, start - pd.Timedelta(days=10), end).get("Close", pd.DataFrame())
        if not fetched.empty:
            union = fetched.index.union(index)
            rates = fetched.reindex(union).ffill().bfill().reindex(index)

    fx = pd.DataFrame(index=index)
    for ticker, (cur, divisor) in major.items():
        if cur.upper() == BASE_CURRENCY:
            fx[ticker] = 1.0 / divisor
        elif f"{cur}{BASE_CURRENCY}=X" in rates.columns:
            fx[ticker] = rates[f"{cur}{BASE_CURRENCY}=X"] / divisor
        else:
            fx[ticker] = float("nan")  # caller drops tickers it cannot convert
    return fx
