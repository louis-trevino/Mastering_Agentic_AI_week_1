import io
import math

import pandas as pd
import pytest

from portfolio import analytics
from portfolio.loader import PortfolioLoadError, load_transactions
from portfolio.prices import MarketData, merge_market

TODAY = pd.Timestamp("2026-10-08")
NAN = float("nan")


def csv(text: str) -> io.StringIO:
    return io.StringIO(text.strip() + "\n")


def make_market(closes: pd.DataFrame, dividends=None, splits=None, fx=None, currencies=None) -> MarketData:
    zeros = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    fx = fx if fx is not None else pd.DataFrame(1.0, index=closes.index, columns=closes.columns)
    return MarketData(
        closes=closes * fx,
        dividends=(dividends if dividends is not None else zeros) * fx,
        splits=splits if splits is not None else zeros,
        fx=fx,
        currencies=currencies or {t: "USD" for t in closes.columns},
    )


@pytest.fixture
def idx() -> pd.DatetimeIndex:
    # Business days from 2025-01-02 (Jan 1 is a holiday) to 2026-10-08.
    return pd.bdate_range("2025-01-02", "2026-10-08")


@pytest.fixture
def market(idx) -> MarketData:
    n = len(idx)
    return make_market(pd.DataFrame({
        "AAA": [100 + i * 0.1 for i in range(n)],  # steadily rising
        "BBB": [50.0] * n,                          # flat
        "SPY": [200.0] * n,                         # flat benchmark
    }, index=idx))


def tx_frame(rows):
    df = pd.DataFrame(rows, columns=["Line", "Ticker", "Date", "Transaction_Type", "Quantity", "Price"])
    df["Date"] = pd.to_datetime(df["Date"])
    return df


def pipeline(rows, market):
    trades, w1 = analytics.resolve_trades(tx_frame(rows), market)
    ledger, w2 = analytics.run_fifo(trades)
    divs = analytics.dividend_events(ledger.trades, market)
    return ledger, divs, w1 + w2


# ---- loader -----------------------------------------------------------------

def test_loads_sample_format_and_zero_price_becomes_nan():
    res = load_transactions(csv("""
Ticker,Date,Transaction_Type,Quantity,Price
aapl,2025-01-01,BUY,20,0
MSFT,2025-01-01,sell,22,410.5
"""), today=TODAY)
    tx = res.transactions
    assert list(tx["Ticker"]) == ["AAPL", "MSFT"]
    assert list(tx["Transaction_Type"]) == ["BUY", "SELL"]
    assert math.isnan(tx.loc[0, "Price"])
    assert tx.loc[1, "Price"] == 410.5
    assert res.warnings == []


def test_header_is_case_insensitive_and_extra_columns_ignored():
    res = load_transactions(csv("""
ticker , DATE,transaction_type,quantity,price,Notes
AAPL,2025-01-01,BUY,1,,hello
"""), today=TODAY)
    assert len(res.transactions) == 1


def test_missing_column_raises():
    with pytest.raises(PortfolioLoadError, match="Quantity"):
        load_transactions(csv("Ticker,Date,Transaction_Type,Price\nAAPL,2025-01-01,BUY,0"), today=TODAY)


def test_bad_rows_are_skipped_with_warnings():
    res = load_transactions(csv("""
Ticker,Date,Transaction_Type,Quantity,Price
AAPL,2025-01-01,BUY,10,0
,2025-01-01,BUY,10,0
MSFT,not-a-date,BUY,10,0
TSLA,2030-01-01,BUY,10,0
NVDA,2025-01-01,SWAP,10,0
AMZN,2025-01-01,BUY,-3,0
GOOG,2025-01-01,BUY,3,abc
"""), today=TODAY)
    assert list(res.transactions["Ticker"]) == ["AAPL"]
    assert [w.split(":")[0] for w in res.warnings] == [f"Line {n}" for n in range(3, 9)]


def test_no_valid_rows_raises():
    with pytest.raises(PortfolioLoadError, match="No valid"):
        load_transactions(csv("Ticker,Date,Transaction_Type,Quantity,Price\nAAPL,2025-01-01,HOLD,1,0"), today=TODAY)


