"""Grid search: RR thresholds × stop strategies × MA exit combinations.

Usage:
    python backtest_grid.py --universe tw --years 15
"""

from __future__ import annotations

import math
import os
import sys
import time
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from backtest import (
    Position,
    SignalPoint,
    _build_signal_map,
    _prepare_backtest_context,
    _parse_args,
)
from scanner_metrics import detect_resistance_3y, assess_resistance_status

# ---- Grid parameters ----
RR_VALUES = [2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
STOP_STRATEGIES = ["none", "direct", "gradual"]
MA_PERIODS = [5, 10, 20, 30, 50, 60, 200]


def _valid_ma_pairs() -> List[Tuple[int, int]]:
    pairs = []
    for p in MA_PERIODS:
        for f in MA_PERIODS:
            if f > p:
                pairs.append((p, f))
    return pairs


def _equity_stats(equity: pd.Series) -> Dict[str, float]:
    daily_ret = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if daily_ret.empty:
        return dict(cagr=0, sharpe=0, sortino=0, mdd=0, calmar=0, total_ret=0)
    years = max(1 / 252, len(daily_ret) / 252)
    total_ret = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)
    mu = float(daily_ret.mean())
    sigma = float(daily_ret.std(ddof=0))
    sharpe = (mu / sigma) * math.sqrt(252) if sigma > 0 else 0
    downside = daily_ret[daily_ret < 0]
    ds = float(downside.std(ddof=0)) if len(downside) > 1 else 0
    sortino = (mu / ds) * math.sqrt(252) if ds > 0 else 0
    dd = equity / equity.cummax() - 1
    mdd = float(dd.min())
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    return dict(
        cagr=round(cagr * 100, 2),
        sharpe=round(sharpe, 3),
        sortino=round(sortino, 3),
        mdd=round(abs(mdd) * 100, 2),
        calmar=round(calmar, 3),
        total_ret=round(total_ret * 100, 2),
    )


