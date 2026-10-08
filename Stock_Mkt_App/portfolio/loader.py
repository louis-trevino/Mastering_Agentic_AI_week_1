"""Read and validate a portfolio transactions CSV."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import IO

import pandas as pd

REQUIRED_COLUMNS = ["Ticker", "Date", "Transaction_Type", "Quantity", "Price"]
SUPPORTED_TYPES = {"BUY", "SELL"}


class PortfolioLoadError(ValueError):
    """The file cannot be used at all (bad header, unreadable, no valid rows)."""


@dataclass
class LoadResult:
    transactions: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


def load_transactions(source: str | IO, today: pd.Timestamp | None = None) -> LoadResult:
    """Parse the CSV into a clean transactions frame.

    Bad rows are skipped and reported as warnings; a bad header raises.
    Output columns: Line, Ticker, Date, Transaction_Type, Quantity, Price
    (Price is NaN when the CSV gave 0/blank, meaning "look it up in yfinance").
    """
    today = (today or pd.Timestamp.today()).normalize()

    try:
        raw = pd.read_csv(source, dtype=str, skipinitialspace=True)
    except Exception as exc:  # pandas raises several types for malformed files
        raise PortfolioLoadError(f"Could not read the CSV file: {exc}") from exc

    # Match the header case-insensitively and map to canonical names.
    by_lower = {str(c).strip().lower(): c for c in raw.columns}
    missing = [c for c in REQUIRED_COLUMNS if c.lower() not in by_lower]
    if missing:
        raise PortfolioLoadError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Expected header: {','.join(REQUIRED_COLUMNS)}"
        )
    df = raw[[by_lower[c.lower()] for c in REQUIRED_COLUMNS]].copy()
    df.columns = REQUIRED_COLUMNS
    df = df.apply(lambda s: s.str.strip())
    df.insert(0, "Line", df.index + 2)  # +1 for header, +1 for 1-based lines

    df["Ticker"] = df["Ticker"].str.upper()
    df["Transaction_Type"] = df["Transaction_Type"].str.upper()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce")
    price_text = df["Price"].fillna("")
    df["Price"] = pd.to_numeric(df["Price"], errors="coerce")

    warnings: list[str] = []
    checks = [
        (df["Ticker"].isna() | (df["Ticker"] == ""), "missing ticker"),
        (df["Date"].isna(), "invalid date"),
        (df["Date"] > today, "date is in the future"),
        (~df["Transaction_Type"].isin(SUPPORTED_TYPES),
         "transaction type must be BUY or SELL"),
        (df["Quantity"].isna() | (df["Quantity"] <= 0), "quantity must be a positive number"),
        ((price_text != "") & df["Price"].isna(), "price is not a number"),
        (df["Price"] < 0, "price cannot be negative"),
    ]
    bad = pd.Series(False, index=df.index)
    for mask, reason in checks:
        mask = mask.fillna(False) & ~bad  # report only the first problem per row
        for line in df.loc[mask, "Line"]:
            warnings.append(f"Line {line}: {reason} - row skipped.")
        bad |= mask

    clean = df.loc[~bad].copy()
    if clean.empty:
        raise PortfolioLoadError("No valid transactions found in the file.")

    # 0 means "unknown purchase price": resolve it from market data later.
    clean.loc[clean["Price"] == 0, "Price"] = float("nan")
    warnings.sort(key=lambda w: int(w.split()[1].rstrip(":")))
    return LoadResult(clean.reset_index(drop=True), warnings)
