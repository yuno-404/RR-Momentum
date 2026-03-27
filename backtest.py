"""10Y momentum strategy backtest aligned with the scanner logic.

Strategy defaults (aligned with your rules):
- Entry requires the same 5 filters as the scanner
- Per-trade stop defaults to the latest VCP contraction depth
- Per-trade capital allocation is fixed at 12.5% of total equity
- Portfolio uses max 8 concurrent positions
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import argparse
import hashlib
import json
import math
import os
import pickle
import time
from typing import Dict, List, Tuple

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import pandas as pd
import yfinance as yf

from scanner_data import get_sp1500_tickers, get_sp500_tickers
from scanner_metrics import detect_resistance_3y, assess_resistance_status
from scanner_tw import (
    build_tw_leader_state,
    is_tw_ticker,
    load_tw_coverage_metadata,
)

# ---------------------------------------------------------------------------
# OHLCV cache
# ---------------------------------------------------------------------------
_CACHE_DIR = "backtest_cache"


def _ohlcv_cache_path(tickers: List[str], years: int, market: str = "us") -> str:
    sig = hashlib.md5(f"{market}|{sorted(tickers)}|{years}".encode()).hexdigest()[:12]
    return os.path.join(_CACHE_DIR, f"ohlcv_{sig}.pkl")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Position:
    ticker: str
    entry_date: str
    entry_price: float
    shares: float
    stop_price: float
    risk_per_share: float
    regime: str = "UPTREND"
    moved_to_breakeven: bool = False
    trend_exit_active: bool = False
    partial_exit_taken: bool = False


@dataclass
class SignalPoint:
    rr_value: float
    pivot: float
    stop_pct: float


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest momentum scanner strategy")
    parser.add_argument("--universe", default="sp500",
                        help="sp500, sp1500, or comma-separated tickers")
    parser.add_argument("--years", type=int, default=10, help="Backtest years")
    parser.add_argument("--initial-capital", type=float, default=100000.0,
                        help="Initial portfolio capital")
    parser.add_argument("--min-rs", type=int, default=85,
                        help="Min RS rank for mid/small caps")
    parser.add_argument("--rr-min", type=float, default=0.0,
                        help="Optional minimum R:R gate (default: 0 = disabled to match scanner)")
    parser.add_argument("--rr-grid", default="",
                        help="Comma-separated rr_min batch values, e.g. 2.5,3,3.5")
    parser.add_argument("--stop-pct", type=float, default=0.08,
                        help="Fallback fixed stop percent used only when VCP stop is unavailable")
    parser.add_argument("--position-pct", type=float, default=0.125,
                        help="Fixed position percent (0.125 = 12.5%%)")
    parser.add_argument("--max-positions", type=int, default=8,
                        help="Maximum concurrent positions")
    parser.add_argument("--workers", type=int,
                        default=max(2, (os.cpu_count() or 4) // 2),
                        help="Worker count")
    parser.add_argument("--engine", default="process",
                        choices=["process", "thread", "single"],
                        help="Signal precompute engine")
    parser.add_argument("--output", default="backtest_results",
                        help="Output folder (batch mode creates rr_* subfolders)")
    parser.add_argument("--breakout-buffer-pct", type=float, default=0.0,
                        help="Breakout buffer above pivot (0.0 = pivot)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Skip OHLCV cache, force re-download")
    parser.add_argument("--f4-exit-days", type=int, default=0,
                        help="Exit after N consecutive F4-fail days (0=off)")
    parser.add_argument("--f3-exit-days", type=int, default=0,
                        help="Exit after N consecutive F3/RS-fail days (0=off)")
    parser.add_argument("--regime-filter", default="on",
                        choices=["on", "off"],
                        help="Enable benchmark regime gate (default: on)")
    parser.add_argument("--choppy-max-positions", type=int, default=4,
                        help="Max positions during CHOPPY regime (default: 4)")
    parser.add_argument("--choppy-capital-fraction", type=float, default=0.5,
                        help="Maximum capital deployed during CHOPPY regime")
    return parser.parse_args()


def _parse_rr_values(args: argparse.Namespace) -> List[float]:
    raw = str(getattr(args, "rr_grid", "") or "").strip()
    if not raw:
        return [float(args.rr_min)]

    values: List[float] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        values.append(float(token))
    if not values:
        return [float(args.rr_min)]
    return values


def _rr_slug(rr_value: float) -> str:
    text = f"{rr_value:g}"
    return text.replace("-", "m").replace(".", "_")


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def _resolve_universe(
    universe: str,
) -> Tuple[List[str], Dict[str, str], Dict[str, str], Dict[str, float], str]:
    cap_size: Dict[str, str] = {}
    market_caps: Dict[str, float] = {}
    if universe == "sp1500":
        tickers, sectors, cap_size = get_sp1500_tickers()
        market = "us"
    elif universe == "sp500":
        tickers, sectors = get_sp500_tickers()
        cap_size = {t: "large" for t in tickers}
        market = "us"
    elif universe in {"tw", "tw_all", "taiwan", "twse"}:
        tickers, sectors, cap_size, market_caps = load_tw_coverage_metadata()
        market = "tw"
    else:
        tickers = [t.strip().upper() for t in universe.split(",") if t.strip()]
        if tickers and all(is_tw_ticker(t) for t in tickers):
            _, known_sectors, known_caps, market_caps = load_tw_coverage_metadata(tickers)
            sectors = {t: known_sectors.get(t, "N/A") for t in tickers}
            cap_size = {t: known_caps.get(t, "unknown") for t in tickers}
            market = "tw"
        else:
            sectors = {t: "N/A" for t in tickers}
            cap_size = {t: "unknown" for t in tickers}
            market = "us"
    return tickers, sectors, cap_size, market_caps, market


# ---------------------------------------------------------------------------
# Data download & caching
# ---------------------------------------------------------------------------

def _extract_ticker_frame(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if isinstance(getattr(data, "columns", None), pd.MultiIndex):
        frame = data.xs(ticker, level=1, axis=1)
    else:
        frame = data
    cols = [c for c in ["Open", "High", "Low", "Close", "Volume"]
            if c in frame.columns]
    return frame[cols].dropna()


def _download_ohlcv(tickers: List[str], years: int) -> Dict[str, pd.DataFrame]:
    all_data: Dict[str, pd.DataFrame] = {}
    batch_size = 80
    period = f"{years + 1}y"
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        batch_str = " ".join(batch)
        try:
            raw = yf.download(
                batch_str, period=period, auto_adjust=True,
                progress=False, threads=True,
            )
        except Exception:
            continue
        if raw is None or len(raw) == 0:
            continue
        for ticker in batch:
            try:
                frame = _extract_ticker_frame(raw, ticker)
                if len(frame) >= 350:
                    all_data[ticker] = frame
            except Exception:
                continue
    return all_data


def _download_tw_ohlcv(tickers: List[str], years: int) -> Dict[str, pd.DataFrame]:
    all_data: Dict[str, pd.DataFrame] = {}
    batch_size = 80
    period = f"{years + 1}y"
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        unresolved = list(batch)
        for suffix in (".TW", ".TWO"):
            if not unresolved:
                break
            symbols = [f"{ticker}{suffix}" for ticker in unresolved]
            try:
                raw = yf.download(
                    " ".join(symbols),
                    period=period,
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
            except Exception:
                continue
            if raw is None or len(raw) == 0:
                continue

            resolved: List[str] = []
            for ticker, symbol in zip(unresolved, symbols):
                try:
                    frame = _extract_ticker_frame(raw, symbol)
                except Exception:
                    continue
                if len(frame) >= 350:
                    all_data[ticker] = frame
                    resolved.append(ticker)
            unresolved = [ticker for ticker in unresolved if ticker not in resolved]
    return all_data


def _download_ohlcv_cached(
    tickers: List[str], years: int, *, no_cache: bool = False, market: str = "us",
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV with a local pickle cache (auto-refreshed every 24 h)."""
    cache = _ohlcv_cache_path(tickers, years, market)
    if not no_cache and os.path.exists(cache):
        age_h = (time.time() - os.path.getmtime(cache)) / 3600
        if age_h < 24:
            print(f"  Loading from cache ({age_h:.1f} h old)")
            with open(cache, "rb") as f:
                return pickle.load(f)
    data = _download_tw_ohlcv(tickers, years) if market == "tw" else _download_ohlcv(tickers, years)
    if data:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(cache, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    return data


def _benchmark_cache_path(years: int, market: str) -> str:
    return os.path.join(_CACHE_DIR, f"bench_{market}_{years}y.pkl")


def _download_benchmark(years: int, market: str = "us", *, no_cache: bool = False) -> pd.Series:
    cache = _benchmark_cache_path(years, market)
    if not no_cache and os.path.exists(cache):
        age_h = (time.time() - os.path.getmtime(cache)) / 3600
        if age_h < 24:
            with open(cache, "rb") as f:
                return pickle.load(f)

    symbol = "^TWII" if market == "tw" else "^GSPC"
    bench = yf.download(
        symbol, period=f"{years + 1}y", auto_adjust=True, progress=False,
    )
    close = bench["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    result = pd.Series(close).dropna()

    if len(result) > 0:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(cache, "wb") as f:
            pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        # Fall back to stale cache if available
        if os.path.exists(cache):
            print(f"  WARNING: {symbol} download failed, using stale cache")
            with open(cache, "rb") as f:
                return pickle.load(f)
        raise RuntimeError(
            f"Benchmark {symbol} download failed (rate limit?) and no cache exists"
        )
    return result


def _download_regime_series(years: int, market: str = "us", *, no_cache: bool = False) -> Dict[str, pd.Series]:
    cache = os.path.join(_CACHE_DIR, f"regime_{market}_{years}y.pkl")
    if not no_cache and os.path.exists(cache):
        age_h = (time.time() - os.path.getmtime(cache)) / 3600
        if age_h < 24:
            with open(cache, "rb") as f:
                return pickle.load(f)

    symbols = {"TAIEX": "^TWII"} if market == "tw" else {"S&P 500": "^GSPC", "NASDAQ": "^IXIC"}
    out: Dict[str, pd.Series] = {}
    for name, symbol in symbols.items():
        hist = yf.download(
            symbol, period=f"{years + 1}y", auto_adjust=True, progress=False,
        )
        if hist is None or len(hist) == 0 or "Close" not in hist:
            continue
        close = hist["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        out[name] = pd.Series(close).dropna()

    if out:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(cache, "wb") as f:
            pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
    elif os.path.exists(cache):
        print("  WARNING: regime download failed, using stale cache")
        with open(cache, "rb") as f:
            return pickle.load(f)
    return out


def _build_market_regime_series(series_map: Dict[str, pd.Series]) -> pd.Series:
    """Mirror scanner_regime logic across time for backtests."""
    if not series_map:
        return pd.Series(dtype="object")

    all_dates = pd.Index(sorted(set().union(*(series.index for series in series_map.values()))))
    states: Dict[str, pd.DataFrame] = {}

    for name, close in series_map.items():
        aligned = close.reindex(all_dates).ffill()
        ma50 = aligned.rolling(50).mean()
        ma200 = aligned.rolling(200).mean()
        states[name] = pd.DataFrame({
            "above_ma50": aligned > ma50,
            "above_ma200": aligned > ma200,
            "ma50_above_ma200": ma50 > ma200,
        }, index=all_dates)

    regime = pd.Series("CHOPPY", index=all_dates)
    bullish = pd.Series(True, index=all_dates)
    bearish = pd.Series(True, index=all_dates)
    for state in states.values():
        bullish &= (
            state["above_ma50"].fillna(False)
            & state["above_ma200"].fillna(False)
            & state["ma50_above_ma200"].fillna(False)
        )
        bearish &= (
            (~state["above_ma50"].fillna(False))
            & (~state["ma50_above_ma200"].fillna(False))
        )

    regime[bullish] = "UPTREND"
    regime[bearish] = "DOWNTREND"
    return regime


# ---------------------------------------------------------------------------
# Daily pre-filter (vectorised)
# ---------------------------------------------------------------------------

def _build_daily_prefilter(
    all_data: Dict[str, pd.DataFrame],
    benchmark_close: pd.Series,
    cap_size: Dict[str, str],
    min_rs: int,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tickers = sorted(all_data.keys())
    close_df = pd.DataFrame(
        {t: all_data[t]["Close"] for t in tickers},
    ).sort_index()
    high_df = pd.DataFrame(
        {t: all_data[t]["High"] for t in tickers},
    ).reindex(close_df.index)
    low_df = pd.DataFrame(
        {t: all_data[t]["Low"] for t in tickers},
    ).reindex(close_df.index)

    high_52w = high_df.rolling(252).max()
    f1 = ((high_52w - close_df) / high_52w) <= 0.25

    base_low = low_df.rolling(252).min()
    f2 = ((close_df - base_low) / base_low) >= 0.25

    ma50 = close_df.rolling(50).mean()
    ma200 = close_df.rolling(200).mean()
    f4 = (ma50 > ma200) & (ma50 > ma50.shift(20)) & (ma200 > ma200.shift(20))

    q1 = close_df / close_df.shift(63) - 1.0
    q2 = close_df.shift(63) / close_df.shift(126) - 1.0
    q3 = close_df.shift(126) / close_df.shift(189) - 1.0
    q4 = close_df.shift(189) / close_df.shift(252) - 1.0
    rs_score = 0.4 * q1 + 0.2 * q2 + 0.2 * q3 + 0.2 * q4
    rs_rank = rs_score.rank(axis=1, pct=True) * 99

    bench = benchmark_close.reindex(close_df.index).ffill()
    rs_line = close_df.div(bench, axis=0)
    recent_rs = rs_line / rs_line.shift(63) - 1.0
    older_rs = rs_line.shift(63) / rs_line.shift(126) - 1.0
    rs_ma50 = rs_line.rolling(50).mean()
    rs_rising = (
        (recent_rs > 0)
        & (recent_rs >= older_rs)
        & (rs_line > rs_ma50)
        & (rs_ma50 > rs_ma50.shift(20))
    )

    # Vectorised threshold gate (replaces per-ticker Python loop)
    thresholds = pd.Series(
        {t: 70.0 if cap_size.get(t, "unknown") == "large" else float(min_rs)
         for t in tickers},
    )
    rs_gate = (rs_rank >= thresholds) & rs_rising

    prefilter = f1 & f2 & f4 & rs_gate
    return (
        prefilter.fillna(False),
        rs_rank.fillna(0),
        f4.fillna(False),
    )


# ---------------------------------------------------------------------------
# Fast VCP + R:R on numpy arrays  (backtest hot-path)
# ---------------------------------------------------------------------------

def _vcp_rr_fast(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    vol: np.ndarray,
    vol50: np.ndarray,
    end: int,
    rr_min: float,
    market: str,
) -> Tuple[float, float, float]:
    """Combined VCP detection + R:R scoring on pre-extracted numpy arrays.

    ``end`` is the exclusive upper bound (equivalent to ``hist.iloc[:end]``).

    Returns ``(rr_value, pivot, stop_pct)`` when VCP passes,
    else ``(0.0, 0.0, 0.0)``.
    """
    _ZERO = (0.0, 0.0, 0.0)

    is_tw = market == "tw"
    lookback = min(260 if is_tw else 200, end)
    if lookback < 60:
        return _ZERO

    s = end - lookback
    h = high[s:end]
    lo = low[s:end]
    c = close[s:end]
    v = vol[s:end]
    n = len(h)
    order = 5

    # ---- Vectorised swing-point detection via sliding_window_view ----
    window = 2 * order + 1  # 11
    h_padded = np.pad(h, order, mode="edge")
    l_padded = np.pad(lo, order, mode="edge")
    h_rm = sliding_window_view(h_padded, window).max(axis=1)
    l_rm = sliding_window_view(l_padded, window).min(axis=1)

    sh_mask = np.zeros(n, dtype=bool)
    sl_mask = np.zeros(n, dtype=bool)
    sh_mask[order:] = h[order:] >= h_rm[order:]
    sl_mask[order:] = lo[order:] <= l_rm[order:]

    sh_idx = np.where(sh_mask)[0]
    sl_idx = np.where(sl_mask)[0]

    if len(sh_idx) + len(sl_idx) < 4:
        return _ZERO

    # ---- Build alternating swing list ----
    all_swings: list = []
    for i in sh_idx:
        all_swings.append((int(i), float(h[i]), True))   # True = High
    for i in sl_idx:
        all_swings.append((int(i), float(lo[i]), False))  # False = Low
    all_swings.sort(key=lambda x: x[0])

    filtered = [all_swings[0]]
    for pt in all_swings[1:]:
        if filtered[-1][2] != pt[2]:
            filtered.append(pt)
        elif pt[2] and pt[1] > filtered[-1][1]:
            filtered[-1] = pt
        elif (not pt[2]) and pt[1] < filtered[-1][1]:
            filtered[-1] = pt

    # ---- H->L contractions ----
    contractions: list = []
    for i in range(len(filtered) - 1):
        if filtered[i][2] and not filtered[i + 1][2]:
            h_i, h_p = filtered[i][0], filtered[i][1]
            l_i, l_p = filtered[i + 1][0], filtered[i + 1][1]
            if h_p <= 0:
                continue
            depth = (h_p - l_p) / h_p * 100.0
            # Only count down-day volume (selling pressure)
            if l_i > h_i:
                c_vol = v[h_i:l_i + 1]
                c_close = c[h_i:l_i + 1]
                down_mask = np.zeros(len(c_close), dtype=bool)
                down_mask[1:] = c_close[1:] < c_close[:-1]
                avg_v = float(np.mean(c_vol[down_mask])) if down_mask.any() else float(np.mean(c_vol))
            else:
                avg_v = 0.0
            contractions.append((depth, h_i, l_i, h_p, l_p, avg_v))

    if len(contractions) < (2 if is_tw else 3):
        return _ZERO

    if len(contractions) >= 3:
        selected = contractions[-3:]
    else:
        selected = contractions[-2:]

    c1 = selected[0]
    c_last = selected[-1]
    c1d = c1[0]
    c2d = selected[1][0] if len(selected) >= 2 else 0.0
    c3d = selected[2][0] if len(selected) >= 3 else c_last[0]

    # ---- Early-exit checks (ordered by fail likelihood) ----
    if len(selected) >= 3:
        decreasing_ok = (c1d > c2d) and (c3d <= c2d + (3.0 if is_tw else -1e-9))
    else:
        decreasing_ok = c1d > c2d
    if not decreasing_ok:
        return _ZERO
    if c3d < 1.0:
        return _ZERO
    if c3d > (12 if is_tw else 10):
        return _ZERO
    if not ((8 <= c1d <= 45) if is_tw else (10 <= c1d <= 40)):
        return _ZERO
    if c_last[4] < c1[4] * (0.95 if is_tw else 1.0):
        return _ZERO
    if c_last[5] > c1[5] * (1.15 if is_tw else 1.0):
        return _ZERO

    # Volume dry-up in last contraction (down days only = selling pressure)
    c3_h_abs = s + c_last[1]
    c3_l_abs = s + c_last[2]
    if c3_l_abs <= c3_h_abs:
        return _ZERO
    c3_v = vol[c3_h_abs:c3_l_abs + 1]
    c3_v50 = vol50[c3_h_abs:c3_l_abs + 1]
    c3_close = close[c3_h_abs:c3_l_abs + 1]
    down_mask = np.zeros(len(c3_close), dtype=bool)
    down_mask[1:] = c3_close[1:] < c3_close[:-1]
    if down_mask.any():
        c3_v_down = c3_v[down_mask]
        c3_v50_down = c3_v50[down_mask]
        nan_mask = ~np.isnan(c3_v50_down)
        has_dryup = nan_mask.sum() > 0 and np.any(c3_v_down[nan_mask] < c3_v50_down[nan_mask])
        if is_tw and not has_dryup and nan_mask.sum() > 0:
            has_dryup = float(np.mean(c3_v_down[nan_mask])) <= float(np.mean(c3_v50_down[nan_mask])) * 1.05
    else:
        # No down days in last contraction = no selling pressure (bullish)
        has_dryup = True
    if not has_dryup:
        return _ZERO

    # ---- VCP passed - compute R:R ----
    current = float(close[end - 1])
    stop_pct = c3d / 100.0
    if stop_pct <= 0 or current <= 0:
        return _ZERO

    # R:R to 52-week high
    hi_start = max(0, end - 252)
    high_52w = float(np.nanmax(high[hi_start:end]))
    rr_52w = max(0.0, (high_52w - current) / current) / stop_pct

    # R:R breakout projection
    pivot = float(c_last[3])
    rr_bo = 0.0
    if pivot > 0 and c1d > 0:
        target_bo = pivot * (1.0 + c1d / 100.0)
        rr_bo = max(0.0, (target_bo - current) / current) / stop_pct

    rr_value = max(rr_52w, rr_bo)
    if rr_min > 0 and rr_value < rr_min:
        return _ZERO
    return (rr_value, pivot, float(stop_pct * 100.0))


# ---------------------------------------------------------------------------
# Worker function for parallel signal precomputation
# ---------------------------------------------------------------------------

def _precompute_signals_fast(
    args: Tuple[str, np.ndarray, np.ndarray, np.ndarray,
                np.ndarray, np.ndarray, List[int], List[int], float, str],
) -> Tuple[str, Dict[int, Tuple[float, float, float]]]:
    """Compute VCP + R:R for one ticker on pre-extracted numpy arrays.

    ``ticker_indices`` are positions in the ticker's own array (for VCP).
    ``output_indices`` are the corresponding positions in the prefilter/close_df
    index (for the simulation loop).
    """
    ticker, high, low, close, vol, vol50, ticker_indices, output_indices, rr_min, market = args
    out: Dict[int, Tuple[float, float, float]] = {}
    for ticker_i, out_i in zip(ticker_indices, output_indices):
        if ticker_i < 300:
            continue
        end = ticker_i + 1
        if end > len(high):
            continue
        rr, pivot, stop_pct = _vcp_rr_fast(high, low, close, vol, vol50, end, rr_min, market)
        if stop_pct > 0:
            out[out_i] = (rr, pivot, stop_pct)
    return ticker, out


def _build_signal_map(
    all_data: Dict[str, pd.DataFrame],
    prefilter: pd.DataFrame,
    rr_min: float,
    workers: int,
    engine: str,
    market: str,
) -> Dict[str, Dict[int, SignalPoint]]:
    # Pre-extract numpy arrays (much faster to pickle than DataFrames)
    # Map prefilter positions (in close_df union index) to each ticker's own
    # positional index so VCP looks at the correct data rows.
    pf_index = prefilter.index
    tasks: list = []
    for ticker, hist in all_data.items():
        h = hist["High"].values.astype(np.float64)
        lo = hist["Low"].values.astype(np.float64)
        c = hist["Close"].values.astype(np.float64)
        v = hist["Volume"].values.astype(np.float64)
        v50 = pd.Series(v).rolling(50).mean().values

        pf_positions = np.where(prefilter[ticker].values)[0]
        if len(pf_positions) == 0:
            continue
        # Map prefilter dates -> ticker's own positional indices
        pf_dates = pf_index[pf_positions]
        ticker_positions = hist.index.get_indexer(pf_dates)
        valid = ticker_positions >= 0
        ticker_idxs = ticker_positions[valid].tolist()
        output_idxs = pf_positions[valid].tolist()
        if ticker_idxs:
            tasks.append((ticker, h, lo, c, v, v50, ticker_idxs, output_idxs, rr_min, market))

    total_cand = sum(len(t[6]) for t in tasks)
    print(f"  Tickers with candidates: {len(tasks)} | "
          f"Total candidate-days: {total_cand:,}")

    # Convert raw tuples back to SignalPoint
    def _collect(raw_map: Dict[int, Tuple[float, float, float]]) -> Dict[int, SignalPoint]:
        return {k: SignalPoint(rr_value=v[0], pivot=v[1], stop_pct=v[2])
                for k, v in raw_map.items()}

    signal_map: Dict[str, Dict[int, SignalPoint]] = {}
    if engine == "single" or workers <= 1:
        for task in tasks:
            tk, raw = _precompute_signals_fast(task)
            signal_map[tk] = _collect(raw)
        return signal_map

    # Sort largest tasks first for better load balancing
    tasks.sort(key=lambda t: len(t[6]), reverse=True)
    chunksize = max(1, len(tasks) // (workers * 4))

    pool_cls = ThreadPoolExecutor if engine == "thread" else ProcessPoolExecutor
    with pool_cls(max_workers=workers) as ex:
        for tk, raw in ex.map(
            _precompute_signals_fast, tasks, chunksize=chunksize,
        ):
            signal_map[tk] = _collect(raw)
    return signal_map


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _equity_stats(equity: pd.Series) -> Dict[str, float]:
    daily_ret = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if daily_ret.empty:
        return {
            "total_return_pct": 0.0, "cagr_pct": 0.0, "sharpe": 0.0,
            "sortino": 0.0, "mdd_pct": 0.0, "calmar": 0.0,
        }

    years = max(1.0 / 252.0, len(daily_ret) / 252.0)
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0)

    mu = float(daily_ret.mean())
    sigma = float(daily_ret.std(ddof=0))
    sharpe = 0.0 if sigma == 0 else (mu / sigma) * math.sqrt(252)

    downside = daily_ret[daily_ret < 0]
    downside_sigma = float(downside.std(ddof=0)) if len(downside) > 1 else 0.0
    sortino = 0.0 if downside_sigma == 0 else (mu / downside_sigma) * math.sqrt(252)

    rolling_max = equity.cummax()
    dd = equity / rolling_max - 1.0
    mdd = float(dd.min())
    calmar = 0.0 if mdd == 0 else cagr / abs(mdd)

    return {
        "total_return_pct": round(total_return * 100.0, 2),
        "cagr_pct": round(cagr * 100.0, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "mdd_pct": round(abs(mdd) * 100.0, 2),
        "calmar": round(calmar, 3),
    }


def _trade_stats(trades: List[Dict]) -> Dict[str, float]:
    if not trades:
        return {
            "trades": 0, "win_rate_pct": 0.0, "avg_r": 0.0,
            "profit_factor": 0.0, "avg_hold_days": 0.0,
        }

    df = pd.DataFrame(trades)
    wins = df[df["r_multiple"] > 0]
    losses = df[df["r_multiple"] <= 0]
    gross_win = float(wins["pnl"].sum())
    gross_loss = float(abs(losses["pnl"].sum()))
    profit_factor = 0.0 if gross_loss == 0 else gross_win / gross_loss

    return {
        "trades": int(len(df)),
        "win_rate_pct": round(float((df["r_multiple"] > 0).mean() * 100.0), 2),
        "avg_r": round(float(df["r_multiple"].mean()), 3),
        "profit_factor": round(float(profit_factor), 3),
        "avg_hold_days": round(float(df["hold_days"].mean()), 2),
    }


# ---------------------------------------------------------------------------
# Shared backtest preparation
# ---------------------------------------------------------------------------

def _prepare_backtest_context(args: argparse.Namespace) -> Dict[str, object]:
    print("=" * 68)
    print("  MOMENTUM STRATEGY BACKTEST")
    print("=" * 68)

    tickers, sectors, cap_size, market_caps, market = _resolve_universe(args.universe)
    print(f"Universe: {args.universe} ({len(tickers)} symbols)")
    print(f"Years: {args.years} | Position: {args.position_pct*100:.1f}% "
          f"| Stop: dynamic VCP contraction")

    # ---- Step 1: download ------------------------------------------------
    t1 = time.perf_counter()
    print("\n[1/5] Downloading OHLCV...")
    all_data = _download_ohlcv_cached(
        tickers, args.years, no_cache=args.no_cache, market=market,
    )
    print(f"  Loaded symbols: {len(all_data)}  ({time.perf_counter()-t1:.1f}s)")

    # ---- Step 2: benchmark ------------------------------------------------
    t2 = time.perf_counter()
    print("[2/5] Downloading benchmark...")
    benchmark_close = _download_benchmark(args.years, market=market, no_cache=args.no_cache)
    regime_filter_on = args.regime_filter == "on"
    if regime_filter_on:
        regime_series = _download_regime_series(args.years, market=market, no_cache=args.no_cache)
        bench_regime = _build_market_regime_series(regime_series)
        print(f"  Regime filter: ON (choppy max={args.choppy_max_positions})")
    else:
        bench_regime = None
        print("  Regime filter: OFF")
    print(f"  ({time.perf_counter()-t2:.1f}s)")

    common_tickers = sorted(set(all_data.keys()))
    all_data = {t: all_data[t] for t in common_tickers}
    if not all_data:
        raise RuntimeError("No valid symbols downloaded for backtest")

    # ---- Step 3: pre-filter -----------------------------------------------
    t3 = time.perf_counter()
    print("[3/5] Building daily prefilter mask...")
    prefilter, rs_rank, f4_daily = _build_daily_prefilter(
        all_data, benchmark_close, cap_size, args.min_rs,
    )

    leader_daily = None
    if market == "tw":
        leader_daily, _, _ = build_tw_leader_state(
            all_data=all_data,
            sectors=sectors,
            market_caps=market_caps,
            benchmark_close=benchmark_close,
        )

    # Pre-compute consecutive-failure exit masks
    if args.f4_exit_days > 0:
        f4_exit = (
            (~f4_daily).astype(np.int8)
            .rolling(args.f4_exit_days, min_periods=args.f4_exit_days)
            .sum().fillna(0) >= args.f4_exit_days
        )
    else:
        f4_exit = None

    if args.f3_exit_days > 0:
        # F3 exit: RS rank drops below cap-size threshold for N days
        rs_thresholds = pd.Series(
            {t: 70.0 if cap_size.get(t, "unknown") == "large"
             else float(args.min_rs)
             for t in common_tickers},
        )
        rs_below = rs_rank < rs_thresholds
        f3_exit = (
            rs_below.astype(np.int8)
            .rolling(args.f3_exit_days, min_periods=args.f3_exit_days)
            .sum().fillna(0) >= args.f3_exit_days
        )
    else:
        f3_exit = None

    print(f"  ({time.perf_counter()-t3:.1f}s)")

    dates = prefilter.index
    if len(dates) < 400:
        raise RuntimeError("Not enough history to run backtest")

    return {
        "all_data": all_data,
        "benchmark_close": benchmark_close,
        "bench_regime": bench_regime,
        "prefilter": prefilter,
        "rs_rank": rs_rank,
        "f4_exit": f4_exit,
        "f3_exit": f3_exit,
        "leader_daily": leader_daily,
        "dates": dates,
        "market": market,
    }


def _run_backtest_once(
    args: argparse.Namespace,
    prepared: Dict[str, object],
    rr_min: float,
    output_dir: str,
    batch_label: str | None = None,
) -> Dict:
    t0 = time.perf_counter()

    all_data = prepared["all_data"]
    bench_regime = prepared["bench_regime"]
    prefilter = prepared["prefilter"]
    rs_rank = prepared["rs_rank"]
    f4_exit = prepared["f4_exit"]
    f3_exit = prepared["f3_exit"]
    leader_daily = prepared["leader_daily"]
    dates = prepared["dates"]
    market = prepared["market"]
    regime_filter_on = args.regime_filter == "on"

    # ---- Step 4: VCP + R:R signals ----------------------------------------
    t4 = time.perf_counter()
    label = f" [{batch_label}]" if batch_label else ""
    print(f"[4/5]{label} Precomputing VCP + R:R signals...")
    signal_map = _build_signal_map(
        all_data=all_data,
        prefilter=prefilter,
        rr_min=rr_min,
        workers=max(1, int(args.workers)),
        engine=args.engine,
        market=market,
    )
    total_signals = sum(len(v) for v in signal_map.values())
    print(f"  Signals found: {total_signals:,}  ({time.perf_counter()-t4:.1f}s)")

    # Build reverse lookup: day_index -> [(ticker, SignalPoint), ...]
    day_signals: Dict[int, List[Tuple[str, SignalPoint]]] = {}
    for ticker, sigs in signal_map.items():
        for day_idx, sp in sigs.items():
            day_signals.setdefault(day_idx, []).append((ticker, sp))

    # ---- Step 5: simulation -----------------------------------------------
    t5 = time.perf_counter()
    print("[5/5] Running simulation loop...")

    cash = float(args.initial_capital)
    positions: Dict[str, Position] = {}
    trades: List[Dict] = []
    equity_rows: list = []

    start_idx = 300
    end_idx = len(dates) - 2

    for i in range(start_idx, end_idx + 1):
        date = dates[i]

        # ---- Manage exits first ----
        to_close: list = []
        for ticker, pos in positions.items():
            frame = all_data[ticker]
            if date not in frame.index:
                continue
            row = frame.loc[date]
            low_price = float(row["Low"])
            high_price = float(row["High"])
            close_price = float(row["Close"])

            close_series = frame["Close"]
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series.iloc[:, 0]
            ma5 = close_series.rolling(5).mean()
            ma10 = close_series.rolling(10).mean()
            ma5_today = float(ma5.loc[date]) if date in ma5.index and not pd.isna(ma5.loc[date]) else np.nan
            ma10_today = float(ma10.loc[date]) if date in ma10.index and not pd.isna(ma10.loc[date]) else np.nan

            be_trigger = pos.entry_price + pos.risk_per_share      # +1R
            be2_trigger = pos.entry_price + 2 * pos.risk_per_share  # +2R
            if (not pos.moved_to_breakeven) and high_price >= be_trigger:
                # +1R reached: tighten stop to half-risk (-0.5R from entry)
                pos.stop_price = pos.entry_price - 0.5 * pos.risk_per_share
                pos.trend_exit_active = True
            if (not pos.moved_to_breakeven) and high_price >= be2_trigger:
                # +2R reached: move stop to breakeven
                pos.moved_to_breakeven = True
                pos.stop_price = pos.entry_price

            exit_reason = None
            exit_price = None
            exit_shares = pos.shares
            partial_exit = False
            # Stop check is ALWAYS active (initial stop, half-risk, or breakeven)
            if low_price <= pos.stop_price:
                exit_reason = "BE_STOP" if pos.moved_to_breakeven else "STOP"
                exit_price = pos.stop_price
            elif pos.trend_exit_active:
                if not np.isnan(ma10_today) and close_price < ma10_today:
                    exit_reason = "MA10_BREAK"
                    exit_price = close_price
                elif (
                    (not pos.partial_exit_taken)
                    and not np.isnan(ma5_today)
                    and close_price < ma5_today
                ):
                    exit_reason = "MA5_BREAK_HALF"
                    exit_price = close_price
                    exit_shares = pos.shares / 2.0
                    partial_exit = True
            elif f4_exit is not None and f4_exit.at[date, ticker]:
                exit_reason = "F4_FAIL"
                exit_price = close_price
            elif f3_exit is not None and f3_exit.at[date, ticker]:
                exit_reason = "F3_FAIL"
                exit_price = close_price

            if exit_reason and exit_price is not None:
                pnl = (exit_price - pos.entry_price) * exit_shares
                r_mult = (
                    0.0 if pos.risk_per_share == 0
                    else (exit_price - pos.entry_price) / pos.risk_per_share
                )
                to_close.append((ticker, exit_price, exit_reason, pnl, r_mult, exit_shares, partial_exit))

        for ticker, exit_price, exit_reason, pnl, r_multiple, exit_shares, partial_exit in to_close:
            pos = positions[ticker]
            cash += exit_shares * exit_price
            trades.append({
                "ticker": ticker,
                "entry_date": pos.entry_date,
                "exit_date": str(date.date()),
                "entry": round(pos.entry_price, 4),
                "exit": round(float(exit_price), 4),
                "shares": round(float(exit_shares), 4),
                "pnl": round(float(pnl), 2),
                "r_multiple": round(float(r_multiple), 3),
                "hold_days": int((date - pd.Timestamp(pos.entry_date)).days),
                "reason": exit_reason,
                "regime": pos.regime,
            })
            if partial_exit:
                pos.shares -= exit_shares
                pos.partial_exit_taken = True
                if pos.shares <= 1e-9:
                    positions.pop(ticker, None)
            else:
                positions.pop(ticker, None)

        # ---- Mark-to-market ----
        mtm = 0.0
        for ticker, pos in positions.items():
            frame = all_data[ticker]
            if date in frame.index:
                mtm += pos.shares * float(frame.loc[date, "Close"])
            else:
                mtm += pos.shares * pos.entry_price
        equity = cash + mtm
        equity_rows.append({"date": date, "equity": equity})

        # ---- Regime gate ----
        if regime_filter_on and bench_regime is not None:
            day_regime = bench_regime.get(date, "CHOPPY")
        else:
            day_regime = "UPTREND"

        if day_regime == "DOWNTREND":
            continue

        effective_max = (
            args.choppy_max_positions
            if day_regime == "CHOPPY"
            else args.max_positions
        )
        regime_capital_limit = (
            equity * args.choppy_capital_fraction
            if day_regime == "CHOPPY"
            else equity
        )

        if len(positions) >= effective_max:
            continue

        # ---- New entries (O(1) lookup via day_signals) ----
        raw_cands = day_signals.get(i, [])
        if not raw_cands:
            continue

        filtered_cands: list = []
        for t, sp in raw_cands:
            if t in positions:
                continue
            if leader_daily is not None and date in leader_daily.index:
                leader_strong = bool(leader_daily.at[date, t])
            else:
                leader_strong = False
            # CHOPPY: only enter if close breaks above nearest resistance
            if day_regime == "CHOPPY" and regime_filter_on:
                frame_t = all_data[t]
                if date not in frame_t.index:
                    continue
                lookback_3y = max(0, frame_t.index.get_loc(date) - 756)
                hist_slice = frame_t.iloc[lookback_3y:frame_t.index.get_loc(date) + 1]
                levels = detect_resistance_3y(hist_slice)
                current = float(frame_t.loc[date, "Close"])
                status = assess_resistance_status(current, levels)
                if status["resistance_status"] != "BREAK_ABOVE":
                    continue
            filtered_cands.append((t, sp.rr_value, sp.pivot, leader_strong, sp.stop_pct))

        candidates = filtered_cands
        if not candidates:
            continue

        if market == "tw":
            candidates.sort(
                key=lambda x: (x[3], float(rs_rank.loc[date, x[0]])),
                reverse=True,
            )
        else:
            candidates.sort(
                key=lambda x: float(rs_rank.loc[date, x[0]]),
                reverse=True,
            )

        slots = effective_max - len(positions)
        picks = candidates[:slots]

        next_date = dates[i + 1]
        projected_exposure = mtm
        for ticker, rr_value, pivot, leader_strong, signal_stop_pct in picks:
            frame = all_data[ticker]
            if next_date not in frame.index:
                continue
            entry_price = float(frame.loc[next_date, "Open"])
            if entry_price <= 0:
                continue

            breakout_price = (
                pivot * (1.0 + args.breakout_buffer_pct) if pivot > 0 else 0.0
            )
            if breakout_price > 0 and entry_price < breakout_price:
                continue

            position_value = equity * args.position_pct
            if projected_exposure + position_value > regime_capital_limit + 1e-9:
                continue
            if cash < position_value:
                continue

            shares = position_value / entry_price
            stop_pct = float(signal_stop_pct) / 100.0 if signal_stop_pct > 0 else float(args.stop_pct)
            stop_price = entry_price * (1.0 - stop_pct)
            risk_per_share = entry_price - stop_price

            cash -= shares * entry_price
            projected_exposure += position_value
            positions[ticker] = Position(
                ticker=ticker,
                entry_date=str(next_date.date()),
                entry_price=entry_price,
                shares=shares,
                stop_price=stop_price,
                risk_per_share=risk_per_share,
                regime=day_regime,
            )

    # ---- Force-close remaining positions ----
    final_date = dates[end_idx]
    for ticker, pos in list(positions.items()):
        frame = all_data[ticker]
        if final_date not in frame.index:
            continue
        exit_price = float(frame.loc[final_date, "Close"])
        pnl = (exit_price - pos.entry_price) * pos.shares
        r_multiple = (
            0.0 if pos.risk_per_share == 0
            else (exit_price - pos.entry_price) / pos.risk_per_share
        )
        cash += pos.shares * exit_price
        trades.append({
            "ticker": ticker,
            "entry_date": pos.entry_date,
            "exit_date": str(final_date.date()),
            "entry": round(pos.entry_price, 4),
            "exit": round(float(exit_price), 4),
            "shares": round(float(pos.shares), 4),
            "pnl": round(float(pnl), 2),
            "r_multiple": round(float(r_multiple), 3),
            "hold_days": int(
                (final_date - pd.Timestamp(pos.entry_date)).days,
            ),
            "reason": "FORCE_CLOSE",
            "regime": pos.regime,
        })

    print(f"  ({time.perf_counter()-t5:.1f}s)")

    # ---- Results ----------------------------------------------------------
    equity_df = pd.DataFrame(equity_rows)
    if not equity_df.empty:
        equity_df = (
            equity_df.drop_duplicates("date").set_index("date").sort_index()
        )
    else:
        equity_df = pd.DataFrame(
            index=dates[start_idx:end_idx + 1],
            data={"equity": args.initial_capital},
        )

    perf = _equity_stats(equity_df["equity"])
    tstats = _trade_stats(trades)

    # Per-regime trade breakdown
    regime_stats: Dict[str, Dict] = {}
    if trades:
        tdf = pd.DataFrame(trades)
        for reg in ("UPTREND", "CHOPPY", "DOWNTREND"):
            subset = tdf[tdf["regime"] == reg]
            if subset.empty:
                regime_stats[reg] = {"trades": 0, "win_rate_pct": 0.0, "avg_r": 0.0}
            else:
                regime_stats[reg] = {
                    "trades": int(len(subset)),
                    "win_rate_pct": round(
                        float((subset["r_multiple"] > 0).mean() * 100.0), 2,
                    ),
                    "avg_r": round(float(subset["r_multiple"].mean()), 3),
                }

    results = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "universe": args.universe,
            "market": market,
            "years": args.years,
            "initial_capital": args.initial_capital,
            "min_rs": args.min_rs,
            "rr_min": rr_min,
            "stop_pct": args.stop_pct,
            "position_pct": args.position_pct,
            "max_positions": args.max_positions,
            "workers": args.workers,
            "engine": args.engine,
            "breakout_buffer_pct": args.breakout_buffer_pct,
            "f4_exit_days": args.f4_exit_days,
            "f3_exit_days": args.f3_exit_days,
            "regime_filter": args.regime_filter,
            "choppy_max_positions": args.choppy_max_positions,
            "choppy_capital_fraction": args.choppy_capital_fraction,
        },
        "performance": perf,
        "trade_stats": tstats,
        "regime_stats": regime_stats,
    }

    os.makedirs(output_dir, exist_ok=True)
    with open(
        os.path.join(output_dir, "backtest_summary.json"), "w", encoding="utf-8",
    ) as f:
        json.dump(results, f, indent=2)
    pd.DataFrame(trades).to_csv(
        os.path.join(output_dir, "trades.csv"), index=False,
    )
    equity_df.to_csv(os.path.join(output_dir, "equity_curve.csv"))

    elapsed = time.perf_counter() - t0
    print(f"\nBacktest done in {elapsed:.1f}s.")
    print(f"Total return: {perf['total_return_pct']}% | "
          f"CAGR: {perf['cagr_pct']}%")
    print(f"Sharpe: {perf['sharpe']} | MDD: {perf['mdd_pct']}%")
    print(f"Trades: {tstats['trades']} | Win rate: {tstats['win_rate_pct']}% "
          f"| Avg R: {tstats['avg_r']}")
    if regime_stats:
        print("Regime breakdown:")
        for reg in ("UPTREND", "CHOPPY", "DOWNTREND"):
            rs = regime_stats.get(reg, {})
            print(f"  {reg:10s}  trades={rs.get('trades',0):3d}  "
                  f"win={rs.get('win_rate_pct',0):5.1f}%  "
                  f"avg_r={rs.get('avg_r',0):+.3f}")
    print(f"Output: {output_dir}")

    return results


# ---------------------------------------------------------------------------
# Main backtest loop
# ---------------------------------------------------------------------------

def run_backtest(args: argparse.Namespace) -> Dict:
    rr_values = _parse_rr_values(args)
    prepared = _prepare_backtest_context(args)

    if len(rr_values) == 1:
        return _run_backtest_once(
            args=args,
            prepared=prepared,
            rr_min=rr_values[0],
            output_dir=args.output,
        )

    print("\nRunning RR grid with shared downloaded data:")
    print("  " + ", ".join(f"RR>{value:g}" for value in rr_values))
    os.makedirs(args.output, exist_ok=True)

    batch_results: List[Dict] = []
    comparison_rows: List[Dict[str, float | str]] = []
    for rr_value in rr_values:
        subdir = os.path.join(args.output, f"rr_{_rr_slug(rr_value)}")
        result = _run_backtest_once(
            args=args,
            prepared=prepared,
            rr_min=rr_value,
            output_dir=subdir,
            batch_label=f"RR>{rr_value:g}",
        )
        batch_results.append(result)
        comparison_rows.append({
            "rr_min": rr_value,
            "total_return_pct": result["performance"]["total_return_pct"],
            "cagr_pct": result["performance"]["cagr_pct"],
            "sharpe": result["performance"]["sharpe"],
            "sortino": result["performance"]["sortino"],
            "mdd_pct": result["performance"]["mdd_pct"],
            "calmar": result["performance"]["calmar"],
            "trades": result["trade_stats"]["trades"],
            "win_rate_pct": result["trade_stats"]["win_rate_pct"],
            "avg_r": result["trade_stats"]["avg_r"],
            "profit_factor": result["trade_stats"]["profit_factor"],
            "avg_hold_days": result["trade_stats"]["avg_hold_days"],
            "output": subdir,
        })

    comparison_df = pd.DataFrame(comparison_rows).sort_values(
        ["cagr_pct", "sharpe", "calmar"],
        ascending=[False, False, False],
    )
    best_cagr = comparison_df.iloc[0].to_dict()
    best_sharpe = comparison_df.sort_values(
        ["sharpe", "cagr_pct", "calmar"],
        ascending=[False, False, False],
    ).iloc[0].to_dict()

    comparison_payload = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": {
            "universe": args.universe,
            "years": args.years,
            "initial_capital": args.initial_capital,
            "min_rs": args.min_rs,
            "rr_grid": rr_values,
            "regime_filter": args.regime_filter,
        },
        "ranking": comparison_df.to_dict(orient="records"),
        "best_by_cagr": best_cagr,
        "best_by_sharpe": best_sharpe,
    }
    with open(
        os.path.join(args.output, "rr_comparison.json"), "w", encoding="utf-8",
    ) as f:
        json.dump(comparison_payload, f, indent=2)
    comparison_df.to_csv(
        os.path.join(args.output, "rr_comparison.csv"), index=False,
    )

    print("\nRR comparison summary:")
    for row in comparison_df.to_dict(orient="records"):
        print(
            f"  RR>{row['rr_min']:g}  CAGR={row['cagr_pct']}%  "
            f"Sharpe={row['sharpe']}  MDD={row['mdd_pct']}%  "
            f"Trades={row['trades']}  Win={row['win_rate_pct']}%"
        )
    print(f"Best by CAGR: RR>{best_cagr['rr_min']:g}")
    print(f"Best by Sharpe: RR>{best_sharpe['rr_min']:g}")
    print(f"Output root: {args.output}")

    return comparison_payload


if __name__ == "__main__":
    run_backtest(_parse_args())
