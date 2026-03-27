"""Filter implementations for momentum scanner."""

import numpy as np
import pandas as pd


def check_filter_1_near_highs(hist, threshold=0.25):
    """F1: Price within 25% of 52-week high (1 year only)."""
    if len(hist) < 252:
        return False, 0.0
    high_52w = float(hist["High"].iloc[-252:].max())
    current = float(hist["Close"].iloc[-1])
    distance = (high_52w - current) / high_52w
    return distance <= threshold, round(distance * 100, 1)


def check_filter_2_momentum(hist, min_rally=0.25):
    """F2: Stock rallied at least 25% off its base low."""
    if len(hist) < 126:
        return False, 0.0
    base_low = hist["Low"].iloc[-252:].min() if len(hist) >= 252 else hist["Low"].min()
    current = hist["Close"].iloc[-1]
    rally = (current - base_low) / base_low
    return rally >= min_rally, round(rally * 100, 1)


def check_filter_4_ma_alignment(hist):
    """F4: 50MA > 200MA, both sloping up."""
    if len(hist) < 220:
        return False, {}
    close = hist["Close"]
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()

    current_ma50 = ma50.iloc[-1]
    current_ma200 = ma200.iloc[-1]
    prev_ma50 = ma50.iloc[-20]
    prev_ma200 = ma200.iloc[-20]

    ma50_rising = current_ma50 > prev_ma50
    ma200_rising = current_ma200 > prev_ma200
    ma50_above = current_ma50 > current_ma200

    passed = ma50_above and ma50_rising and ma200_rising

    return passed, {
        "ma50": round(float(current_ma50), 2),
        "ma200": round(float(current_ma200), 2),
        "ma50_slope": "up" if ma50_rising else "down",
        "ma200_slope": "up" if ma200_rising else "down",
        "alignment": "bullish" if ma50_above else "bearish",
    }


