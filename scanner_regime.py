"""Market regime checks."""

import numpy as np
import pandas as pd
import yfinance as yf


def scan_market_regime(market="us"):
    """Assess overall market regime for the selected market."""
    if market == "tw":
        indices = {"^TWII": "TAIEX"}
    else:
        indices = {"^GSPC": "S&P 500", "^IXIC": "NASDAQ"}
    results = {}

    for symbol, name in indices.items():
        hist = yf.download(symbol, period="2y", progress=False, auto_adjust=True)
        if hist is None or len(hist) == 0:
            continue
        if "Close" not in hist:
            continue
        close = hist["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = pd.Series(close).dropna()
        if len(close) < 220:
            continue
        ma50 = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()

        close_vals = np.asarray(close, dtype=float)
        ma50_vals = np.asarray(ma50, dtype=float)
        ma200_vals = np.asarray(ma200, dtype=float)

        current = float(close_vals[-1])
        current_ma50 = float(ma50_vals[-1])
        current_ma200 = float(ma200_vals[-1])
        prev_ma50 = float(ma50_vals[-20])
        prev_ma200 = float(ma200_vals[-20])

        results[name] = {
            "price": round(current, 2),
            "ma50": round(current_ma50, 2),
            "ma200": round(current_ma200, 2),
            "above_ma50": current > current_ma50,
            "above_ma200": current > current_ma200,
            "ma50_above_ma200": current_ma50 > current_ma200,
            "ma50_rising": current_ma50 > prev_ma50,
            "ma200_rising": current_ma200 > prev_ma200,
        }

    all_bullish = all(
        r["above_ma50"] and r["above_ma200"] and r["ma50_above_ma200"]
        for r in results.values()
    )
    all_bearish = all(
        not r["above_ma50"] and not r["ma50_above_ma200"]
        for r in results.values()
    )

    if all_bullish:
        regime = "UPTREND"
    elif all_bearish:
        regime = "DOWNTREND"
    else:
        regime = "CHOPPY"

    return regime, results