# ---- pricing ----------------------------------------------------------------

def test_holiday_purchase_uses_next_trading_day(market):
    trades, warns = analytics.resolve_trades(tx_frame([(2, "AAA", "2025-01-01", "BUY", 10, NAN)]), market)
    assert trades.loc[0, "Price_Date"] == pd.Timestamp("2025-01-02")
    assert trades.loc[0, "Price"] == 100
    assert trades.loc[0, "Price_Source"] == "yfinance"
    assert warns == []


def test_csv_price_wins_and_unknown_ticker_skipped(market):
    trades, warns = analytics.resolve_trades(tx_frame([
        (2, "AAA", "2025-01-01", "BUY", 10, 90.0),
        (3, "ZZZ", "2025-01-01", "BUY", 10, NAN),
    ]), market)
    assert list(trades["Ticker"]) == ["AAA"]
    assert trades.loc[0, "Price"] == 90.0
    assert "ZZZ" in warns[0]


def test_split_adjusts_quantity_and_typed_price(idx):
    closes = pd.DataFrame({"NNN": [10.0] * len(idx)}, index=idx)  # already split-adjusted
    splits = pd.DataFrame({"NNN": 0.0}, index=idx)
    splits.loc[pd.Timestamp("2025-06-02"), "NNN"] = 10.0
    market = make_market(closes, splits=splits)
    trades, _ = analytics.resolve_trades(tx_frame([
        (2, "NNN", "2025-03-03", "BUY", 1, 100.0),   # before split: 1 share at $100 -> 10 @ $10
        (3, "NNN", "2025-07-01", "BUY", 5, 10.0),    # after split: unchanged
    ]), market)
    assert list(trades["Quantity"]) == [10, 5]
    assert list(trades["Price"]) == [10, 10]


def test_typed_price_is_converted_from_native_currency(idx):
    closes = pd.DataFrame({"VOD.L": [70.0] * len(idx)}, index=idx)  # pence
    fx = pd.DataFrame({"VOD.L": [1.25 / 100] * len(idx)}, index=idx)  # GBp -> USD
    market = make_market(closes, fx=fx, currencies={"VOD.L": "GBp"})
    trades, _ = analytics.resolve_trades(tx_frame([(2, "VOD.L", "2025-03-03", "BUY", 100, 80.0)]), market)
    assert trades.loc[0, "Price"] == pytest.approx(1.0)  # 80p * 1.25 / 100
    assert market.closes["VOD.L"].iloc[-1] == pytest.approx(0.875)


# ---- FIFO -------------------------------------------------------------------

def test_fifo_sells_oldest_lots_first(market):
    ledger, _, warns = pipeline([
        (2, "BBB", "2025-01-02", "BUY", 10, 40.0),
        (3, "BBB", "2025-02-03", "BUY", 10, 60.0),
        (4, "BBB", "2025-03-03", "SELL", 15, 55.0),
    ], market)
    assert warns == []
    sale = ledger.realized.iloc[0]
    assert sale["Cost"] == pytest.approx(10 * 40 + 5 * 60)
    assert sale["Realized_PL"] == pytest.approx(15 * 55 - 700)
    assert ledger.open_lots[["Quantity", "Price"]].values.tolist() == [[5, 60.0]]


def test_oversell_is_rejected(market):
    ledger, _, warns = pipeline([
        (2, "BBB", "2025-01-02", "BUY", 10, 40.0),
        (3, "BBB", "2025-03-03", "SELL", 11, 55.0),
    ], market)
    assert "exceeds" in warns[0]
    assert ledger.realized.empty
    assert len(ledger.trades) == 1


