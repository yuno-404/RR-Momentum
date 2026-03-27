"""Taiwan market helpers shared by scanner and backtest."""

from __future__ import annotations

from collections import defaultdict
import os
import re
from typing import Dict, Iterable, List, Tuple

import pandas as pd


_ROOT = os.path.dirname(os.path.abspath(__file__))
_TW_REPORTS_DIR = os.path.join(_ROOT, "My-TW-Coverage", "Pilot_Reports")


def is_tw_ticker(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}", str(value).strip()))


def _iter_tw_reports() -> Iterable[Tuple[str, str, str]]:
    if not os.path.isdir(_TW_REPORTS_DIR):
        return
    for sector in sorted(os.listdir(_TW_REPORTS_DIR)):
        sector_dir = os.path.join(_TW_REPORTS_DIR, sector)
        if not os.path.isdir(sector_dir):
            continue
        for filename in sorted(os.listdir(sector_dir)):
            match = re.match(r"^(\d{4})_(.+)\.md$", filename)
            if not match:
                continue
            yield match.group(1), sector, os.path.join(sector_dir, filename)


def _parse_market_cap(filepath: str) -> float | None:
    try:
        with open(filepath, "r", encoding="utf-8") as handle:
            for _ in range(12):
                line = handle.readline()
                if not line:
                    break
                if not line.lstrip().startswith("**"):
                    continue
                match = re.search(r"([0-9][0-9,]*)", line)
                if match:
                    return float(match.group(1).replace(",", ""))
    except OSError:
        return None
    return None


def load_tw_coverage_metadata(
    selected_tickers: Iterable[str] | None = None,
) -> Tuple[List[str], Dict[str, str], Dict[str, str], Dict[str, float]]:
    selected = set(selected_tickers or [])
    tickers: List[str] = []
    sectors: Dict[str, str] = {}
    market_caps: Dict[str, float] = {}

    for ticker, sector, filepath in _iter_tw_reports() or []:
        if selected and ticker not in selected:
            continue
        tickers.append(ticker)
        sectors[ticker] = sector
        market_cap = _parse_market_cap(filepath)
        if market_cap is not None:
            market_caps[ticker] = market_cap

    cap_size: Dict[str, str] = {ticker: "unknown" for ticker in tickers}
    if market_caps:
        cap_series = pd.Series(market_caps, dtype="float64")
        q_large = float(cap_series.quantile(0.8))
        q_mid = float(cap_series.quantile(0.4))
        for ticker in tickers:
            cap = float(market_caps.get(ticker, 0.0))
            if cap >= q_large and cap > 0:
                cap_size[ticker] = "large"
            elif cap >= q_mid and cap > 0:
                cap_size[ticker] = "mid"
            else:
                cap_size[ticker] = "small"

    return tickers, sectors, cap_size, market_caps


def build_tw_sector_leaders(
    sectors: Dict[str, str],
    market_caps: Dict[str, float],
    available_tickers: Iterable[str] | None = None,
) -> Dict[str, str]:
    available = set(available_tickers or sectors.keys())
    grouped: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
    for ticker, sector in sectors.items():
        if ticker not in available:
            continue
        grouped[sector].append((float(market_caps.get(ticker, 0.0)), ticker))

    leaders: Dict[str, str] = {}
    for sector, items in grouped.items():
        items.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if items:
            leaders[sector] = items[0][1]
    return leaders


def _close_series(hist: pd.DataFrame) -> pd.Series:
    close = hist["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return pd.Series(close).dropna()


def build_tw_leader_state(
    all_data: Dict[str, pd.DataFrame],
    sectors: Dict[str, str],
    market_caps: Dict[str, float],
    benchmark_close: pd.Series,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, str]]:
    """Return leader strong flags and scores for each Taiwan ticker by date."""

    if not all_data:
        return pd.DataFrame(), pd.DataFrame(), {}

    dates = pd.Index(sorted(set().union(*(df.index for df in all_data.values()))))
    tickers = sorted(all_data.keys())
    strong_df = pd.DataFrame(False, index=dates, columns=tickers)
    score_df = pd.DataFrame(0.0, index=dates, columns=tickers)
    leaders = build_tw_sector_leaders(sectors, market_caps, tickers)

    bench = benchmark_close.reindex(dates).ffill()
    bench_ret20 = bench / bench.shift(20) - 1.0

    sector_members: Dict[str, List[str]] = defaultdict(list)
    for ticker in tickers:
        sector_members[sectors.get(ticker, "N/A")].append(ticker)

    for sector, leader in leaders.items():
        hist = all_data.get(leader)
        if hist is None:
            continue

        close = _close_series(hist).reindex(dates)
        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()
        ret20 = close / close.shift(20) - 1.0
        rel20 = ret20 - bench_ret20

        cond_close = close > ma20
        cond_stack = ma20 > ma50
        cond_trend = ma50 > ma200
        cond_ret20 = ret20 > 0.03
        cond_rel20 = rel20 > 0.0

        score = (
            cond_close.astype(float)
            + cond_stack.astype(float)
            + cond_trend.astype(float)
            + cond_ret20.astype(float)
            + cond_rel20.astype(float)
        )
        strong = (
            cond_close
            & cond_stack
            & cond_trend
            & (score >= 4.0)
        ).fillna(False)

        members = sector_members.get(sector, [])
        for ticker in members:
            strong_df.loc[:, ticker] = strong.values
            score_df.loc[:, ticker] = score.fillna(0.0).values

    return strong_df.fillna(False), score_df.fillna(0.0), leaders

