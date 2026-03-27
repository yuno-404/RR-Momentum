"""Main scanner orchestration."""

from datetime import datetime
import json
import os
import time

import numpy as np
import pandas as pd
import yfinance as yf

from scanner_charts import generate_chart
from scanner_data import get_sp1500_tickers, get_sp500_tickers
from scanner_filters import (
    check_filter_1_near_highs,
    check_filter_2_momentum,
    check_filter_4_ma_alignment,
    detect_vcp,
)
from scanner_metrics import (
    assess_resistance_status,
    compute_pr_rank,
    compute_rs_line_trend,
    compute_weighted_rs_score,
    detect_resistance_3y,
    estimate_rr_ratios,
)
from scanner_regime import scan_market_regime
from scanner_tw import build_tw_leader_state, is_tw_ticker, load_tw_coverage_metadata


def _clean_for_json(obj):
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_for_json(i) for i in obj]
    return obj


def _detect_market(universe):
    if universe in {"tw", "tw_all", "taiwan", "twse"}:
        return "tw"
    if universe in {"sp500", "sp1500"}:
        return "us"
    tokens = [token.strip().upper() for token in universe.split(",") if token.strip()]
    if tokens and all(is_tw_ticker(token) for token in tokens):
        return "tw"
    return "us"


def _extract_symbol_frame(data, symbol):
    if isinstance(getattr(data, "columns", None), pd.MultiIndex):
        return data.xs(symbol, level=1, axis=1)
    return data


def _download_tw_histories(tickers, period="5y", batch_size=50):
    all_data = {}
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
                    progress=False,
                    auto_adjust=True,
                    threads=True,
                )
            except Exception:
                continue
            if raw is None or len(raw) == 0:
                continue

            resolved = []
            for ticker, symbol in zip(unresolved, symbols):
                try:
                    frame = _extract_symbol_frame(raw, symbol)
                except Exception:
                    continue
                if frame is not None and len(frame) > 100:
                    all_data[ticker] = frame.copy()
                    resolved.append(ticker)
            unresolved = [ticker for ticker in unresolved if ticker not in resolved]
        time.sleep(0.5)
    return all_data


