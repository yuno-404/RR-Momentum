"""Scoring and relative-strength metrics."""

import numpy as np
import pandas as pd


def compute_pr_rank(rs_scores):
    """Compute PR percentile rank (1-99) from weighted RS scores."""
    if not rs_scores:
        return {}
    series = pd.Series(rs_scores, dtype="float64")
    series = series.replace([np.inf, -np.inf], np.nan).dropna()
    if series.empty:
        return {}
    ranks = series.rank(pct=True) * 99
    return ranks.clip(1, 99).round(0).astype(int).to_dict()


def compute_weighted_rs_score(hist):
    """
    Compute weighted RS score using quarterly returns.

    Weights:
      - Most recent quarter (63d): 40%
      - Previous 3 quarters (each 63d): 20% each
    """
    if len(hist) < 252:
        return np.nan

    close = hist["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    q1 = float(close.iloc[-1] / close.iloc[-63] - 1)
    q2 = float(close.iloc[-63] / close.iloc[-126] - 1)
    q3 = float(close.iloc[-126] / close.iloc[-189] - 1)
    q4 = float(close.iloc[-189] / close.iloc[-252] - 1)

    return (0.4 * q1) + (0.2 * q2) + (0.2 * q3) + (0.2 * q4)


def compute_rs_line_trend(hist, benchmark_close, lookback_short=63, lookback_long=126):
    """Check RS Line trend (stock/benchmark) is rising and not converging."""
    close = hist["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    aligned = pd.concat([close.rename("stock"), benchmark_close.rename("benchmark")], axis=1).dropna()
    if len(aligned) < lookback_long:
        return False

    rs_line = aligned["stock"] / aligned["benchmark"]
    recent_rs = float(rs_line.iloc[-1] / rs_line.iloc[-lookback_short] - 1)
    older_rs = float(rs_line.iloc[-lookback_short] / rs_line.iloc[-lookback_long] - 1)

    ma50 = rs_line.rolling(50).mean()
    if ma50.dropna().shape[0] < 20:
        return False

    ma50_rising = float(ma50.iloc[-1]) > float(ma50.iloc[-20])
    above_ma50 = float(rs_line.iloc[-1]) > float(ma50.iloc[-1])
    return recent_rs > 0 and recent_rs >= older_rs and ma50_rising and above_ma50


def estimate_rr_ratios(hist, vcp_info):
    """Estimate dual R:R (to 52W high and breakout projection)."""
    out = {
        "rr_to_52w": 0.0,
        "rr_breakout": 0.0,
        "stop_pct": 0.0,
        "target_52w": 0.0,
        "target_breakout": 0.0,
        "pivot": 0.0,
    }

    if not vcp_info or "last_contraction" not in vcp_info:
        return out

    stop_pct = float(vcp_info["last_contraction"]) / 100.0
    if stop_pct <= 0:
        return out

    close = hist["Close"]
    high = hist["High"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    if isinstance(high, pd.DataFrame):
        high = high.iloc[:, 0]

    current = float(close.iloc[-1])
    high_52w = float(high.iloc[-252:].max()) if len(high) >= 252 else float(high.max())

    upside_52w = max(0.0, (high_52w - current) / current)
    rr_to_52w = upside_52w / stop_pct

    pivot = 0.0
    target_breakout = 0.0
    rr_breakout = 0.0

    contractions = vcp_info.get("contractions", [])
    c1_depth = float(vcp_info.get("c1_depth", 0.0))
    if contractions:
        pivot = float(contractions[-1].get("high_price", 0.0))
        if pivot > 0 and c1_depth > 0:
            target_breakout = pivot * (1.0 + c1_depth / 100.0)
            upside_breakout = max(0.0, (target_breakout - current) / current)
            rr_breakout = upside_breakout / stop_pct

    out.update({
        "rr_to_52w": round(float(rr_to_52w), 1),
        "rr_breakout": round(float(rr_breakout), 1),
        "stop_pct": round(float(stop_pct * 100.0), 1),
        "target_52w": round(float(high_52w), 2),
        "target_breakout": round(float(target_breakout), 2) if target_breakout > 0 else 0.0,
        "pivot": round(float(pivot), 2) if pivot > 0 else 0.0,
    })
    return out


def detect_resistance_3y(hist_3y, order=10, band_pct=0.02):
    """Detect 1-3 major resistance levels from 3Y daily local highs."""
    high = hist_3y["High"]
    if isinstance(high, pd.DataFrame):
        high = high.iloc[:, 0]
    high = pd.Series(high).dropna()

    min_points = (order * 2) + 1
    if len(high) < min_points:
        return []

    values = high.values.astype(float)
    local_highs = []
    for i in range(order, len(values) - order):
        center = values[i]
        window = values[i - order:i + order + 1]
        if center < np.max(window):
            continue
        left = values[i - order:i]
        right = values[i + 1:i + order + 1]
        if np.any(center <= left) or np.any(center < right):
            continue
        local_highs.append({
            "price": float(center),
            "date": high.index[i],
        })

    if not local_highs:
        return []

    local_highs.sort(key=lambda x: x["price"], reverse=True)
    clusters = []
    for p in local_highs:
        assigned = False
        for cluster in clusters:
            center = cluster["center"]
            if abs(p["price"] - center) / center <= band_pct:
                cluster["points"].append(p)
                prices = [pt["price"] for pt in cluster["points"]]
                cluster["center"] = float(np.mean(prices))
                assigned = True
                break
        if not assigned:
            clusters.append({"center": p["price"], "points": [p]})

    ranked = sorted(
        clusters,
        key=lambda c: (
            len(c["points"]),
            c["center"],
        ),
        reverse=True,
    )

    levels = []
    for c in ranked[:3]:
        prices = [pt["price"] for pt in c["points"]]
        dates = [pt["date"] for pt in c["points"]]
        levels.append({
            "price": round(float(np.mean(prices)), 2),
            "touches": int(len(prices)),
            "band_low": round(float(min(prices)), 2),
            "band_high": round(float(max(prices)), 2),
            "last_touch": max(dates),
        })

    return sorted(levels, key=lambda x: x["price"])


def assess_resistance_status(current, levels, near_pct=0.02):
    """Assess resistance proximity and breakout state."""
    out = {
        "nearest_resistance": 0.0,
        "distance_to_resistance_pct": 0.0,
        "resistance_status": "CLEAR",
    }

    if not levels:
        return out

    prices = sorted(float(l["price"]) for l in levels if float(l.get("price", 0.0)) > 0)
    if not prices:
        return out

    current = float(current)
    above_or_equal = [p for p in prices if p >= current]

    if not above_or_equal:
        nearest = prices[-1]
        dist_pct = ((nearest - current) / current) * 100.0
        out.update({
            "nearest_resistance": round(float(nearest), 2),
            "distance_to_resistance_pct": round(float(dist_pct), 2),
            "resistance_status": "BREAK_ABOVE",
        })
        return out

    nearest = above_or_equal[0]
    dist_pct = ((nearest - current) / current) * 100.0
    status = "NEAR_RESISTANCE" if dist_pct <= near_pct * 100.0 else "CLEAR"
    out.update({
        "nearest_resistance": round(float(nearest), 2),
        "distance_to_resistance_pct": round(float(dist_pct), 2),
        "resistance_status": status,
    })
    return out