def test_positions_split_open_and_realized(market):
    ledger, divs, _ = pipeline([
        (2, "BBB", "2025-01-02", "BUY", 10, 40.0),
        (3, "BBB", "2025-02-03", "BUY", 30, 60.0),
        (4, "BBB", "2025-03-03", "SELL", 10, 55.0),
        (5, "AAA", "2025-01-02", "BUY", 1, NAN),
        (6, "AAA", "2025-02-03", "SELL", 1, NAN),
    ], market)
    pos = analytics.build_positions(ledger, divs, market).set_index("Ticker")
    assert pos.loc["BBB", "Quantity"] == 30
    assert pos.loc["BBB", "Avg_Cost"] == pytest.approx(60.0)
    assert pos.loc["BBB", "Unrealized_PL"] == pytest.approx(30 * (50 - 60))
    assert pos.loc["BBB", "Realized_PL"] == pytest.approx(10 * (55 - 40))
    assert pos.loc["BBB", "Weight_Pct"] == pytest.approx(100.0)
    assert pos.loc["AAA", "Quantity"] == 0  # closed position still listed
    assert pos.loc["AAA", "Realized_PL"] > 0


# ---- dividends --------------------------------------------------------------

def test_dividends_paid_on_shares_held_before_ex_date(idx):
    closes = pd.DataFrame({"DDD": [100.0] * len(idx)}, index=idx)
    divs = pd.DataFrame({"DDD": 0.0}, index=idx)
    divs.loc[pd.Timestamp("2025-03-03"), "DDD"] = 1.0
    divs.loc[pd.Timestamp("2025-06-02"), "DDD"] = 1.0
    market = make_market(closes, dividends=divs)
    _, events, _ = pipeline([
        (2, "DDD", "2025-01-02", "BUY", 10, NAN),
        (3, "DDD", "2025-03-03", "BUY", 5, NAN),    # bought ON ex-date: not entitled to March
        (4, "DDD", "2025-06-02", "SELL", 15, NAN),  # sold ON ex-date: still entitled to June
    ], market)
    assert events["Shares"].tolist() == [10, 15]
    assert events["Amount"].sum() == pytest.approx(25.0)


# ---- performance ------------------------------------------------------------

def test_past_year_uses_later_of_window_start_and_purchase(market):
    as_of = market.closes.index[-1]
    ledger, divs, _ = pipeline([
        (2, "AAA", "2025-01-01", "BUY", 1, NAN),   # older than a year: measured from window start
        (3, "BBB", "2026-06-01", "BUY", 2, 45.0),  # inside the window: measured from purchase
    ], market)
    perf = analytics.period_performance(ledger.trades, divs, market, as_of - pd.DateOffset(years=1), as_of)[0].set_index("Ticker")

    closes = market.closes["AAA"]
    start_aaa = closes.loc[closes.index <= as_of - pd.DateOffset(years=1)].iloc[-1]
    assert perf.loc["AAA", "Profit"] == pytest.approx(closes.iloc[-1] - start_aaa)
    assert perf.loc["BBB", "Profit"] == pytest.approx(2 * (50 - 45))
    assert perf.loc["BBB", "Growth_Pct"] == pytest.approx((50 / 45 - 1) * 100)


def test_past_year_counts_sales_and_dividends(idx):
    closes = pd.DataFrame({"DDD": [100.0] * len(idx)}, index=idx)
    divs = pd.DataFrame({"DDD": 0.0}, index=idx)
    divs.loc[pd.Timestamp("2026-03-02"), "DDD"] = 2.0
    market = make_market(closes, dividends=divs)
    ledger, events, _ = pipeline([
        (2, "DDD", "2025-01-02", "BUY", 10, NAN),
        (3, "DDD", "2026-05-01", "SELL", 4, 110.0),  # sold above market: +40 realized
    ], market)
    perf = analytics.period_performance(ledger.trades, events, market, idx[-1] - pd.DateOffset(years=1), idx[-1])[0].set_index("Ticker")
    # value 1000 -> 600, +440 proceeds, +20 dividends
    assert perf.loc["DDD", "Profit"] == pytest.approx(600 - 1000 + 440 + 20)
    assert perf.loc["DDD", "Growth_Pct"] == pytest.approx(6.0)