def scan_stocks(universe="sp1500", min_rs=85):
    """Main scanner: apply all 5 filters to a stock universe."""
    market = _detect_market(universe)
    print("=" * 60)
    print("  MOMENTUM STOCK SCANNER")
    print("=" * 60)

    print("\n[1/7] Checking market regime...")
    regime, index_data = scan_market_regime(market=market)
    print(f"\n  Market Regime: {regime}")
    for name, data in index_data.items():
        status = "above" if data["above_ma50"] else "BELOW"
        print(f"  {name}: ${data['price']}  (50MA: ${data['ma50']} [{status}])")

    print(f"\n[2/7] Loading {universe} universe...")
    cap_size = {}
    market_caps = {}
    sectors = {}
    if universe == "sp1500":
        tickers, sectors, cap_size = get_sp1500_tickers()
    elif universe == "sp500":
        tickers, sectors = get_sp500_tickers()
        cap_size = {t: "large" for t in tickers}
    elif market == "tw":
        if universe in {"tw", "tw_all", "taiwan", "twse"}:
            tickers, sectors, cap_size, market_caps = load_tw_coverage_metadata()
        else:
            selected = [token.strip() for token in universe.split(",") if token.strip()]
            tickers, sectors, cap_size, market_caps = load_tw_coverage_metadata(selected)
            for ticker in selected:
                sectors.setdefault(ticker, "N/A")
                cap_size.setdefault(ticker, "unknown")
    else:
        tickers = universe.split(",")
        sectors = {ticker: "N/A" for ticker in tickers}
    print(f"  {len(tickers)} stocks loaded")

    print("\n[3/7] Downloading price data (this may take a minute)...")
    if market == "tw":
        all_data = _download_tw_histories(tickers, period="5y", batch_size=50)
    else:
        all_data = {}
        batch_size = 50
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]
            batch_str = " ".join(batch)
            try:
                data = yf.download(batch_str, period="5y", progress=False, auto_adjust=True, threads=True)
                if data is None or len(data) == 0:
                    continue
                for ticker in batch:
                    try:
                        ticker_data = None
                        if isinstance(getattr(data, "columns", None), pd.MultiIndex):
                            ticker_data = data.xs(ticker, level=1, axis=1)
                        else:
                            ticker_data = data
                        if ticker_data is not None and len(ticker_data) > 100:
                            all_data[ticker] = ticker_data.copy()
                    except (KeyError, Exception):
                        pass
            except Exception as e:
                print(f"  Batch error: {e}")
            time.sleep(0.5)
    print(f"  {len(all_data)} stocks with sufficient data")

    print("\n[4/7] Computing weighted RS + PR rankings...")
    benchmark_symbol = "^TWII" if market == "tw" else "^GSPC"
    benchmark_name = "TAIEX" if market == "tw" else "S&P 500"
    sp500_hist = yf.download(benchmark_symbol, period="2y", progress=False, auto_adjust=True)
    if sp500_hist is None or len(sp500_hist) == 0:
        print(f"  Failed to download {benchmark_name} benchmark for RS Line. Exiting.")
        return {
            "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "regime": regime,
            "index_data": index_data,
            "candidates": [],
        }

    if "Close" not in sp500_hist:
        print(f"  {benchmark_name} benchmark missing Close column. Exiting.")
        return {
            "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "regime": regime,
            "index_data": index_data,
            "candidates": [],
        }
    sp500_close = sp500_hist["Close"]
    if isinstance(sp500_close, pd.DataFrame):
        sp500_close = sp500_close.iloc[:, 0]
    sp500_close = pd.Series(sp500_close).dropna()
    if len(sp500_close) < 252:
        print(f"  Not enough {benchmark_name} data for RS Line. Exiting.")
        return {
            "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "regime": regime,
            "index_data": index_data,
            "candidates": [],
        }

    rs_scores = {}
    for ticker, hist in all_data.items():
        try:
            score = compute_weighted_rs_score(hist)
            if np.isfinite(score):
                rs_scores[ticker] = score
        except Exception:
            pass
    rs_ranks = compute_pr_rank(rs_scores)
    above_threshold = sum(1 for v in rs_ranks.values() if v >= min_rs)
    print(f"  {above_threshold} stocks with PR >= {min_rs}")

    tw_leader_flags = {}
    tw_leader_scores = {}
    tw_leaders = {}
    if market == "tw":
        leader_flag_df, leader_score_df, tw_leaders = build_tw_leader_state(
            all_data=all_data,
            sectors=sectors,
            market_caps=market_caps,
            benchmark_close=sp500_close,
        )
        if not leader_flag_df.empty:
            last_date = leader_flag_df.index[-1]
            tw_leader_flags = leader_flag_df.loc[last_date].to_dict()
            tw_leader_scores = leader_score_df.loc[last_date].to_dict()

    print("\n[5/7] Applying strategy filters...")
    candidates = []
    near_miss = []
    filter_total = 6

    for ticker, hist in all_data.items():
        rs = rs_ranks.get(ticker, 0)

        is_large_cap = cap_size.get(ticker, "unknown") == "large"
        rs_threshold = 70 if is_large_cap else min_rs

        if rs < rs_threshold:
            continue

        close = hist["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        current_close = float(close.iloc[-1])

        filters = {}

        rs_rising = compute_rs_line_trend(hist, sp500_close)
        filters["F3_RS_rising"] = rs_rising

        f1_pass, distance = check_filter_1_near_highs(hist)
        filters["F1_near_highs"] = f1_pass

        f2_pass, rally = check_filter_2_momentum(hist)
        filters["F2_momentum"] = f2_pass

        f4_pass, ma_info = check_filter_4_ma_alignment(hist)
        filters["F4_ma_align"] = f4_pass

        f5_pass, vcp_info = detect_vcp(hist, market=market)
        filters["F5_vcp"] = f5_pass

        rr_info = estimate_rr_ratios(hist, vcp_info)
        pivot = float(rr_info.get("pivot", 0.0))
        pivot_breakout = bool(pivot > 0 and current_close >= pivot)
        filters["F6_pivot_breakout"] = pivot_breakout

        passed_count = sum(filters.values())
        all_pass = all(filters.values())

        if not all_pass:
            if passed_count >= max(3, filter_total - 2):
                failed = [k for k, v in filters.items() if not v]
                near_miss.append({
                    "ticker": ticker,
                    "rs": rs,
                    "passed": passed_count,
                    "failed": failed,
                    "distance": distance,
                    "rally": rally,
                    "rr_to_52w": rr_info["rr_to_52w"],
                    "rr_breakout": rr_info["rr_breakout"],
                    "rr_info": rr_info,
                    "vcp": vcp_info,
                    "ma": ma_info,
                    "industry_leader": tw_leaders.get(sectors.get(ticker, ""))
                    if market == "tw" else None,
                    "industry_leader_strong": bool(tw_leader_flags.get(ticker, False))
                    if market == "tw" else None,
                    "industry_leader_score": float(tw_leader_scores.get(ticker, 0.0))
                    if market == "tw" else None,
                })
            continue

        current_price = round(float(close.iloc[-1]), 2)
        high_52w = round(float(hist["High"].iloc[-252:].max()), 2)
        resistance_levels = detect_resistance_3y(hist)
        res_info = assess_resistance_status(current_price, resistance_levels)

        cap_label = cap_size.get(ticker, "unknown")
        candidates.append({
            "ticker": ticker,
            "sector": sectors.get(ticker, "N/A"),
            "cap": cap_label,
            "price": current_price,
            "high_52w": high_52w,
            "distance_from_high": distance,
            "rally_from_base": rally,
            "rs_rank": rs,
            "rs_rising": rs_rising,
            "ma": ma_info,
            "vcp": vcp_info,
            "rr_to_52w": rr_info["rr_to_52w"],
            "rr_breakout": rr_info["rr_breakout"],
            "rr_info": rr_info,
            "resistance_levels": resistance_levels,
            "nearest_resistance": res_info["nearest_resistance"],
            "distance_to_resistance_pct": res_info["distance_to_resistance_pct"],
            "resistance_status": res_info["resistance_status"],
            "industry_leader": tw_leaders.get(sectors.get(ticker, "")) if market == "tw" else None,
            "industry_leader_strong": bool(tw_leader_flags.get(ticker, False)) if market == "tw" else None,
            "industry_leader_score": round(float(tw_leader_scores.get(ticker, 0.0)), 2)
            if market == "tw" else None,
        })

    if market == "tw":
        candidates.sort(
            key=lambda x: (bool(x.get("industry_leader_strong")), x["rs_rank"]),
            reverse=True,
        )
    else:
        candidates.sort(key=lambda x: x["rs_rank"], reverse=True)

    print(f"\n[6/7] Results:")
    print(f"  Passed ALL {filter_total} filters:  {len(candidates)}")
    print(f"  Near miss:                 {len(near_miss)}")
    print("=" * 60)

    if not candidates:
        print("\n  No stocks passed all 5 filters.")
        print(f"  Market regime: {regime}")

    for c in candidates:
        depths_str = ' -> '.join(f'{d}%' for d in c['vcp'].get('depths', []))
        print(f"""
{'-' * 60}
Ticker: {c['ticker']}  [{c['cap'].upper()}]
Sector: {c['sector']}
Price:  ${c['price']}   52W High: ${c['high_52w']}
Distance from 52W high: {c['distance_from_high']}%

-- RS & Momentum ----------------------
RS Rank:          {c['rs_rank']} / 99
RS Trend:         {'RISING' if c['rs_rising'] else 'FALLING'}
Rally from Base:  {c['rally_from_base']}%

-- Trend ------------------------------
50MA:  ${c['ma']['ma50']}  (slope: {c['ma']['ma50_slope']})
200MA: ${c['ma']['ma200']}  (slope: {c['ma']['ma200_slope']})
MA Alignment: {c['ma']['alignment']}

-- VCP Analysis -----------------------
Contraction Depths: {depths_str}
  Decreasing:       {c['vcp'].get('decreasing', False)}
  Volume Declining:  {c['vcp'].get('vol_declining', False)}
  Dry-up Day (<50V): {c['vcp'].get('vol_dryup_day', False)}
Last Contraction:   {c['vcp'].get('last_contraction', 0)}%

-- Observation ------------------------
R:R to 52W:         {f"{c['rr_to_52w']}:1" if c['rr_to_52w'] > 0 else 'N/A'}
R:R breakout:       {f"{c['rr_breakout']}:1" if c['rr_breakout'] > 0 else 'N/A'}
Breakout Target:    ${c['rr_info'].get('target_breakout', 0)}
Pivot:              ${c['rr_info'].get('pivot', 0)}
Resistance Status:  {c['resistance_status']}  ({c['distance_to_resistance_pct']}%)
Nearest Resistance: ${c['nearest_resistance']}
Industry Leader:    {c.get('industry_leader') or 'N/A'}
Leader Strong:      {c.get('industry_leader_strong')}
Leader Score:       {c.get('industry_leader_score')}

-- Filters (ALL PASS) -----------------
[v] F1: Within 25% of high ({c['distance_from_high']}%)
[v] F2: Rallied {c['rally_from_base']}% off base
[v] F3: RS {c['rs_rank']} + trend rising
[v] F4: 50MA > 200MA, both rising
[v] F5: VCP contraction present
[v] F6: Price above pivot

-- Verdict ----------------------------
>>> TRADE <<<
""")

    if near_miss:
        near_miss.sort(key=lambda x: (x["passed"], x["rs"]), reverse=True)
        print(f"\n  -- Near Misses (passed 3+ filters) -- [{len(near_miss)} stocks]")
        for nm in near_miss[:15]:
            failed_str = ", ".join(nm["failed"])
            print(f"  {nm['ticker']:6s}  RS:{nm['rs']:2d}  "
                  f"passed:{nm['passed']}/5  "
                  f"failed: {failed_str}")

    print("\n[7/7] Generating charts...")
    chart_dir = "charts"
    if os.path.exists(chart_dir):
        for f in os.listdir(chart_dir):
            if f.endswith(".png"):
                os.remove(os.path.join(chart_dir, f))

    chart_files = []

    for c in candidates:
        ticker = c["ticker"]
        if ticker in all_data:
            fp = generate_chart(
                ticker, all_data[ticker], c["vcp"],
                c["rs_rank"], c.get("rr_info", {}), "PASS", chart_dir,
                resistance_levels=c.get("resistance_levels", []),
                nearest_resistance=c.get("nearest_resistance"),
                resistance_status=c.get("resistance_status"),
                distance_to_resistance_pct=c.get("distance_to_resistance_pct"))
            chart_files.append(fp)
            print(f"    [PASS]     {fp}")

    vcp_fails = sorted(
        [nm for nm in near_miss if "F5_vcp" in nm["failed"]],
        key=lambda x: x["rs"], reverse=True
    )[:8]
    for nm in vcp_fails:
        ticker = nm["ticker"]
        if ticker in all_data:
            fp = generate_chart(
                ticker, all_data[ticker], nm["vcp"],
                nm["rs"], nm.get("rr_info", {}), "VCP_FAIL", chart_dir)
            chart_files.append(fp)
            print(f"    [VCP_FAIL] {fp}")

    other_near = sorted(
        [nm for nm in near_miss if nm["passed"] >= 4 and "F5_vcp" not in nm["failed"]],
        key=lambda x: x["rs"], reverse=True
    )[:5]
    for nm in other_near:
        ticker = nm["ticker"]
        if ticker in all_data:
            failed_str = "+".join(nm["failed"])
            fp = generate_chart(
                ticker, all_data[ticker], nm["vcp"],
                nm["rs"], nm.get("rr_info", {}),
                f"NEAR_MISS({failed_str})", chart_dir)
            chart_files.append(fp)
            print(f"    [NEAR]     {fp}")

    print(f"\n  {len(chart_files)} charts saved to {chart_dir}/")

    print("\n" + "=" * 60)
    print(f"  Total candidates:     {len(candidates)}")
    print(f"  Near misses (3+/5):   {len(near_miss)}")
    print(f"  Charts generated:     {len(chart_files)}")
    print(f"  Market regime:        {regime}")
    print("=" * 60)

    output = {
        "scan_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market": market,
        "regime": regime,
        "index_data": index_data,
        "candidates": _clean_for_json(candidates),
    }
    with open("scan_results.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("\n  Results saved to scan_results.json")

    return output