def _run_sim(
    all_data: Dict,
    dates: pd.DatetimeIndex,
    day_signals: Dict[int, List[Tuple[str, SignalPoint]]],
    bench_regime,
    rs_rank: pd.DataFrame,
    f4_exit,
    f3_exit,
    leader_daily,
    market: str,
    ma_dfs: Dict[int, pd.DataFrame],
    close_df: pd.DataFrame,
    high_df: pd.DataFrame,
    low_df: pd.DataFrame,
    open_df: pd.DataFrame,
    initial_capital: float,
    position_pct: float,
    max_positions: int,
    regime_filter_on: bool,
    choppy_max_positions: int,
    choppy_capital_fraction: float,
    breakout_buffer_pct: float,
    stop_pct_fallback: float,
    stop_strategy: str,
    ma_partial: int,
    ma_full: int,
) -> Dict[str, float]:
    cash = float(initial_capital)
    positions: Dict[str, Position] = {}
    trades: list = []
    equity_rows: list = []

    ma_p_df = ma_dfs[ma_partial]
    ma_f_df = ma_dfs[ma_full]

    start_idx = 300
    end_idx = len(dates) - 2

    for i in range(start_idx, end_idx + 1):
        date = dates[i]

        # ---- Exits ----
        to_close: list = []
        for ticker, pos in positions.items():
            cv = close_df.at[date, ticker]
            if pd.isna(cv):
                continue
            close_price = float(cv)
            low_price = float(low_df.at[date, ticker])
            high_price = float(high_df.at[date, ticker])

            mp = ma_p_df.at[date, ticker]
            mf = ma_f_df.at[date, ticker]
            ma_p_today = float(mp) if not pd.isna(mp) else np.nan
            ma_f_today = float(mf) if not pd.isna(mf) else np.nan

            # ---- Trailing stop updates ----
            be_trigger = pos.entry_price + pos.risk_per_share
            be2_trigger = pos.entry_price + 2 * pos.risk_per_share

            if stop_strategy == "direct":
                if (not pos.moved_to_breakeven) and high_price >= be_trigger:
                    pos.moved_to_breakeven = True
                    pos.stop_price = pos.entry_price
                    pos.trend_exit_active = True
            elif stop_strategy == "gradual":
                if not pos.trend_exit_active and high_price >= be_trigger:
                    pos.stop_price = pos.entry_price - 0.5 * pos.risk_per_share
                    pos.trend_exit_active = True
                if (not pos.moved_to_breakeven) and high_price >= be2_trigger:
                    pos.moved_to_breakeven = True
                    pos.stop_price = pos.entry_price
            else:  # none
                if (not pos.moved_to_breakeven) and high_price >= be_trigger:
                    pos.moved_to_breakeven = True
                    pos.trend_exit_active = True

            exit_reason = None
            exit_price = None
            exit_shares = pos.shares
            partial_exit = False

            if stop_strategy == "none":
                if (not pos.trend_exit_active) and low_price <= pos.stop_price:
                    exit_reason = "STOP"
                    exit_price = pos.stop_price
                elif pos.trend_exit_active:
                    if not np.isnan(ma_f_today) and close_price < ma_f_today:
                        exit_reason = "MA_FULL"
                        exit_price = close_price
                    elif (
                        (not pos.partial_exit_taken)
                        and not np.isnan(ma_p_today)
                        and close_price < ma_p_today
                    ):
                        exit_reason = "MA_PARTIAL"
                        exit_price = close_price
                        exit_shares = pos.shares / 2.0
                        partial_exit = True
            else:
                if low_price <= pos.stop_price:
                    exit_reason = "BE_STOP" if pos.moved_to_breakeven else "STOP"
                    exit_price = pos.stop_price
                elif pos.trend_exit_active:
                    if not np.isnan(ma_f_today) and close_price < ma_f_today:
                        exit_reason = "MA_FULL"
                        exit_price = close_price
                    elif (
                        (not pos.partial_exit_taken)
                        and not np.isnan(ma_p_today)
                        and close_price < ma_p_today
                    ):
                        exit_reason = "MA_PARTIAL"
                        exit_price = close_price
                        exit_shares = pos.shares / 2.0
                        partial_exit = True

            if exit_reason is None and not pos.trend_exit_active:
                if (
                    f4_exit is not None
                    and ticker in f4_exit.columns
                    and date in f4_exit.index
                    and f4_exit.at[date, ticker]
                ):
                    exit_reason = "F4_FAIL"
                    exit_price = close_price
                elif (
                    f3_exit is not None
                    and ticker in f3_exit.columns
                    and date in f3_exit.index
                    and f3_exit.at[date, ticker]
                ):
                    exit_reason = "F3_FAIL"
                    exit_price = close_price

            if exit_reason and exit_price is not None:
                r_mult = (
                    0.0
                    if pos.risk_per_share == 0
                    else (exit_price - pos.entry_price) / pos.risk_per_share
                )
                pnl = (exit_price - pos.entry_price) * exit_shares
                to_close.append(
                    (ticker, exit_price, pnl, r_mult, exit_shares, partial_exit)
                )

        for ticker, exit_price, pnl, r_multiple, exit_shares, partial_exit in to_close:
            pos = positions[ticker]
            cash += exit_shares * exit_price
            trades.append({"r_multiple": float(r_multiple), "pnl": float(pnl)})
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
            cv = close_df.at[date, ticker]
            mtm += pos.shares * (float(cv) if not pd.isna(cv) else pos.entry_price)
        equity = cash + mtm
        equity_rows.append(equity)

        # ---- Regime gate ----
        if regime_filter_on and bench_regime is not None:
            day_regime = bench_regime.get(date, "CHOPPY")
        else:
            day_regime = "UPTREND"

        if day_regime == "DOWNTREND":
            continue

        effective_max = (
            choppy_max_positions if day_regime == "CHOPPY" else max_positions
        )
        regime_capital_limit = (
            equity * choppy_capital_fraction if day_regime == "CHOPPY" else equity
        )

        if len(positions) >= effective_max:
            continue

        # ---- New entries ----
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
            if day_regime == "CHOPPY" and regime_filter_on:
                frame_t = all_data[t]
                if date not in frame_t.index:
                    continue
                loc = frame_t.index.get_loc(date)
                lookback_3y = max(0, loc - 756)
                hist_slice = frame_t.iloc[lookback_3y : loc + 1]
                levels = detect_resistance_3y(hist_slice)
                current = float(frame_t.iloc[loc]["Close"])
                status = assess_resistance_status(current, levels)
                if status["resistance_status"] != "BREAK_ABOVE":
                    continue
            filtered_cands.append(
                (t, sp.rr_value, sp.pivot, leader_strong, sp.stop_pct)
            )

        if not filtered_cands:
            continue

        if market == "tw":
            filtered_cands.sort(
                key=lambda x: (x[3], float(rs_rank.at[date, x[0]])),
                reverse=True,
            )
        else:
            filtered_cands.sort(
                key=lambda x: float(rs_rank.at[date, x[0]]),
                reverse=True,
            )

        slots = effective_max - len(positions)
        picks = filtered_cands[:slots]
        next_date = dates[i + 1]
        projected_exposure = mtm

        for ticker, rr_value, pivot, leader_strong, signal_stop_pct in picks:
            ov = open_df.at[next_date, ticker]
            if pd.isna(ov):
                continue
            entry_price = float(ov)
            if entry_price <= 0:
                continue

            breakout_price = (
                pivot * (1.0 + breakout_buffer_pct) if pivot > 0 else 0.0
            )
            if breakout_price > 0 and entry_price < breakout_price:
                continue

            position_value = equity * position_pct
            if projected_exposure + position_value > regime_capital_limit + 1e-9:
                continue
            if cash < position_value:
                continue

            shares = position_value / entry_price
            s_pct = (
                float(signal_stop_pct) / 100.0
                if signal_stop_pct > 0
                else stop_pct_fallback
            )
            stop_price = entry_price * (1.0 - s_pct)
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

    # ---- Force-close ----
    final_date = dates[end_idx]
    for ticker, pos in list(positions.items()):
        cv = close_df.at[final_date, ticker]
        exit_price = float(cv) if not pd.isna(cv) else pos.entry_price
        pnl = (exit_price - pos.entry_price) * pos.shares
        r_mult = (
            0.0
            if pos.risk_per_share == 0
            else (exit_price - pos.entry_price) / pos.risk_per_share
        )
        cash += pos.shares * exit_price
        trades.append({"r_multiple": float(r_mult), "pnl": float(pnl)})

    # ---- Stats ----
    if not equity_rows:
        return dict(
            total_ret=0, cagr=0, sharpe=0, sortino=0, mdd=0, calmar=0,
            trades=0, win_rate=0, avg_r=0, profit_factor=0,
        )

    eq = pd.Series(equity_rows, index=dates[start_idx : end_idx + 1])
    stats = _equity_stats(eq)

    if trades:
        df_t = pd.DataFrame(trades)
        wins = df_t[df_t["r_multiple"] > 0]
        losses = df_t[df_t["r_multiple"] <= 0]
        gw = float(wins["pnl"].sum())
        gl = float(abs(losses["pnl"].sum()))
        pf = gw / gl if gl > 0 else 0
        stats.update(
            {
                "trades": len(df_t),
                "win_rate": round(float((df_t["r_multiple"] > 0).mean() * 100), 2),
                "avg_r": round(float(df_t["r_multiple"].mean()), 3),
                "profit_factor": round(pf, 3),
            }
        )
    else:
        stats.update({"trades": 0, "win_rate": 0, "avg_r": 0, "profit_factor": 0})

    return stats


