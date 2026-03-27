"""Data-loading utilities for scanner universes."""

from io import StringIO
import urllib.request

import pandas as pd


def _fetch_html(url):
    """Fetch HTML with User-Agent header."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode("utf-8")


def get_sp500_tickers():
    """Get S&P 500 tickers from Wikipedia."""
    html = _fetch_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    table = pd.read_html(StringIO(html))[0]
    tickers = table["Symbol"].str.replace(".", "-", regex=False).tolist()
    sectors = dict(zip(
        table["Symbol"].str.replace(".", "-", regex=False),
        table["GICS Sector"]
    ))
    return tickers, sectors


def get_sp400_tickers():
    """Get S&P 400 Mid Cap tickers from Wikipedia."""
    html = _fetch_html("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies")
    table = pd.read_html(StringIO(html))[0]
    col = "Symbol" if "Symbol" in table.columns else table.columns[0]
    tickers = table[col].astype(str).str.replace(".", "-", regex=False).tolist()
    return tickers


def get_sp600_tickers():
    """Get S&P 600 Small Cap tickers from Wikipedia."""
    html = _fetch_html("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies")
    table = pd.read_html(StringIO(html))[0]
    col = "Symbol" if "Symbol" in table.columns else table.columns[0]
    tickers = table[col].astype(str).str.replace(".", "-", regex=False).tolist()
    return tickers


def get_sp1500_tickers():
    """Get full S&P 1500 (500 + 400 + 600) - the broad US market."""
    sp500_tickers, sectors = get_sp500_tickers()
    sp400_tickers = get_sp400_tickers()
    sp600_tickers = get_sp600_tickers()

    cap_size = {}
    for t in sp500_tickers:
        cap_size[t] = "large"
    for t in sp400_tickers:
        cap_size[t] = "mid"
    for t in sp600_tickers:
        cap_size[t] = "small"

    all_tickers = list(dict.fromkeys(sp500_tickers + sp400_tickers + sp600_tickers))
    return all_tickers, sectors, cap_size
