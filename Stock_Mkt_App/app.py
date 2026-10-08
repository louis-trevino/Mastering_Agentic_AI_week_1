"""Stock Portfolio Analyzer - Streamlit entry point.

Run with:  uv run streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from portfolio import analytics, charts
from portfolio.loader import REQUIRED_COLUMNS, PortfolioLoadError, load_transactions
from portfolio.prices import BASE_CURRENCY, MarketData, PriceDataError, fetch_market_data, merge_market

SAMPLE_FILE = Path(__file__).parent / "stock_data_01.csv"

BENCHMARKS = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq-100",
    "VTI": "US total market",
    "DIA": "Dow Jones 30",
    "IWM": "Russell 2000 (small caps)",
    "VT": "Global stocks",
    "VXUS": "International ex-US",
    "AGG": "US aggregate bonds",
}
PERIOD_LABELS = {
    "3M": "past 3 months",
    "6M": "past 6 months",
    "YTD": "year to date",
    "1Y": "past 12 months",
    "3Y": "past 3 years",
    "5Y": "past 5 years",
    "Max": "since first trade",
}

st.set_page_config(page_title="Stock Portfolio Analyzer", page_icon="📈", layout="wide")


@st.cache_data(ttl=3600, show_spinner="Downloading prices, dividends and FX rates from yfinance…")
def cached_market(tickers: tuple[str, ...], start: pd.Timestamp, end: pd.Timestamp) -> MarketData:
    return fetch_market_data(tickers, start, end)


def analyze(source) -> dict | None:
    """Load the CSV, fetch market data and compute the period-independent results."""
    try:
        loaded = load_transactions(source)
    except PortfolioLoadError as exc:
        st.error(str(exc))
        return None

    tx = loaded.transactions
    today = pd.Timestamp.today().normalize()
    start = min(tx["Date"].min(), today - analytics.HISTORY) - pd.Timedelta(days=10)
    try:
        market = cached_market(tuple(sorted(tx["Ticker"].unique())), start, today)
    except PriceDataError as exc:
        st.error(str(exc))
        return None

    trades, warnings = analytics.resolve_trades(tx, market)
    warnings = loaded.warnings + warnings
    if trades.empty:
        st.error("None of the transactions could be priced.")
        return {"warnings": warnings}
    ledger, fifo_warnings = analytics.run_fifo(trades)
    warnings += fifo_warnings
    warnings.sort(key=lambda w: int(w.split()[1].rstrip(":")))

    trades = ledger.trades
    dividends = analytics.dividend_events(trades, market)
    positions = analytics.build_positions(ledger, dividends, market).sort_values(
        ["Market_Value", "Total_Return"], ascending=False)
    return {
        "market": market,
        "download": (start, today),
        "ledger": ledger,
        "dividends": dividends,
        "positions": positions,
        "wealth": analytics.wealth_series(trades, dividends, market),
        "colors": charts.ticker_colors(list(positions["Ticker"])),
        "as_of": market.closes.index[-1],
        "warnings": warnings,
    }


def compare(result: dict, period: str, bench: str) -> dict:
    """Period- and benchmark-dependent results for the Performance tab."""
    market, trades, as_of = result["market"], result["ledger"].trades, result["as_of"]
    if bench not in market.closes.columns:
        try:
            market = merge_market(market, cached_market((bench,), *result["download"]))
        except PriceDataError:
            pass
    has_bench = bench in market.closes.columns

    start = analytics.period_start(period, as_of, trades["Date"].min())
    perf, snapshot = analytics.period_performance(trades, result["dividends"], market, start, as_of)
    tickers = list(perf["Ticker"]) + ([bench] if has_bench and bench not in set(perf["Ticker"]) else [])
    out = {
        "perf": perf,
        "snapshot": snapshot,
        "growth": analytics.growth_series(market.closes, tickers, snapshot),
        "bench": None,
    }
    if has_bench:
        b_trades = analytics.benchmark_trades(trades, market, bench)
        if not b_trades.empty:
            b_divs = analytics.dividend_events(b_trades, market)
            out["bench"] = {
                "perf": analytics.period_performance(b_trades, b_divs, market, start, as_of)[0],
                "wealth": analytics.wealth_series(b_trades, b_divs, market),
            }
    return out


def money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def pct(v: float) -> str:
    return "n/a" if pd.isna(v) else f"{v:+.2f}%"


def bench_label(ticker: str) -> str:
    return f"{ticker} — {BENCHMARKS[ticker]}" if ticker in BENCHMARKS else ticker


MONEY = dict(format="dollar")
st.title("📈 Stock Portfolio Analyzer")
st.caption(f"All amounts in {BASE_CURRENCY}. Non-USD tickers are converted at daily FX rates.")
tab_data, tab_alloc, tab_perf = st.tabs(["📄 Portfolio data", "🥧 Allocation", "📊 Performance"])

with tab_data:
    left, right = st.columns([3, 1])
    with left:
        uploaded = st.file_uploader("Upload your portfolio CSV", type=["csv"])
    with right:
        use_sample = st.toggle("Use sample file", value=SAMPLE_FILE.exists(),
                               disabled=not SAMPLE_FILE.exists(),
                               help=f"Loads {SAMPLE_FILE.name}. An uploaded file always takes precedence.")
    st.caption(f"Required header: `{','.join(REQUIRED_COLUMNS)}`. Transaction_Type is BUY or SELL. "
               "A Price of 0 or blank means *use the yfinance close on that date*; "
               "a typed price is in the ticker's own trading currency.")

with tab_perf:
    ctl_period, ctl_bench = st.columns([3, 2])
    period = ctl_period.segmented_control("Period", list(PERIOD_LABELS), default="1Y", key="period") or "1Y"
    bench = ctl_bench.selectbox(
        "Benchmark", list(BENCHMARKS), format_func=bench_label, accept_new_options=True, key="bench",
        help="Pick an index fund, or type any Yahoo ticker (e.g. ^GSPC, EWJ).",
    )
    bench = (bench or "SPY").strip().upper()

source = uploaded if uploaded is not None else (SAMPLE_FILE if use_sample else None)
result = analyze(source) if source is not None else None
ready = result is not None and "positions" in result

with tab_data:
    if result and result["warnings"]:
        with st.expander(f"⚠️ {len(result['warnings'])} warning(s)", expanded=True):
            st.markdown("\n".join(f"- {w}" for w in result["warnings"]))

    if ready:
        pos = result["positions"]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Market value", money(pos["Market_Value"].sum()))
        c2.metric("Unrealized P/L", money(pos["Unrealized_PL"].sum()))
        c3.metric("Realized P/L", money(pos["Realized_PL"].sum()))
        c4.metric("Dividends received", money(pos["Dividends"].sum()))
        total_bought = result["wealth"]["Total_Bought"].iloc[-1]
        c5.metric("Total return", money(pos["Total_Return"].sum()),
                  pct(pos["Total_Return"].sum() / total_bought * 100) if total_bought else None)

        head_l, head_r = st.columns([3, 1])
        head_l.subheader("Holdings")
        head_l.caption(f"Prices as of {result['as_of']:%Y-%m-%d}. Quantities and average cost are "
                       "split-adjusted (today's shares). Cost basis uses FIFO.")
        head_r.download_button("⬇️ Download holdings (CSV)", pos.to_csv(index=False).encode("utf-8"),
                               file_name=f"holdings_{result['as_of']:%Y%m%d}.csv", mime="text/csv",
                               width="stretch")
        st.dataframe(
            pos,
            hide_index=True,
            width="stretch",
            column_config={
                "Quantity": st.column_config.NumberColumn(format="%.4g"),
                "Avg_Cost": st.column_config.NumberColumn("Avg cost", **MONEY),
                "Current_Price": st.column_config.NumberColumn("Current price", **MONEY),
                "Cost_Basis": st.column_config.NumberColumn("Cost basis", **MONEY),
                "Market_Value": st.column_config.NumberColumn("Market value", **MONEY),
                "Unrealized_PL": st.column_config.NumberColumn("Unrealized P/L", **MONEY),
                "Realized_PL": st.column_config.NumberColumn("Realized P/L", **MONEY),
                "Dividends": st.column_config.NumberColumn("Dividends", **MONEY),
                "Total_Return": st.column_config.NumberColumn("Total return", **MONEY),
                "Total_Return_Pct": st.column_config.NumberColumn("Total return %", format="%.2f%%"),
                "Weight_Pct": st.column_config.NumberColumn("Weight", format="%.1f%%"),
                "First_Purchase": st.column_config.DateColumn("First purchase"),
            },
        )
        ledger = result["ledger"]
        with st.expander(f"Transactions ({len(ledger.trades)})"):
            st.dataframe(ledger.trades.drop(columns=["Split_Factor"]), hide_index=True, width="stretch",
                         column_config={"Date": st.column_config.DateColumn(),
                                        "Price": st.column_config.NumberColumn("Price (USD)", **MONEY),
                                        "Amount": st.column_config.NumberColumn("Amount (USD)", **MONEY),
                                        "Price_Date": st.column_config.DateColumn("Priced on")})
        if not ledger.realized.empty:
            with st.expander(f"Realized sales — FIFO ({len(ledger.realized)})"):
                st.dataframe(ledger.realized, hide_index=True, width="stretch",
                             column_config={"Date": st.column_config.DateColumn(),
                                            "Proceeds": st.column_config.NumberColumn(**MONEY),
                                            "Cost": st.column_config.NumberColumn(**MONEY),
                                            "Realized_PL": st.column_config.NumberColumn("Realized P/L", **MONEY)})
        if not result["dividends"].empty:
            with st.expander(f"Dividends received ({len(result['dividends'])})"):
                st.dataframe(result["dividends"], hide_index=True, width="stretch",
                             column_config={"Ex_Date": st.column_config.DateColumn("Ex-date"),
                                            "Shares": st.column_config.NumberColumn(format="%.4g"),
                                            "Per_Share": st.column_config.NumberColumn("Per share", format="$%.4f"),
                                            "Amount": st.column_config.NumberColumn(**MONEY)})
    elif source is None:
        st.info("Upload a CSV file, or switch on **Use sample file**, to get started.")

with tab_alloc:
    if ready and result["positions"]["Market_Value"].sum() > 0:
        st.plotly_chart(charts.allocation_pie(result["positions"], result["colors"]), width="stretch")
        with st.expander("Table view"):
            open_pos = result["positions"].query("Market_Value > 0")
            st.dataframe(open_pos[["Ticker", "Market_Value", "Weight_Pct"]], hide_index=True,
                         column_config={"Market_Value": st.column_config.NumberColumn("Market value", **MONEY),
                                        "Weight_Pct": st.column_config.NumberColumn("Weight", format="%.1f%%")})
    elif ready:
        st.info("All positions are closed — nothing to allocate.")
    else:
        st.info("Load a portfolio in the **Portfolio data** tab first.")

with tab_perf:
    if not ready:
        st.info("Load a portfolio in the **Portfolio data** tab first.")
    else:
        view = compare(result, period, bench)
        perf, colors, b = view["perf"], result["colors"], view["bench"]
        label = PERIOD_LABELS[period]
        if b is None:
            st.warning(f"Couldn't get price data for benchmark **{bench}** — comparison hidden.")

        first_trade = result["ledger"].trades["Date"].min()
        note = (f" Your first trade was on {first_trade:%Y-%m-%d}, so the period effectively starts there."
                if view["snapshot"] < first_trade and period != "Max" else "")
        st.caption(f"Period: close of {view['snapshot']:%Y-%m-%d} → {result['as_of']:%Y-%m-%d}.{note} "
                   "Profit = change in value + sale proceeds + dividends − purchases. "
                   "Growth % = profit ÷ (value at start + purchases).")

        if perf.empty:
            st.info("You held nothing during this period.")
        else:
            mine = analytics.totals(perf)
            theirs = analytics.totals(b["perf"]) if b else None
            # Only growth % is compared: the benchmark starts the period with a different
            # value than the portfolio, so a raw $ difference would mislead.
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(f"Portfolio profit ({period})", money(mine["profit"]))
            k2.metric(f"Portfolio growth ({period})", pct(mine["growth_pct"]),
                      f"{mine['growth_pct'] - theirs['growth_pct']:+.2f} pts vs {bench}" if theirs else None)
            if theirs:
                k3.metric(f"{bench} profit ({period}, same cash flows)", money(theirs["profit"]))
                k4.metric(f"{bench} growth ({period}, same cash flows)", pct(theirs["growth_pct"]))

            refs = {"Portfolio": (mine["growth_pct"], charts.PORTFOLIO_COLOR, "solid")}
            if theirs:
                refs[bench] = (theirs["growth_pct"], charts.BENCHMARK_COLOR, "dash")
            col1, col2 = st.columns(2)
            col1.plotly_chart(charts.profit_bar(perf, colors, label), width="stretch")
            col2.plotly_chart(charts.growth_bar(perf, colors, label, refs), width="stretch")
            st.plotly_chart(charts.growth_lines(view["growth"], colors, label, benchmark=bench), width="stretch")

            with st.expander("Table view"):
                st.dataframe(perf, hide_index=True, column_config={
                    "Start_Value": st.column_config.NumberColumn("Value at start", **MONEY),
                    "Buys": st.column_config.NumberColumn(**MONEY),
                    "Sells": st.column_config.NumberColumn(**MONEY),
                    "Dividends": st.column_config.NumberColumn(**MONEY),
                    "End_Value": st.column_config.NumberColumn("Value now", **MONEY),
                    "Profit": st.column_config.NumberColumn(f"Profit ({period})", **MONEY),
                    "Growth_Pct": st.column_config.NumberColumn(f"Growth ({period})", format="%.2f%%"),
                })

        st.subheader(f"Since first trade: you vs {bench}")
        st.caption(f"What if every purchase and sale had been made in {bench} instead, on the same days "
                   f"and for the same dollar amounts? Includes dividends on both sides.")
        w = result["wealth"].iloc[-1]
        bought = w["Total_Bought"]
        gain = w["Wealth"] - bought
        i1, i2, i3, _ = st.columns(4)
        i1.metric("Invested (all purchases)", money(bought))
        if b:
            b_gain = b["wealth"]["Wealth"].iloc[-1] - bought
            i2.metric("Your gain", money(gain), f"{money(gain - b_gain)} vs {bench}")
            i3.metric(f"{bench} gain (same cash flows)", money(b_gain))
        else:
            i2.metric("Your gain", money(gain))
        st.plotly_chart(charts.wealth_lines(result["wealth"], b["wealth"] if b else None, bench),
                        width="stretch")
