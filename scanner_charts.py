"""Chart generation for scanner outputs."""

import os

import matplotlib

matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.transforms as mtransforms
import pandas as pd


def generate_chart(
    ticker,
    hist,
    vcp_info,
    rs_rank,
    rr_info,
    status,
    output_dir="charts",
    resistance_levels=None,
    nearest_resistance=None,
    resistance_status=None,
    distance_to_resistance_pct=None,
):
    """Generate price + volume chart with VCP swing point overlay."""
    os.makedirs(output_dir, exist_ok=True)

    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]
    volume = hist["Volume"]

    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    if isinstance(high, pd.DataFrame):
        high = high.iloc[:, 0]
    if isinstance(low, pd.DataFrame):
        low = low.iloc[:, 0]
    if isinstance(volume, pd.DataFrame):
        volume = volume.iloc[:, 0]

    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()

    chart_len = min(252, len(close))
    close_c = close.iloc[-chart_len:]
    vol_c = volume.iloc[-chart_len:]
    ma50_c = ma50.iloc[-chart_len:]
    ma200_c = ma200.iloc[-chart_len:]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8),
                                   gridspec_kw={'height_ratios': [3, 1]},
                                   sharex=True)

    ax1.plot(close_c.index, close_c.values, 'k-', linewidth=1, label='Price')
    ax1.plot(ma50_c.index, ma50_c.values, 'b-', linewidth=1, label='50MA')
    ax1.plot(ma200_c.index, ma200_c.values, 'r-', linewidth=1, label='200MA')

    high_52w = float(high.iloc[-252:].max()) if len(high) >= 252 else float(high.max())
    ax1.axhline(y=high_52w, color='green', linestyle='--', alpha=0.5,
                label=f'52W High: ${high_52w:.2f}')

    contraction_colors = ['#d62728', '#ff7f0e', '#2ca02c']
    if vcp_info and 'contractions' in vcp_info:
        for j, c in enumerate(vcp_info['contractions']):
            h_idx = c['high_idx']
            l_idx = c['low_idx']
            if h_idx >= len(hist) or l_idx >= len(hist):
                continue
            h_date = hist.index[h_idx]
            l_date = hist.index[l_idx]
            h_price = c['high_price']
            l_price = c['low_price']
            depth = c['depth']

            color = contraction_colors[j] if j < 3 else 'gray'

            ax1.plot([h_date, l_date], [h_price, l_price],
                     '--', color=color, linewidth=2.5, alpha=0.8)

            ax1.plot(h_date, h_price, 'v', color='red', markersize=10, zorder=5)
            ax1.plot(l_date, l_price, '^', color='green', markersize=10, zorder=5)

            mid_x = h_date + (l_date - h_date) / 2
            mid_y = (h_price + l_price) / 2
            ax1.annotate(f'-{depth}%', xy=(mid_x, mid_y),
                         fontsize=11, fontweight='bold',
                         ha='center', va='center',
                         bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                                   edgecolor=color, alpha=0.85))

    if resistance_levels:
        txt_transform = mtransforms.blended_transform_factory(ax1.transAxes, ax1.transData)
        for lvl in resistance_levels:
            level_price = float(lvl.get('price', 0.0))
            touches = int(lvl.get('touches', 0))
            if level_price <= 0:
                continue

            band_low = float(lvl.get('band_low', level_price))
            band_high = float(lvl.get('band_high', level_price))
            ax1.axhline(y=level_price, color='#d62728', linestyle='-', alpha=0.45, linewidth=1.6)
            if band_high > band_low:
                ax1.axhspan(band_low, band_high, color='#d62728', alpha=0.08)

            dist_text = ""
            if nearest_resistance is not None and distance_to_resistance_pct is not None and abs(level_price - float(nearest_resistance)) < 1e-9:
                dist_text = f" ({distance_to_resistance_pct:+.2f}%)"

            ax1.text(1.005, level_price,
                     f"RES {level_price:.2f} x{touches}{dist_text}",
                     transform=txt_transform,
                     color='#b22222', fontsize=8, va='center', ha='left', clip_on=False)

    rr_52w = float(rr_info.get("rr_to_52w", 0.0)) if isinstance(rr_info, dict) else 0.0
    rr_breakout = float(rr_info.get("rr_breakout", 0.0)) if isinstance(rr_info, dict) else 0.0
    rr_52w_str = f'{rr_52w}:1' if rr_52w > 0 else 'N/A'
    rr_breakout_str = f'{rr_breakout}:1' if rr_breakout > 0 else 'N/A'
    if resistance_status is None:
        resistance_status = 'N/A'
    res_dist_str = 'N/A' if distance_to_resistance_pct is None else f'{distance_to_resistance_pct:+.2f}%'
    status_key = status.split('(')[0].strip()
    title_color = {'PASS': 'green', 'VCP_FAIL': '#cc6600', 'NEAR_MISS': '#cc6600'}.get(
        status_key, 'black')
    ax1.set_title(
        f'{ticker}  |  RS: {rs_rank}  |  RR52: {rr_52w_str}  |  RRBO: {rr_breakout_str}  '
        f'|  RES: {resistance_status} ({res_dist_str})  |  {status}',
                   fontsize=14, fontweight='bold', color=title_color)
    ax1.legend(loc='upper left', fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylabel('Price ($)')

    close_vals = close_c.values
    vol_colors = []
    for j in range(len(close_vals)):
        if j == 0:
            vol_colors.append('gray')
        elif close_vals[j] >= close_vals[j - 1]:
            vol_colors.append('#2ca02c')
        else:
            vol_colors.append('#d62728')

    ax2.bar(vol_c.index, vol_c.values, color=vol_colors, alpha=0.6, width=1)
    vol_ma = vol_c.rolling(50).mean()
    ax2.plot(vol_ma.index, vol_ma.values, 'b-', linewidth=1, label='50d Avg Vol')
    ax2.set_ylabel('Volume')
    ax2.legend(loc='upper left', fontsize=9)
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate(rotation=45)

    plt.tight_layout()
    safe_status = status.replace(':', '-').replace(' ', '_').replace('(', '').replace(')', '')
    filepath = os.path.join(output_dir, f"{ticker}_{safe_status}.png")
    plt.savefig(filepath, dpi=100, bbox_inches='tight')
    plt.close(fig)
    return filepath