def test_benchmark_mirrors_cash_flows(market):
    ledger, divs, _ = pipeline([
        (2, "BBB", "2025-01-02", "BUY", 10, NAN),    # $500 -> 2.5 SPY
        (3, "BBB", "2025-03-03", "SELL", 4, 75.0),   # $300 -> sell 1.5 SPY
    ], market)
    bench = analytics.benchmark_trades(ledger.trades, market, "SPY")
    assert bench["Quantity"].tolist() == pytest.approx([2.5, 1.5])
    wealth = analytics.wealth_series(bench, analytics.dividend_events(bench, market), market)
    # flat SPY: 1 share left ($200) + $300 cash out = $500 invested
    assert wealth["Wealth"].iloc[-1] == pytest.approx(500.0)
    mine = analytics.wealth_series(ledger.trades, divs, market)
    assert mine["Wealth"].iloc[-1] == pytest.approx(6 * 50 + 300)


def test_benchmark_sale_capped_at_holdings(market):
    trades = pd.DataFrame([
        {"Line": 2, "Ticker": "BBB", "Date": pd.Timestamp("2025-01-02"), "Type": "BUY", "Quantity": 2,
         "Price": 50.0, "Amount": 100.0, "Price_Date": pd.Timestamp("2025-01-02"), "Price_Source": "CSV", "Split_Factor": 1.0},
        {"Line": 3, "Ticker": "BBB", "Date": pd.Timestamp("2025-02-03"), "Type": "SELL", "Quantity": 2,
         "Price": 500.0, "Amount": 1000.0, "Price_Date": pd.Timestamp("2025-02-03"), "Price_Source": "CSV", "Split_Factor": 1.0},
    ])
    bench = analytics.benchmark_trades(trades, market, "SPY")
    assert bench["Quantity"].tolist() == pytest.approx([0.5, 0.5])


def test_growth_series_starts_at_zero(market):
    g = analytics.growth_series(market.closes, ["AAA", "BBB"], market.closes.index[-1] - pd.DateOffset(years=1))
    first = g.sort_values("Date").groupby("Ticker").first()
    assert first["Growth_Pct"].abs().max() == pytest.approx(0.0)
    assert g[g["Ticker"] == "BBB"]["Growth_Pct"].abs().max() == pytest.approx(0.0)


# ---- periods & benchmark data ----------------------------------------------

@pytest.mark.parametrize("period, expected", [
    ("3M", "2026-07-08"),
    ("1Y", "2025-10-08"),
    ("5Y", "2021-10-08"),
    ("YTD", "2025-12-31"),
    ("Max", "2024-02-29"),
])
def test_period_start(period, expected):
    assert analytics.period_start(period, TODAY, pd.Timestamp("2024-03-01")) == pd.Timestamp(expected)


def test_max_period_equals_total_return(market):
    ledger, divs, _ = pipeline([
        (2, "BBB", "2025-01-02", "BUY", 10, 40.0),
        (3, "BBB", "2025-03-03", "SELL", 4, 55.0),
    ], market)
    start = analytics.period_start("Max", TODAY, ledger.trades["Date"].min())
    perf, snapshot = analytics.period_performance(ledger.trades, divs, market, start, market.closes.index[-1])
    pos = analytics.build_positions(ledger, divs, market)
    assert snapshot == pd.Timestamp("2025-01-01")  # before the data starts: nothing held yet
    assert perf["Profit"].sum() == pytest.approx(pos["Total_Return"].sum())
    assert analytics.totals(perf)["growth_pct"] == pytest.approx(pos["Total_Return_Pct"].iloc[0])


def test_ytd_uses_last_close_of_previous_year(market):
    start = analytics.period_start("YTD", TODAY, pd.Timestamp("2025-01-02"))
    assert analytics.snapshot_date(market.closes.index, start) == pd.Timestamp("2025-12-31")


def test_merge_market_adds_only_new_tickers(market, idx):
    extra = make_market(pd.DataFrame({"QQQ": [300.0] * len(idx), "AAA": [1.0] * len(idx)}, index=idx),
                        currencies={"QQQ": "USD", "AAA": "USD"})
    merged = merge_market(market, extra)
    assert list(merged.closes.columns) == ["AAA", "BBB", "SPY", "QQQ"]
    assert merged.closes["AAA"].iloc[-1] == market.closes["AAA"].iloc[-1]  # base wins
    assert merge_market(market, make_market(market.closes[["SPY"]])) is market
