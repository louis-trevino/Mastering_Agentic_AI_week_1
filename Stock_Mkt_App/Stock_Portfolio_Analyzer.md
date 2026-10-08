# Stock Portfolio Analyzer

Oct 8, 2026 · Louis C Trevino

## Use Case Overview

The Stock Portfolio Analyzer turns an investor's list of stock trades into a live view of what the portfolio is worth, how each holding has performed, and whether it beat a simple index fund.

### Who it is for

Individual investors who record their buys and sells in a spreadsheet and want analysis without looking up prices by hand. The user needs no finance or programming skills beyond preparing a CSV file.

### The problem

Working out a portfolio's real performance by hand is slow and easy to get wrong. Prices must be looked up for every trade date, stock splits change share counts, foreign stocks need currency conversion, and dividends are easy to forget. A spreadsheet also cannot easily answer the key question: would the same money have done better in an index fund?

### Goals

- Turn a list of transactions into current holdings, cost, market value and profit, priced automatically from Yahoo Finance.
- Show how the portfolio is split across its holdings.
- Show profit and growth per holding over a period the user chooses.
- Compare the portfolio with a benchmark the user chooses, on equal terms.

### Key user scenarios

1. **Review holdings.** The user uploads a CSV and sees each holding's quantity, average cost, current value, realized and unrealized profit, and dividends received.
2. **Check allocation.** The user sees each holding's share of total market value in a pie chart.
3. **Review performance.** The user picks a period (3 months to since the first trade) and sees profit and growth per holding.
4. **Compare with a benchmark.** The user picks an index fund such as SPY or QQQ and sees how the same purchases and sales would have performed in it.
5. **Export.** The user downloads the holdings table as a CSV file.

### Inputs and outputs

|  | Description |
| --- | --- |
| Input | A CSV file with the header `Ticker,Date,Transaction_Type,Quantity,Price`, one row per BUY or SELL. A price of 0 or blank means "use the market closing price on that date". |
| Market data | Daily closing prices, dividends, stock splits and exchange rates, downloaded from Yahoo Finance through the yfinance library. |
| Output | A web page with three tabs (Portfolio data, Allocation, Performance) and a downloadable holdings CSV. All amounts are in US dollars. |

Example input:

```csv
Ticker,Date,Transaction_Type,Quantity,Price
AAPL,2025-01-01,BUY,20,0
MSFT,2025-01-01,BUY,22,0
MSFT,2025-11-03,SELL,10,0
```

### Scope

In scope: BUY and SELL transactions, US and international stocks and ETFs, dividends, stock splits, US dollar reporting, a selectable period and benchmark, and CSV export.

Out of scope for this version:

- Taxes, fees and commissions
- Cash balances, deposits and withdrawals
- Saving portfolios between sessions (the user uploads the file each time)
- Multiple users, logins or connections to brokerage accounts
- Intraday prices (the app uses daily closing prices)

## Solution Description

The solution is a Python web app built with Streamlit. It downloads market data from Yahoo Finance and does all calculations with pandas. It runs locally with one command and needs no database or account.

### Technology stack

| Layer | Choice | Role |
| --- | --- | --- |
| Environment and packages | uv | Creates the virtual environment and installs pinned dependencies (`pyproject.toml`, `uv.lock`) |
| Language | Python 3.12 | All application code |
| User interface | Streamlit | Web page, tabs, file upload, controls and tables |
| Data processing | pandas | Transactions, holdings and performance calculations |
| Market data | yfinance | Daily prices, dividends, splits, currencies and exchange rates from Yahoo Finance |
| Charts | Plotly | Interactive pie, bar and line charts |
| Testing | pytest | Unit tests on synthetic data |

### Architecture

### The three tabs

| Tab | What it shows | User controls |
| --- | --- | --- |
| Portfolio data | Totals (market value, unrealized and realized profit, dividends, total return), a holdings table, and expandable lists of transactions, sales and dividends | CSV upload, sample-file switch, holdings CSV download |
| Allocation | Pie chart of each holding's share of market value, with a table view | None |
| Performance | Profit and growth % per holding, cumulative price growth over time, and a comparison with the benchmark since the first trade | Period (3M, 6M, YTD, 1Y, 3Y, 5Y, Max) and benchmark (list or any ticker) |

### Calculation rules

| Topic | Rule |
| --- | --- |
| Purchase price | A price typed in the CSV is used as given, in the stock's own currency. A blank or 0 price uses that day's closing price, or the next trading day's if the market was closed. |
| Sales | Sales are matched against the oldest shares first (FIFO). A sale larger than the shares held is skipped with a warning. |
| Stock splits | Share counts and typed prices from before a split are adjusted to today's shares. |
| Currency | Everything is reported in US dollars, converted at each day's exchange rate. Prices quoted in pence (London) are divided by 100 first. |
| Dividends | Counted as cash received on shares held at the close before each ex-dividend date. |
| Total return | Unrealized profit + realized profit + dividends. |
| Benchmark | Every purchase and sale is copied as the same dollar amount of the benchmark on the same day, so both sides invest the same money at the same time. |

Profit and growth over a period:

```latex
\text{Profit} = V_{\text{end}} - V_{\text{start}} - \text{Buys} + \text{Sells} + \text{Dividends}
```

```latex
\text{Growth \%} = \frac{\text{Profit}}{V_{\text{start}} + \text{Buys}} \times 100
```

V is the market value at the start and end of the period. Buys, Sells and Dividends count only what happened within the period. For the Max period, growth equals the total return %.

### Validation and error handling

- A file without the required columns is rejected with a message naming the missing columns.
- Rows with a missing ticker, an invalid or future date, an unknown transaction type, a non-positive quantity or an invalid price are skipped and listed as warnings.
- Tickers Yahoo Finance does not recognise are skipped with a warning. Downloads are retried once.
- If the benchmark cannot be downloaded, the comparison is hidden and the rest of the page still works.

### Testing

26 unit tests cover CSV validation, pricing, splits, currency conversion, FIFO sales, dividend timing, period calculations and the benchmark. They use synthetic prices, so they run without internet access. The full app was also run headless against live data for every period and several benchmarks.

### Running the app

```
uv sync
uv run streamlit run app.py
```

The app opens at http://localhost:8501. Run the tests with `uv run pytest`.

### Limitations

- Prices are daily closes, cached for one hour.
- Price history goes back 5 years, or to the first trade if that is earlier.
- Price indices such as ^GSPC pay no dividends, so ETF benchmarks such as SPY compare more fairly.