def main():
    args = _parse_args()
    t_start = time.perf_counter()

    print("=" * 70)
    print("  GRID SEARCH BACKTEST")
    print("=" * 70)

    # ---- Shared data ----
    prepared = _prepare_backtest_context(args)
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

    # ---- Precompute price DataFrames ----
    tickers = sorted(all_data.keys())
    close_df = pd.DataFrame(
        {t: all_data[t]["Close"] for t in tickers}
    ).sort_index()
    high_df = pd.DataFrame(
        {t: all_data[t]["High"] for t in tickers}
    ).reindex(close_df.index)
    low_df = pd.DataFrame(
        {t: all_data[t]["Low"] for t in tickers}
    ).reindex(close_df.index)
    open_df = pd.DataFrame(
        {t: all_data[t]["Open"] for t in tickers}
    ).reindex(close_df.index)

    # ---- Precompute all MAs ----
    print("\nPrecomputing MA DataFrames...")
    ma_dfs: Dict[int, pd.DataFrame] = {}
    for period in MA_PERIODS:
        ma_dfs[period] = close_df.rolling(period).mean()
    print(f"  MA periods: {MA_PERIODS}")

    ma_pairs = _valid_ma_pairs()
    total_combos = len(RR_VALUES) * len(STOP_STRATEGIES) * len(ma_pairs)
    print(f"\nGrid: {len(RR_VALUES)} RR × {len(STOP_STRATEGIES)} stops "
          f"× {len(ma_pairs)} MA pairs = {total_combos} combinations\n")

    results: list = []
    combo_count = 0

    for rr_idx, rr_min in enumerate(RR_VALUES):
        # Precompute signals once per RR
        t_rr = time.perf_counter()
        print(f"[{rr_idx+1}/{len(RR_VALUES)}] RR>{rr_min} — computing signals...",
              end="", flush=True)
        signal_map = _build_signal_map(
            all_data=all_data,
            prefilter=prefilter,
            rr_min=rr_min,
            workers=max(1, int(args.workers)),
            engine=args.engine,
            market=market,
        )
        day_signals: Dict[int, List[Tuple[str, SignalPoint]]] = {}
        for tk, sigs in signal_map.items():
            for day_idx, sp in sigs.items():
                day_signals.setdefault(day_idx, []).append((tk, sp))
        total_sigs = sum(len(v) for v in signal_map.values())
        print(f" {total_sigs:,} signals ({time.perf_counter()-t_rr:.0f}s)")

        n_variants = len(STOP_STRATEGIES) * len(ma_pairs)
        print(f"    Running {n_variants} simulation variants...", end="", flush=True)
        t_sim = time.perf_counter()

        for stop_strat in STOP_STRATEGIES:
            for ma_p, ma_f in ma_pairs:
                combo_count += 1
                stats = _run_sim(
                    all_data=all_data,
                    dates=dates,
                    day_signals=day_signals,
                    bench_regime=bench_regime,
                    rs_rank=rs_rank,
                    f4_exit=f4_exit,
                    f3_exit=f3_exit,
                    leader_daily=leader_daily,
                    market=market,
                    ma_dfs=ma_dfs,
                    close_df=close_df,
                    high_df=high_df,
                    low_df=low_df,
                    open_df=open_df,
                    initial_capital=args.initial_capital,
                    position_pct=args.position_pct,
                    max_positions=args.max_positions,
                    regime_filter_on=regime_filter_on,
                    choppy_max_positions=args.choppy_max_positions,
                    choppy_capital_fraction=args.choppy_capital_fraction,
                    breakout_buffer_pct=args.breakout_buffer_pct,
                    stop_pct_fallback=args.stop_pct,
                    stop_strategy=stop_strat,
                    ma_partial=ma_p,
                    ma_full=ma_f,
                )
                results.append(
                    {
                        "rr_min": rr_min,
                        "stop_strategy": stop_strat,
                        "ma_partial": ma_p,
                        "ma_full": ma_f,
                        **stats,
                    }
                )

        elapsed = time.perf_counter() - t_sim
        print(f" done ({elapsed:.0f}s, {elapsed/n_variants:.2f}s/combo)")

    # ---- Output ----
    df = pd.DataFrame(results)
    col_order = [
        "rr_min", "stop_strategy", "ma_partial", "ma_full",
        "cagr", "sharpe", "sortino", "mdd", "calmar",
        "trades", "win_rate", "avg_r", "profit_factor", "total_ret",
    ]
    df = df[col_order].sort_values(
        ["sharpe", "calmar", "cagr"], ascending=False
    )

    out_dir = args.output
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "grid_results.csv")
    df.to_csv(out_path, index=False)

    elapsed_total = time.perf_counter() - t_start
    print(f"\n{'='*70}")
    print(f"  Grid search complete: {total_combos} combinations in {elapsed_total:.0f}s")
    print(f"  Results saved to: {out_path}")
    print(f"{'='*70}")

    # ---- Top 20 summary ----
    print(f"\n  Top 20 by Sharpe:")
    print(f"  {'RR':>4} {'Stop':<8} {'MA_P':>4}/{'':<4}{'MA_F':<4} "
          f"{'CAGR':>6} {'Sharpe':>7} {'MDD':>6} {'Calmar':>7} "
          f"{'Trades':>6} {'Win%':>6} {'AvgR':>6} {'PF':>5}")
    print(f"  {'-'*85}")
    for _, row in df.head(20).iterrows():
        print(
            f"  {row.rr_min:>4g} {row.stop_strategy:<8} "
            f"{row.ma_partial:>4d} / {row.ma_full:<4d} "
            f"{row.cagr:>5.1f}% {row.sharpe:>7.3f} "
            f"{row.mdd:>5.1f}% {row.calmar:>7.3f} "
            f"{row.trades:>6.0f} {row.win_rate:>5.1f}% "
            f"{row.avg_r:>6.2f} {row.profit_factor:>5.2f}"
        )
    print()


if __name__ == "__main__":
    main()
