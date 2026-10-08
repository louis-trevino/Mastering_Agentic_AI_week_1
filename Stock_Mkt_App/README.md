# Stock Portfolio Analyzer

Streamlit app that reads a portfolio CSV, prices it with yfinance, and shows
holdings, allocation, past-12-month performance and a comparison against SPY.

Designed by: Louis C Trevino, 2026-10-08
Implemented with the help of AI

# App Documentation
[App Document](Stock_Portfolio_Analyzer.md)

## Run

```
uv sync
uv run streamlit run app.py

Application URL on localhost:
http://localhost:8501/

```

Tests: `uv run pytest`

## CSV format

```
Ticker,Date,Transaction_Type,Quantity,Price
AAPL,2025-01-01,BUY,20,0
AAPL,2026-02-02,SELL,5,0
```

- Header is required (case-insensitive; extra columns are ignored).
- `Transaction_Type` is `BUY` or `SELL`. Sales use **FIFO** (oldest shares first);
  a SELL larger than the shares held is skipped with a warning.
- `Price` = 0 or blank → the yfinance close on that date (next trading day if the market was closed).
  A typed price is in the ticker's own trading currency (e.g. CAD for `SHOP.TO`, pence for `VOD.L`).
- Quantities are the shares you actually traded; stock splits after the trade are applied automatically.

Samples: `stock_data_01.csv` (3 buys), `stock_data_02.csv` (sells, a pre-split NVDA buy, CAD and GBp tickers).

## How numbers are calculated

- **Currency:** everything is reported in USD using daily FX rates from Yahoo (`CADUSD=X`, …).
- **Dividends:** cash received on shares held at the close before each ex-date; counted in P/L.
- **Total return** = unrealized P/L + realized P/L + dividends.
- **Period** (Performance tab: 3M, 6M, YTD, 1Y, 3Y, 5Y, Max) starts at the close of the last trading day
  on/before the period start (YTD: Dec 31; Max: before the first trade).
- **Period profit** = value now − value at start − purchases + sale proceeds + dividends (all within the period).
  **Growth %** = that profit ÷ (value at start + purchases in the period). For "Max" this equals total return %.
- **Benchmark** (dropdown, or type any Yahoo ticker): every purchase/sale is mirrored as the same dollar
  amount of the benchmark on the same day. Price indices such as `^GSPC` pay no dividends, so ETFs compare more fairly.

## Layout

| Path | Role |
|---|---|
| `app.py` | Streamlit UI (three tabs) |
| `portfolio/loader.py` | CSV parsing and validation |
| `portfolio/prices.py` | yfinance download: closes, dividends, splits, currency, FX |
| `portfolio/analytics.py` | Trade pricing, FIFO, dividends, positions, performance, benchmark |
| `portfolio/charts.py` | Plotly figures |
| `tests/` | Unit tests (synthetic data, no network) |
