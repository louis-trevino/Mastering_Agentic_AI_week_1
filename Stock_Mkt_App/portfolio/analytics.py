"""Portfolio math. Pure pandas: no network, no Streamlit.

All amounts are USD, and all quantities/prices are split-adjusted (expressed
in today's shares) so they line up with the MarketData closes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import pandas as pd

from portfolio.prices import MarketData

# Fixed-length periods; "YTD" and "Max" are computed in period_start().
PERIODS = {
    "3M": pd.DateOffset(months=3),
    "6M": pd.DateOffset(months=6),
    "YTD": None,
    "1Y": pd.DateOffset(years=1),
    "3Y": pd.DateOffset(years=3),
    "5Y": pd.DateOffset(years=5),
    "Max": None,
}
HISTORY = pd.DateOffset(years=5)  # always download enough for the longest fixed period
_EPS = 1e-9


def price_on_or_after(series: pd.Series, when: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    """First value on/after `when` (handles weekends and holidays).

    Falls back to the last value before `when` if there is nothing after it
    (e.g. a trade made today before the first close is published).
    """
    s = series.dropna()
    after = s.loc[s.index >= when]
    if not after.empty:
        return after.index[0], float(after.iloc[0])
    before = s.loc[s.index < when]
    if not before.empty:
        return before.index[-1], float(before.iloc[-1])
    return None


def price_on_or_before(series: pd.Series, when: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    """Last value on/before `when`."""
    s = series.dropna()
    before = s.loc[s.index <= when]
    if before.empty:
        return None
    return before.index[-1], float(before.iloc[-1])


def split_factor(splits: pd.Series, after: pd.Timestamp) -> float:
    """Cumulative split ratio for splits strictly after `after` (1.0 if none)."""
    s = splits.loc[(splits.index > after) & (splits > 0)]
    return float(s.prod()) if not s.empty else 1.0


def _cumulative(amounts: pd.DataFrame | pd.Series, index: pd.Index) -> pd.DataFrame | pd.Series:
    """Running total of dated amounts, sampled at end of each day in `index`.

    Amounts dated on non-trading days roll into the next trading day.
    """
    cum = amounts.sort_index().cumsum()
    return cum.reindex(cum.index.union(index)).ffill().fillna(0.0).reindex(index)


# ---- trades -------------------------------------------------------------------

def resolve_trades(transactions: pd.DataFrame, market: MarketData) -> tuple[pd.DataFrame, list[str]]:
    """Price every transaction in USD and express it in split-adjusted shares.

    A CSV price is taken as the native-currency price on the trade date and
    converted at that day's FX rate; a missing price uses the close on (or the
    next trading day after) the trade date.
    """
    warnings: list[str] = []
    rows = []
    for tx in transactions.sort_values(["Date", "Line"]).itertuples(index=False):
        t = tx.Ticker
        if t not in market.closes.columns or market.closes[t].dropna().empty:
            warnings.append(f"Line {tx.Line}: no market data (or FX rate) for '{t}' - row skipped.")
            continue
        factor = split_factor(market.splits[t], tx.Date)
        if pd.notna(tx.Price):
            fx = price_on_or_after(market.fx[t], tx.Date)
            price, price_date, source = tx.Price * fx[1] / factor, tx.Date, "CSV"
        else:
            found = price_on_or_after(market.closes[t], tx.Date)
            if found is None:
                warnings.append(f"Line {tx.Line}: no price for {t} near {tx.Date:%Y-%m-%d} - row skipped.")
                continue
            price_date, price = found
            source = "yfinance"
        qty = float(tx.Quantity) * factor
        rows.append({
            "Line": tx.Line, "Ticker": t, "Date": tx.Date, "Type": tx.Transaction_Type,
            "Quantity": qty, "Price": price, "Amount": qty * price,
            "Price_Date": price_date, "Price_Source": source, "Split_Factor": factor,
        })
    return pd.DataFrame(rows, columns=_TRADE_COLUMNS), warnings


_TRADE_COLUMNS = ["Line", "Ticker", "Date", "Type", "Quantity", "Price", "Amount",
                  "Price_Date", "Price_Source", "Split_Factor"]


@dataclass
class Ledger:
    trades: pd.DataFrame     # executed trades (rejected SELLs removed)
    open_lots: pd.DataFrame  # remaining FIFO lots: Ticker, Line, Date, Quantity, Price
    realized: pd.DataFrame   # one row per SELL: Ticker, Line, Date, Quantity, Proceeds, Cost, Realized_PL


def run_fifo(trades: pd.DataFrame) -> tuple[Ledger, list[str]]:
    """Match SELLs against the oldest open BUY lots (first in, first out)."""
    warnings: list[str] = []
    lots: dict[str, deque] = {}
    executed, realized = [], []
    for tr in trades.itertuples():
        book = lots.setdefault(tr.Ticker, deque())
        if tr.Type == "BUY":
            book.append({"Ticker": tr.Ticker, "Line": tr.Line, "Date": tr.Date,
                         "Quantity": tr.Quantity, "Price": tr.Price})
            executed.append(tr.Index)
            continue

        held = sum(lot["Quantity"] for lot in book)
        if tr.Quantity > held + _EPS:
            note = " (split-adjusted)" if tr.Split_Factor != 1 else ""
            warnings.append(f"Line {tr.Line}: SELL of {tr.Quantity:g}{note} {tr.Ticker} exceeds the "
                            f"{held:g} shares held on {tr.Date:%Y-%m-%d} - row skipped.")
            continue
        remaining, cost = tr.Quantity, 0.0
        while remaining > _EPS:
            lot = book[0]
            take = min(lot["Quantity"], remaining)
            cost += take * lot["Price"]
            lot["Quantity"] -= take
            remaining -= take
            if lot["Quantity"] <= _EPS:
                book.popleft()
        realized.append({"Ticker": tr.Ticker, "Line": tr.Line, "Date": tr.Date, "Quantity": tr.Quantity,
                         "Proceeds": tr.Amount, "Cost": cost, "Realized_PL": tr.Amount - cost})
        executed.append(tr.Index)

    open_lots = pd.DataFrame([lot for book in lots.values() for lot in book],
                             columns=["Ticker", "Line", "Date", "Quantity", "Price"])
    realized_df = pd.DataFrame(realized, columns=["Ticker", "Line", "Date", "Quantity",
                                                  "Proceeds", "Cost", "Realized_PL"])
    return Ledger(trades.loc[executed].reset_index(drop=True), open_lots, realized_df), warnings


def holdings_timeline(trades: pd.DataFrame, index: pd.Index) -> pd.DataFrame:
    """Shares held per ticker at the end of each day in `index`."""
    signed = trades["Quantity"].where(trades["Type"] == "BUY", -trades["Quantity"])
    daily = signed.groupby([trades["Date"], trades["Ticker"]]).sum().unstack(fill_value=0.0)
    return _cumulative(daily, index)


def dividend_events(trades: pd.DataFrame, market: MarketData) -> pd.DataFrame:
    """Cash dividends received: shares held at the close before each ex-date."""
    tickers = list(trades["Ticker"].unique())
    eligible = holdings_timeline(trades, market.closes.index).shift(1).fillna(0.0)
    rows = []
    for t in tickers:
        per_share = market.dividends[t]
        for ex_date, amount in per_share[per_share > 0].items():
            shares = eligible.at[ex_date, t]
            if shares > _EPS:
                rows.append({"Ticker": t, "Ex_Date": ex_date, "Shares": shares,
                             "Per_Share": amount, "Amount": shares * amount})
    return pd.DataFrame(rows, columns=["Ticker", "Ex_Date", "Shares", "Per_Share", "Amount"])


# ---- reports ------------------------------------------------------------------

def build_positions(ledger: Ledger, dividends: pd.DataFrame, market: MarketData) -> pd.DataFrame:
    """One row per ticker ever traded, open or closed."""
    trades = ledger.trades
    tickers = sorted(trades["Ticker"].unique())
    last = market.closes.ffill().iloc[-1]

    lots = ledger.open_lots.assign(Cost=ledger.open_lots["Quantity"] * ledger.open_lots["Price"])
    held = lots.groupby("Ticker")[["Quantity", "Cost"]].sum().reindex(tickers, fill_value=0.0)
    buys = trades[trades["Type"] == "BUY"].groupby("Ticker")

    pos = pd.DataFrame(index=pd.Index(tickers, name="Ticker"))
    pos["Currency"] = [market.currencies.get(t, "USD") for t in tickers]
    pos["Quantity"] = held["Quantity"].round(10)
    pos["Avg_Cost"] = (held["Cost"] / held["Quantity"]).where(held["Quantity"] > _EPS)
    pos["Current_Price"] = last.reindex(tickers)
    pos["Cost_Basis"] = held["Cost"]
    pos["Market_Value"] = pos["Quantity"] * pos["Current_Price"]
    pos["Unrealized_PL"] = pos["Market_Value"] - pos["Cost_Basis"]
    pos["Realized_PL"] = ledger.realized.groupby("Ticker")["Realized_PL"].sum().reindex(tickers, fill_value=0.0)
    pos["Dividends"] = dividends.groupby("Ticker")["Amount"].sum().reindex(tickers, fill_value=0.0)
    pos["Total_Return"] = pos["Unrealized_PL"] + pos["Realized_PL"] + pos["Dividends"]
    pos["Total_Return_Pct"] = pos["Total_Return"] / buys["Amount"].sum().reindex(tickers) * 100
    pos["Weight_Pct"] = pos["Market_Value"] / pos["Market_Value"].sum() * 100
    pos["First_Purchase"] = buys["Date"].min().reindex(tickers)
    return pos.reset_index()


def period_start(period: str, as_of: pd.Timestamp, first_trade: pd.Timestamp) -> pd.Timestamp:
    """Calendar date whose closing snapshot opens the period.

    YTD opens on Dec 31 (last year's final close); "Max" opens the day before
    the first trade, so every purchase counts as money put in.
    """
    if period == "Max":
        return first_trade.normalize() - pd.Timedelta(days=1)
    if period == "YTD":
        return pd.Timestamp(as_of.year - 1, 12, 31)
    return (as_of - PERIODS[period]).normalize()


def snapshot_date(index: pd.Index, start: pd.Timestamp) -> pd.Timestamp | None:
    """Last trading day on/before `start`; None if the data begins after it."""
    before = index[index <= start]
    return before[-1] if len(before) else None


def period_performance(trades: pd.DataFrame, dividends: pd.DataFrame, market: MarketData,
                       start: pd.Timestamp, as_of: pd.Timestamp) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Profit and growth % per ticker between the close on `start` and `as_of`.

    Profit = value at end - value at start - buys + sells + dividends, all
    within the period. Growth % = profit / (start value + buys), i.e. the
    return on all capital that was at work in that ticker during the period.
    Returns the frame and the trading day actually used as the start snapshot
    (or `start` itself when the data begins later: nothing was held yet).
    """
    closes = market.closes.ffill()
    timeline = holdings_timeline(trades, closes.index)
    tickers = list(timeline.columns)

    snap = snapshot_date(closes.index, start)
    if snap is None:
        start_value = pd.Series(0.0, index=tickers)
    else:
        start = snap
        start_value = (timeline.loc[start] * closes.loc[start, tickers]).fillna(0.0)
    end_value = (timeline.loc[as_of] * closes.loc[as_of, tickers]).fillna(0.0)
    window = trades[trades["Date"] > start]
    by_type = window.groupby(["Ticker", "Type"])["Amount"].sum().unstack(fill_value=0.0)
    buys = by_type.get("BUY", pd.Series(dtype=float)).reindex(tickers, fill_value=0.0)
    sells = by_type.get("SELL", pd.Series(dtype=float)).reindex(tickers, fill_value=0.0)
    divs = (dividends[dividends["Ex_Date"] > start].groupby("Ticker")["Amount"].sum()
            .reindex(tickers, fill_value=0.0))

    perf = pd.DataFrame({"Start_Value": start_value, "Buys": buys, "Sells": sells,
                         "Dividends": divs, "End_Value": end_value})
    perf["Profit"] = perf["End_Value"] - perf["Start_Value"] - perf["Buys"] + perf["Sells"] + perf["Dividends"]
    capital = perf["Start_Value"] + perf["Buys"]
    perf["Growth_Pct"] = (perf["Profit"] / capital * 100).where(capital > _EPS)
    active = (perf[["Start_Value", "Buys", "Sells", "End_Value"]].abs().sum(axis=1) > _EPS)
    perf.index.name = "Ticker"
    return perf[active].reset_index(), start


def totals(perf: pd.DataFrame) -> dict[str, float]:
    """Whole-portfolio profit and growth % from a period_performance frame."""
    s = perf[["Start_Value", "Buys", "Profit"]].sum()
    capital = s["Start_Value"] + s["Buys"]
    return {"profit": float(s["Profit"]),
            "growth_pct": float(s["Profit"] / capital * 100) if capital > _EPS else float("nan")}


def wealth_series(trades: pd.DataFrame, dividends: pd.DataFrame, market: MarketData) -> pd.DataFrame:
    """Daily totals since the first trade.

    Wealth = market value + cash taken out (sale proceeds + dividends), so a
    sale or a dividend doesn't look like a loss.
    """
    closes = market.closes.ffill()
    index = closes.index[closes.index >= trades["Date"].min()]
    if index.empty:
        index = closes.index[-1:]
    timeline = holdings_timeline(trades, closes.index).loc[index]
    market_value = (timeline * closes.loc[index, timeline.columns]).fillna(0.0).sum(axis=1)

    def flow(kind: str) -> pd.Series:
        t = trades[trades["Type"] == kind]
        return _cumulative(t.groupby("Date")["Amount"].sum(), index) if not t.empty else pd.Series(0.0, index=index)

    div_cum = (_cumulative(dividends.groupby("Ex_Date")["Amount"].sum(), index)
               if not dividends.empty else pd.Series(0.0, index=index))
    buys, sells = flow("BUY"), flow("SELL")
    return pd.DataFrame({
        "Market_Value": market_value,
        "Net_Invested": buys - sells,
        "Wealth": market_value + sells + div_cum,
        "Total_Bought": buys,
    }, index=index)


def benchmark_trades(trades: pd.DataFrame, market: MarketData, ticker: str) -> pd.DataFrame:
    """Mirror every cash flow into `ticker`: each BUY of $X buys $X of it, each
    SELL raising $Y sells $Y of it (capped at what the benchmark holds)."""
    closes = market.closes[ticker]
    held, rows = 0.0, []
    for tr in trades.itertuples(index=False):
        found = price_on_or_after(closes, tr.Price_Date)
        if found is None:
            continue
        price_date, price = found
        qty = tr.Amount / price
        if tr.Type == "SELL":
            qty = min(qty, held)
            if qty <= _EPS:
                continue
        held += qty if tr.Type == "BUY" else -qty
        rows.append({"Line": tr.Line, "Ticker": ticker, "Date": tr.Date, "Type": tr.Type,
                     "Quantity": qty, "Price": price, "Amount": qty * price,
                     "Price_Date": price_date, "Price_Source": "benchmark", "Split_Factor": 1.0})
    return pd.DataFrame(rows, columns=_TRADE_COLUMNS)


def growth_series(closes: pd.DataFrame, tickers: list[str], start: pd.Timestamp) -> pd.DataFrame:
    """Cumulative price growth (%) of each ticker from the close on `start`.

    Long format (Date, Ticker, Growth_Pct) for plotting.
    """
    window = closes.loc[closes.index >= start, tickers].ffill()
    base = window.bfill().iloc[0]
    growth = (window / base - 1) * 100
    growth.index.name = "Date"
    return growth.reset_index().melt(id_vars="Date", var_name="Ticker", value_name="Growth_Pct").dropna()
