"""Plotly figure builders. Colors follow the ticker, never its rank."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# Validated categorical palette (fixed order). A 9th+ ticker folds into "Other".
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
OTHER_COLOR = "#8a8984"
MAX_SLICES = len(PALETTE)

# Non-ticker series: told apart by dash style as well as color.
PORTFOLIO_COLOR = "#9085e9"
BENCHMARK_COLOR = "#8a8984"
INVESTED_COLOR = "#b5b4ad"
GRID = "rgba(128,128,128,0.15)"

_LAYOUT = dict(
    margin=dict(l=8, r=8, t=48, b=8),
    font=dict(size=13),
    hoverlabel=dict(font_size=13),
)


def ticker_colors(tickers: list[str]) -> dict[str, str]:
    """Ticker -> color map, in the order given (largest position first).

    With more tickers than palette slots, the top MAX_SLICES-1 get colors and
    the rest share the grey used by the pie's "Other" slice.
    """
    n_colored = len(tickers) if len(tickers) <= MAX_SLICES else MAX_SLICES - 1
    return {t: PALETTE[i] if i < n_colored else OTHER_COLOR for i, t in enumerate(tickers)}


def allocation_pie(positions: pd.DataFrame, colors: dict[str, str]) -> go.Figure:
    data = positions.loc[positions["Market_Value"] > 0, ["Ticker", "Market_Value"]]
    data = data.sort_values("Market_Value", ascending=False)
    if len(data) > MAX_SLICES:
        head = data.head(MAX_SLICES - 1)
        other = pd.DataFrame([{"Ticker": "Other", "Market_Value": data["Market_Value"].iloc[MAX_SLICES - 1:].sum()}])
        data = pd.concat([head, other], ignore_index=True)

    fig = go.Figure(go.Pie(
        labels=data["Ticker"],
        values=data["Market_Value"],
        hole=0.45,
        sort=False,
        direction="clockwise",
        marker=dict(colors=[colors.get(t, OTHER_COLOR) for t in data["Ticker"]],
                    line=dict(color="rgba(0,0,0,0)", width=2)),
        textinfo="label+percent",
        textposition="outside",
        hovertemplate="<b>%{label}</b><br>Value: $%{value:,.2f}<br>Share: %{percent}<extra></extra>",
    ))
    fig.update_layout(title="Portfolio share by market value", showlegend=True, **_LAYOUT)
    return fig


def _bar(perf: pd.DataFrame, column: str, colors: dict[str, str], title: str,
         value_fmt: str, hover_fmt: str) -> go.Figure:
    data = perf.sort_values(column, ascending=False)
    fig = go.Figure(go.Bar(
        x=data["Ticker"],
        y=data[column],
        marker=dict(color=[colors.get(t, OTHER_COLOR) for t in data["Ticker"]], cornerradius=4),
        text=[value_fmt.format(v) for v in data[column]],
        textposition="outside",
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>" + hover_fmt + "<extra></extra>",
    ))
    fig.add_hline(y=0, line_width=1, line_color=OTHER_COLOR)
    fig.update_layout(title=title, showlegend=False, bargap=0.45, **_LAYOUT)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, zeroline=False)
    return fig


def profit_bar(perf: pd.DataFrame, colors: dict[str, str], period: str) -> go.Figure:
    fig = _bar(perf, "Profit", colors, f"Profit per ticker — {period} (incl. dividends)",
               "${:+,.0f}", "Profit: $%{y:+,.2f}")
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return fig


def growth_bar(perf: pd.DataFrame, colors: dict[str, str], period: str,
               references: dict[str, tuple[float, str, str]] | None = None) -> go.Figure:
    """Growth % bars, plus optional horizontal reference lines
    {label: (value, color, dash)} e.g. the whole portfolio and the benchmark."""
    fig = _bar(perf, "Growth_Pct", colors, f"Growth % per ticker — {period} (incl. dividends)",
               "{:+.1f}%", "Growth: %{y:+.2f}%")
    for label, (value, color, dash) in (references or {}).items():
        if pd.notna(value):
            fig.add_hline(y=value, line_width=2, line_color=color, line_dash=dash,
                          annotation_text=f"{label} {value:+.1f}%", annotation_position="top right")
    fig.update_yaxes(ticksuffix="%")
    return fig


def growth_lines(series: pd.DataFrame, colors: dict[str, str], period: str,
                 benchmark: str | None = None) -> go.Figure:
    fig = go.Figure()
    for ticker, grp in series.groupby("Ticker", sort=True):
        is_bench = ticker == benchmark
        fig.add_trace(go.Scatter(
            x=grp["Date"], y=grp["Growth_Pct"], name=f"{ticker} (benchmark)" if is_bench else ticker,
            mode="lines",
            line=dict(color=BENCHMARK_COLOR if is_bench else colors.get(ticker, OTHER_COLOR),
                      width=2, dash="dash" if is_bench else "solid"),
            hovertemplate=f"<b>{ticker}</b> %{{y:+.2f}}%<extra></extra>",
        ))
    fig.add_hline(y=0, line_width=1, line_color=OTHER_COLOR)
    fig.update_layout(title=f"Cumulative price growth — {period}", hovermode="x unified",
                      legend=dict(orientation="h", y=-0.15), **_LAYOUT)
    fig.update_yaxes(ticksuffix="%", showgrid=True, gridcolor=GRID, zeroline=False)
    fig.update_xaxes(showgrid=False)
    return fig


def wealth_lines(portfolio: pd.DataFrame, benchmark: pd.DataFrame | None, benchmark_name: str) -> go.Figure:
    """Portfolio vs the same cash flows invested in the benchmark, since the first trade."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=portfolio.index, y=portfolio["Net_Invested"], name="Net invested", mode="lines",
        line=dict(color=INVESTED_COLOR, width=2, dash="dot", shape="hv"),
        hovertemplate="Net invested $%{y:,.0f}<extra></extra>",
    ))
    if benchmark is not None:
        fig.add_trace(go.Scatter(
            x=benchmark.index, y=benchmark["Wealth"], name=f"{benchmark_name} (same cash flows)", mode="lines",
            line=dict(color=BENCHMARK_COLOR, width=2, dash="dash"),
            hovertemplate=f"{benchmark_name} $%{{y:,.0f}}<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=portfolio.index, y=portfolio["Wealth"], name="Your portfolio", mode="lines",
        line=dict(color=PORTFOLIO_COLOR, width=2),
        hovertemplate="Portfolio $%{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(title="Portfolio vs benchmark — value incl. cash from sales & dividends",
                      hovermode="x unified", legend=dict(orientation="h", y=-0.15), **_LAYOUT)
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", showgrid=True, gridcolor=GRID, zeroline=False)
    fig.update_xaxes(showgrid=False)
    return fig