def detect_vcp(hist, market="us"):
    """
    F5: Detect VCP using swing point analysis.

    1. Find alternating swing highs and swing lows (local extrema)
    2. Measure contraction depth = (swing_high - swing_low) / swing_high
    3. Last 3 contractions must be progressively decreasing
    4. Volume must be declining across the contraction period
    """
    is_tw = market == "tw"
    lookback = min(260 if is_tw else 200, len(hist))
    if lookback < 60:
        return False, {}

    high = hist["High"].iloc[-lookback:]
    low = hist["Low"].iloc[-lookback:]
    volume = hist["Volume"].iloc[-lookback:]
    volume_full = hist["Volume"]
    close = hist["Close"].iloc[-lookback:]

    if isinstance(high, pd.DataFrame):
        high = high.iloc[:, 0]
    if isinstance(low, pd.DataFrame):
        low = low.iloc[:, 0]
    if isinstance(volume, pd.DataFrame):
        volume = volume.iloc[:, 0]
    if isinstance(volume_full, pd.DataFrame):
        volume_full = volume_full.iloc[:, 0]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]

    high_arr = high.values.astype(float)
    low_arr = low.values.astype(float)
    close_arr = close.values.astype(float)
    vol_arr = volume.values.astype(float)
    vol50_full = volume_full.rolling(50).mean()
    close_full = hist["Close"]
    if isinstance(close_full, pd.DataFrame):
        close_full = close_full.iloc[:, 0]
    n = len(high_arr)
    offset = len(hist) - lookback

    order = 5
    swing_highs = []
    swing_lows = []

    for i in range(order, n):
        left = max(0, i - order)
        right = min(n, i + order + 1)

        if high_arr[i] >= np.max(high_arr[left:right]):
            swing_highs.append((i, float(high_arr[i])))

        if low_arr[i] <= np.min(low_arr[left:right]):
            swing_lows.append((i, float(low_arr[i])))

    all_swings = ([(i, p, 'H') for i, p in swing_highs] +
                  [(i, p, 'L') for i, p in swing_lows])
    all_swings.sort(key=lambda x: x[0])

    filtered = []
    for pt in all_swings:
        if not filtered:
            filtered.append(pt)
        elif filtered[-1][2] != pt[2]:
            filtered.append(pt)
        elif pt[2] == 'H' and pt[1] > filtered[-1][1]:
            filtered[-1] = pt
        elif pt[2] == 'L' and pt[1] < filtered[-1][1]:
            filtered[-1] = pt

    contractions = []
    for i in range(len(filtered) - 1):
        if filtered[i][2] == 'H' and filtered[i + 1][2] == 'L':
            h_idx, h_price = filtered[i][0], filtered[i][1]
            l_idx, l_price = filtered[i + 1][0], filtered[i + 1][1]
            depth = round((h_price - l_price) / h_price * 100, 1)
            # Only count down-day volume (selling pressure)
            if l_idx > h_idx:
                c_vol = vol_arr[h_idx:l_idx + 1]
                c_close = close_arr[h_idx:l_idx + 1]
                down_mask = np.zeros(len(c_close), dtype=bool)
                down_mask[1:] = c_close[1:] < c_close[:-1]
                avg_vol = float(np.mean(c_vol[down_mask])) if down_mask.any() else float(np.mean(c_vol))
            else:
                avg_vol = 0
            contractions.append({
                'depth': depth,
                'high_idx': offset + h_idx,
                'low_idx': offset + l_idx,
                'high_price': round(h_price, 2),
                'low_price': round(l_price, 2),
                'avg_vol': avg_vol,
            })

    profile = "tw_relaxed" if is_tw else "us_strict"
    min_contractions = 2 if is_tw else 3
    if len(contractions) < min_contractions:
        return False, {
            "profile": profile,
            "depths": [c["depth"] for c in contractions],
            "last_contraction": contractions[-1]["depth"] if contractions else 0,
            "decreasing": False,
            "vol_declining": False,
            "vol_dryup_day": False,
            "c1_depth_ok": False,
            "c3_tight": False,
            "low3_higher_than_low1": False,
            "blocking_conditions": ["not_enough_contractions"],
            "contractions": contractions[-3:] if len(contractions) >= 3 else contractions,
        }

    selected = contractions[-3:] if len(contractions) >= 3 else contractions[-2:]
    c1 = selected[0]
    c_last = selected[-1]
    depths = [c["depth"] for c in selected]

    c1_depth = float(c1["depth"])
    c2_depth = float(selected[1]["depth"]) if len(selected) >= 2 else 0.0
    c3_depth = float(selected[2]["depth"]) if len(selected) >= 3 else float(c_last["depth"])

    low1 = float(c1["low_price"])
    low_last = float(c_last["low_price"])

    c1_depth_ok = (8 <= c1_depth <= 45) if is_tw else (10 <= c1_depth <= 40)
    if len(selected) >= 3:
        decreasing = (c1_depth > c2_depth) and (c3_depth <= c2_depth + (3.0 if is_tw else -1e-9))
    else:
        decreasing = c1_depth > c2_depth
    c3_tight = c3_depth <= (12 if is_tw else 10)
    low3_higher_than_low1 = low_last >= (low1 * (0.95 if is_tw else 1.0))
    vol_declining = c_last["avg_vol"] <= (c1["avg_vol"] * (1.15 if is_tw else 1.0))

    c_last_h_idx = int(c_last["high_idx"])
    c_last_l_idx = int(c_last["low_idx"])
    c_last_vol = volume_full.iloc[c_last_h_idx:c_last_l_idx + 1]
    c_last_vol50 = vol50_full.iloc[c_last_h_idx:c_last_l_idx + 1]
    c_last_close = close_full.iloc[c_last_h_idx:c_last_l_idx + 1]
    # Only check volume dry-up on down days (selling pressure)
    down_days = (c_last_close < c_last_close.shift(1)).fillna(False)
    if down_days.any():
        down_vol = c_last_vol[down_days]
        down_vol50 = c_last_vol50[down_days]
        vol_dryup_day = bool((down_vol < down_vol50).fillna(False).any())
        if is_tw and not vol_dryup_day:
            avg_down_vol = down_vol.mean()
            avg_down_vol50 = down_vol50.dropna().mean()
            vol_dryup_day = bool(avg_down_vol50 > 0 and avg_down_vol <= avg_down_vol50 * 1.05)
    else:
        # No down days in last contraction = no selling pressure (bullish)
        vol_dryup_day = True

    blocking = []
    if not c1_depth_ok:
        blocking.append("c1_depth_range")
    if not decreasing:
        blocking.append("decreasing_contractions")
    if not c3_tight:
        blocking.append("last_contraction_too_wide")
    if not low3_higher_than_low1:
        blocking.append("last_low_undercut")
    if not vol_declining:
        blocking.append("volume_not_contracting")
    if not vol_dryup_day:
        blocking.append("no_volume_dryup")

    passed = not blocking

    return passed, {
        "profile": profile,
        "depths": depths,
        "last_contraction": c3_depth,
        "decreasing": decreasing,
        "vol_declining": vol_declining,
        "vol_dryup_day": vol_dryup_day,
        "c1_depth_ok": c1_depth_ok,
        "c3_tight": c3_tight,
        "low3_higher_than_low1": low3_higher_than_low1,
        "c1_depth": c1_depth,
        "c2_depth": c2_depth,
        "c3_depth": c3_depth,
        "low1": low1,
        "low3": low_last,
        "blocking_conditions": blocking,
        "contractions": selected,
    }
