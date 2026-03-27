# Momentum Stock Scanner

以 Minervini/O'Neil 動能邏輯為核心的 Python 選股工具。

本工具會掃描股票池（S&P 500 / S&P 1500 / 自訂清單），計算 RS 排名、檢查趨勢與 VCP 結構，並輸出圖表與 JSON 結果，方便快速挑選候選標的。

## 功能總覽

- 大盤環境判斷（S&P 500 + NASDAQ）：`UPTREND` / `CHOPPY` / `DOWNTREND`
- 加權 RS 分數 + PR Rank（1-99）
- RS Line 趨勢檢查（相對 S&P 500）
- 五大過濾條件（需全過）
  - `F1`：距 52 週高點不超過 25%
  - `F2`：自基底上漲至少 25%
  - `F3`：PR 達門檻且 RS Line 上升
  - `F4`：50MA > 200MA 且兩者皆上升
  - `F5`：VCP 收縮成立（含量縮與乾量）
- 雙 R:R 觀察值（非硬過濾）
  - `rr_to_52w`
  - `rr_breakout`
- 壓力分析（僅對 PASS 標的輸出）
  - 以 5 年日線抓局部高點並群聚為 1~3 條主要壓力區
  - 輸出最近壓力距離與狀態：`NEAR_RESISTANCE` / `CLEAR` / `BREAK_ABOVE`
- 圖表自動輸出（含壓力線/壓力帶、右側壓力標示、標題 RES 狀態）

## 專案結構

- `scanner.py`：CLI 入口
- `scanner_runner.py`：主流程（下載、篩選、輸出）
- `scanner_data.py`：股票池資料來源（S&P 500/400/600）
- `scanner_filters.py`：F1/F2/F4/F5 過濾條件
- `scanner_metrics.py`：RS、R:R、壓力偵測與狀態評估
- `scanner_regime.py`：大盤環境判斷
- `scanner_charts.py`：圖表輸出

## 環境需求

- Python 3.10+
- 可連網（Yahoo Finance、Wikipedia）

## 安裝

1) 建立並啟用虛擬環境

Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Windows CMD:

```cmd
python -m venv venv
venv\Scripts\activate.bat
```

macOS / Linux:

```bash
python3 -m venv venv
source venv/bin/activate
```

2) 安裝相依套件

```bash
pip install -r requirements.txt
```

## 使用方式

```bash
python scanner.py [universe] [min_pr]
```

- `universe`（可省略）
  - `sp500`（預設）
  - `sp1500`
  - 自訂清單（逗號分隔），例如：`AAOI,NVDA,TSLA`
- `min_pr`（可省略）：非大型股 PR 門檻，預設 `85`

範例：

```bash
# 預設：S&P 500 / PR 85
python scanner.py

# 全市場（S&P 1500）
python scanner.py sp1500

# S&P 500，PR 更嚴格
python scanner.py sp500 90

# 自訂清單
python scanner.py AAOI,NVDA,TSLA
```

## 回測（10 年）

新增 `backtest.py`，可用你的策略規則做歷史回測：

- 5 filter 全過才可入場
- `R:R >= 2:1` 才允許交易
- 每筆固定倉位 `12.5%`
- 每筆固定停損 `8%`（單筆風險約為總資金 `1%`）
- 預設最多同時 `8` 檔
- 輸出 `Sharpe`、`MDD`、交易統計與 equity curve

範例：

```bash
# 10 年，S&P 500，使用預設規則
python backtest.py --universe sp500 --years 10

# 10 年，S&P 1500，多執行緒
python backtest.py --universe sp1500 --years 10 --workers 8
```

輸出檔案（預設資料夾 `backtest_results/`）：

- `backtest_summary.json`
- `trades.csv`
- `equity_curve.csv`

## 輸出內容

- Console：
  - PASS 候選股明細
  - Near Miss（3~4/5）列表
- `scan_results.json`：結構化掃描結果（主要為 PASS 候選）
- `charts/`：圖表輸出
  - `*_PASS.png`
  - `*_VCP_FAIL.png`
  - `*_NEAR_MISS(...).png`

### PASS 候選新增壓力欄位

- `resistance_levels`：1~3 條主要壓力（含觸碰次數）
- `nearest_resistance`：最近壓力價
- `distance_to_resistance_pct`：距最近壓力百分比
- `resistance_status`：`NEAR_RESISTANCE` / `CLEAR` / `BREAK_ABOVE`

## 計算說明（重點）

- `rr_to_52w`：以 52 週高點作為目標報酬
- `rr_breakout`：以突破投影目標作為報酬
- 風險 `R` 以最後一次收縮幅度（`last_contraction`）估算
- R:R 目前為觀察指標，不是硬性篩選條件

## 注意事項

- `sp1500` 全掃描需數分鐘（視網路與資料源速度而定）
- 結果依賴外部資料品質（Yahoo Finance/Wikipedia）
- 本專案為研究與觀察用途，不構成投資建議
