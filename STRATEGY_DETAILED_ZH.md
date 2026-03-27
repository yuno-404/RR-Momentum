# 動能策略完整說明文件

## 1. 文件目的

這份文件是依照目前專案中的實際程式碼整理出的策略說明，目標不是抽象描述交易理念，而是把「程式到底怎麼做」完整翻成可讀的策略文件。內容涵蓋：

- 掃描器如何選股
- 每一個 filter 的實作條件
- RS / PR rank / RS line 的計算方式
- VCP 偵測邏輯與風險報酬估算
- 回測引擎如何進場、出場、部位控管
- 市場 regime 如何影響交易數量與條件
- 目前專案中的限制、偏差與值得注意的地方

---

## 2. 專案中與策略直接相關的檔案

- `scanner.py`
  - 掃描器 CLI 入口，實際上只是把參數轉交給 `scanner_core.scan_stocks`
- `scanner_core.py`
  - 相容層，實際上再轉到 `scanner_runner.scan_stocks`
- `scanner_runner.py`
  - 掃描主流程，負責下載資料、判斷市場環境、計算 RS、套用 5 個 filter、輸出候選股與圖表
- `scanner_filters.py`
  - F1 / F2 / F4 / F5 的核心規則
- `scanner_metrics.py`
  - PR 排名、加權 RS、RS line 趨勢、R:R 估算、三年壓力位偵測
- `scanner_regime.py`
  - 市場 regime 判斷
- `scanner_charts.py`
  - PASS / Near Miss / VCP fail 圖表輸出
- `backtest.py`
  - 回測主程式，重建每日 prefilter、預先運算 VCP 與訊號，最後執行投組模擬

---

## 3. 策略核心哲學

這套策略明確是以 O'Neil / Minervini 類型的動能突破邏輯為骨架，但在程式實作上又多加了幾個量化化的約束：

1. 先用市場 regime 過濾大環境
2. 再在股票池中找相對強勢標的
3. 要求價格貼近高點，而不是從低位反彈股
4. 要求中期均線結構偏多
5. 要求存在 VCP 類型的波動收斂
6. 只接受風險報酬比達標的 setup
7. 回測中用固定停損、固定倉位、最大持股數控制整體風險

也就是說，這不是單純的 momentum ranking 策略，而是：

「市場環境 + 相對強勢 + 結構型整理 + 風報比過濾 + 部位風控」

---

## 4. 掃描器整體流程

`scanner_runner.scan_stocks()` 的流程可以拆成 7 步：

### 4.1 Step 1: 市場 regime 檢查

使用 `scanner_regime.scan_market_regime()` 下載：

- `^GSPC`：S&P 500
- `^IXIC`：NASDAQ

對每個指數計算：

- 當前價格
- 50 日均線
- 200 日均線
- 是否站上 50MA
- 是否站上 200MA
- 50MA 是否在 200MA 之上
- 50MA 是否比 20 個交易日前更高
- 200MA 是否比 20 個交易日前更高

regime 判斷規則：

- `UPTREND`
  - 所有被納入的指數都同時滿足：
    - 價格在 50MA 之上
    - 價格在 200MA 之上
    - 50MA > 200MA
- `DOWNTREND`
  - 所有被納入的指數都同時滿足：
    - 價格跌破 50MA
    - 50MA < 200MA
- 其他情況為 `CHOPPY`

注意：

- 這裡的 scanner 只把 regime 顯示出來，不會直接在選股時把股票剔除
- 真正把 regime 納入「能不能交易」的是 `backtest.py`

### 4.2 Step 2: 載入股票池

來源：

- `sp500`
  - 透過 Wikipedia S&P 500 成分股表抓 symbol 與 sector
- `sp1500`
  - 組合 S&P 500、S&P 400、S&P 600
  - 並對股票標記 `large` / `mid` / `small`
- 自訂清單
  - 例如 `AAOI,NVDA,TSLA`

### 4.3 Step 3: 下載歷史價格

透過 `yfinance.download()` 分批抓資料：

- 期間：`5y`
- 調整價格：`auto_adjust=True`
- 批次大小：50 檔

保留條件：

- 至少有 100 根以上資料才納入後續分析

### 4.4 Step 4: 計算加權 RS 與 PR rank

先對每檔股票計算「加權 RS 分數」，再做橫截面 percentile rank，最後轉成 1~99 的 PR rank。

### 4.5 Step 5: 套用 5 個 filter

五個 filter 分別是：

- F1：接近 52 週高點
- F2：自 base 低點反彈幅度夠大
- F3：RS rank 達標且 RS line 趨勢上升
- F4：50MA > 200MA，且兩者都上升
- F5：存在 VCP 收斂結構

### 4.6 Step 6: 輸出結果

輸出兩類：

- `candidates`
  - 五個 filter 全部通過
- `near_miss`
  - 沒有全過，但至少通過 3 個 filter

### 4.7 Step 7: 圖表輸出

產生三類圖：

- `*_PASS.png`
- `*_VCP_FAIL.png`
- `*_NEAR_MISS(...).png`

---

## 5. Relative Strength 與排名邏輯

### 5.1 加權 RS 分數

程式在 `scanner_metrics.compute_weighted_rs_score()` 中定義：

- Q1：最近 63 交易日報酬，權重 40%
- Q2：前一個 63 交易日報酬，權重 20%
- Q3：再前一個 63 交易日報酬，權重 20%
- Q4：最早的 63 交易日報酬，權重 20%

公式：

`weighted_rs = 0.4*q1 + 0.2*q2 + 0.2*q3 + 0.2*q4`

這表示策略偏重最近一季的強度，但仍保留過去 1 年其餘三季的背景權重。

### 5.2 PR rank 計算

`compute_pr_rank()` 會把所有 RS 分數做 percentile rank，再乘以 99，並限制在 1~99。

所以：

- 99 表示幾乎位於股票池最強端
- 50 表示中位附近
- 1 表示最弱端

### 5.3 大型股與中小型股的門檻差異

在 `scanner_runner.py` 中：

- `large cap` 使用固定門檻 `70`
- 其他股票使用 `min_rs`，預設為 `85`

也就是說：

- S&P 500 大型股可以接受較低的 RS rank
- 中型股 / 小型股需要更高的橫截面強度

這是一個很重要的設計，因為它不是全市場一把尺，而是承認大型股的趨勢股常在「相對名次稍低但品質較高」的區間出現。

---

## 6. F3: RS line 趨勢條件

程式在 `compute_rs_line_trend()` 中把 RS line 定義為：

`rs_line = stock_close / benchmark_close`

其中 benchmark 為 S&P 500 的 close。

通過條件有四個：

1. 最近 63 日的 RS 報酬必須大於 0
2. 最近 63 日 RS 表現必須不差於前一段 63 日
3. RS line 必須在自己的 50 日均線之上
4. RS line 的 50 日均線必須高於 20 個交易日前

因此 F3 其實不是單純「PR rank 達標」，而是：

- 橫截面 rank 達標
- 相對 S&P 500 的 RS line 結構也要向上

這讓策略避免只因某檔股票在弱市場中相對沒那麼弱，卻仍被誤認為真正的強勢股。

---

## 7. 五個 Filter 的精確定義

## 7.1 F1: 接近 52 週高點

函式：`check_filter_1_near_highs(hist, threshold=0.25)`

條件：

- 至少要有 252 個交易日資料
- 取最近 252 日的最高價作為 52 週高點
- 目前收盤價距離該高點不超過 25%

公式：

`distance = (high_52w - current_close) / high_52w`

通過條件：

`distance <= 0.25`

策略意義：

- 只找仍然靠近高點的強勢股
- 不做深跌反彈股

回傳除了 pass/fail 之外，也會回傳距高點百分比，作為後續報表顯示。

## 7.2 F2: 自 base 低點至少反彈 25%

函式：`check_filter_2_momentum(hist, min_rally=0.25)`

條件：

- 至少要有 126 天資料
- 用最近 252 天的最低價當作 `base_low`
- 目前價格相對 `base_low` 至少上漲 25%

公式：

`rally = (current_close - base_low) / base_low`

通過條件：

`rally >= 0.25`

策略意義：

- 股票不只是接近高點，還必須真的已經從底部展現出足夠推進
- 等於要求這是一檔已經證明自己能從 base 啟動的標的

## 7.3 F3: RS rank + RS line rising

組成：

- PR rank 要達門檻
- `compute_rs_line_trend()` 要回傳 True

大型股門檻：

- 70

其他股票預設：

- 85

## 7.4 F4: 均線多頭排列且都在上升

函式：`check_filter_4_ma_alignment(hist)`

條件：

- 至少要有 220 天資料
- 計算 50MA 與 200MA
- 當前 50MA > 當前 200MA
- 當前 50MA > 20 個交易日前的 50MA
- 當前 200MA > 20 個交易日前的 200MA

因此 F4 的實際含義是：

- 中期趨勢在長期趨勢之上
- 中期趨勢上升
- 長期趨勢也上升

不是只看黃金交叉，而是看雙均線是否同步向上。

## 7.5 F5: VCP 偵測

這是整個策略最有辨識度的部分，實作在 `scanner_filters.detect_vcp()`

---

## 8. VCP 偵測的實作細節

### 8.1 使用區間

- 最多回看最近 200 個交易日
- 少於 60 日直接失敗

### 8.2 Swing point 偵測方法

使用 `order = 5`：

- 若某日 high 大於等於左右各 5 天窗口中的最大 high，視為 swing high
- 若某日 low 小於等於左右各 5 天窗口中的最小 low，視為 swing low

接著把 high / low 混合後按時間排序，再做「交替過濾」：

- 避免連續兩個 high 或連續兩個 low
- 若連續同類型，只保留更極端的那個

這樣最後會形成高低交替的 swing 序列。

### 8.3 收斂段定義

當序列中出現：

- 一個 swing high
- 下一個是 swing low

就定義為一段 contraction。

其深度：

`depth = (swing_high - swing_low) / swing_high`

每段 contraction 也會記錄：

- high_idx / low_idx
- high_price / low_price
- 該段平均成交量

### 8.4 通過 VCP 的必要條件

至少要有 3 段 contraction，並檢查最後三段 `c1, c2, c3`：

1. 第一段深度要介於 10% 到 40%
   - `10 <= c1_depth <= 40`
2. 三段深度必須遞減
   - `c1 > c2 > c3`
3. 第三段必須夠緊
   - `c3 <= 10%`
4. 第三段低點不能跌破第一段低點
   - `low3 >= low1`
5. 第三段平均成交量要小於第一段平均成交量
   - `avg_vol_c3 < avg_vol_c1`
6. 第三段 contraction 區間內必須至少有一天成交量低於 50 日均量
   - 視為 `volume dry-up`

只要任一條件不滿足，就不算 VCP pass。

### 8.5 這個 VCP 版本的特性

這份程式不是經典圖學式的「人工主觀 VCP」，而是偏量化約束版：

- 強調最後三段收斂
- 強調遞減深度
- 強調量縮
- 強調最後一段夠緊
- 強調低點墊高

因此它抓到的是比較工整、比較 textbook 的 VCP。

### 8.6 可能的偏差

由於 swing point 完全由 local extrema 算法決定，以下情況可能造成誤判：

- 高波動股容易產生很多雜訊 swing
- 盤整區如果非常平，swing high / low 可能不穩定
- VCP 結構若跨度大於 200 日，可能被截斷
- 如果量縮很漂亮但沒有單日低於 50 日均量，也會被判失敗

---

## 9. 壓力位與阻力判斷

### 9.1 三年壓力位偵測

函式：`detect_resistance_3y(hist_3y, order=10, band_pct=0.02)`

做法：

1. 在 high 序列中找 3 年內 local highs
2. 以價格接近程度分群
   - 如果價格差在 2% 內視為同一群
3. 依據：
   - touches 數量
   - 壓力價格高低
   做排序
4. 取前 3 個群作為主要壓力位

每個 level 會包含：

- 平均價格
- touches 次數
- band_low / band_high
- 最後觸及日期

### 9.2 壓力狀態

函式：`assess_resistance_status(current, levels, near_pct=0.02)`

狀態：

- `CLEAR`
  - 最近壓力還有超過 2% 空間
- `NEAR_RESISTANCE`
  - 距最近壓力位在 2% 內
- `BREAK_ABOVE`
  - 現價已高於所有偵測壓力位

scanner 只把這個資訊顯示出來，但 backtest 在 `CHOPPY` regime 會真的使用它做額外過濾。

---

## 10. 風險報酬比估算

函式：`estimate_rr_ratios(hist, vcp_info)`

### 10.1 停損來源

scanner 版本的 R:R 估算，不是用固定 8% 停損，而是用：

- `last_contraction`
  - 也就是 VCP 最後一段收斂深度

所以：

`stop_pct = last_contraction / 100`

### 10.2 兩種 target

程式估兩種目標：

1. `rr_to_52w`
   - 目標為 52 週高點
2. `rr_breakout`
   - 目標為 breakout projection

其中 breakout projection 做法是：

- `pivot = 最後一段 contraction 的 high_price`
- `target_breakout = pivot * (1 + c1_depth)`

也就是把第一段較大的收斂深度，當作 breakout 後的投射幅度。

### 10.3 scanner 如何使用 R:R

在 scanner 階段：

- 只做觀察與輸出
- 不作硬性篩選

在 backtest 階段：

- 會把類似概念轉成 entry gate
- 只有 `rr_value >= rr_min` 才能形成正式訊號

---

## 11. 掃描器輸出欄位怎麼解讀

每一個 candidate 主要欄位：

- `ticker`
- `sector`
- `cap`
- `price`
- `high_52w`
- `distance_from_high`
- `rally_from_base`
- `rs_rank`
- `rs_rising`
- `ma`
  - 50MA / 200MA 與 slope
- `vcp`
  - 三段 depth、是否遞減、是否量縮、是否有 dry-up day
- `rr_to_52w`
- `rr_breakout`
- `rr_info`
  - 內含 pivot、目標價、stop_pct
- `resistance_levels`
- `nearest_resistance`
- `distance_to_resistance_pct`
- `resistance_status`

`scan_results.json` 只保存：

- 掃描時間
- regime
- index_data
- candidates

near miss 不會寫進 JSON，只顯示在 console 與圖表輸出中。

---

## 12. 回測系統設計總覽

`backtest.py` 與 scanner 不完全相同。它不是簡單把 scanner 每天重跑一次，而是做了大量向量化與加速設計：

1. 先下載全體 OHLCV
2. 建 benchmark 與 regime
3. 先向量化產出每日 prefilter mask
4. 再針對 prefilter 為 True 的日期去跑快速 VCP + R:R 檢查
5. 把通過的點存成 signal_map
6. 最後在 simulation loop 中每日依序執行：
   - 先出場
   - 再更新 equity
   - 再判斷 regime
   - 再考慮進場

這代表回測不是「收盤看見訊號就用收盤價成交」，而是：

- 當天收盤前的條件判定訊號
- 下一個交易日的開盤價進場

---

## 13. 回測使用的主要 CLI 參數

重要參數如下：

- `--universe`
  - `sp500`、`sp1500` 或自訂 ticker 清單
- `--years`
  - 回測年數，預設 10
- `--initial-capital`
  - 初始資金，預設 100000
- `--min-rs`
  - 中小型股 RS 門檻，預設 85
- `--rr-min`
  - 最低可接受 R:R，預設 3.0
- `--stop-pct`
  - 固定停損比率，預設 8%
- `--position-pct`
  - 每筆交易固定配置比例，預設 12.5%
- `--max-positions`
  - 最大同時持股數，預設 8
- `--workers`
  - 平行運算 worker 數
- `--engine`
  - `process` / `thread` / `single`
- `--breakout-buffer-pct`
  - 突破 pivot 的額外 buffer
- `--f4-exit-days`
  - F4 連續失敗幾天出場，預設 2
- `--f3-exit-days`
  - F3 / RS 連續失敗幾天出場，預設 3
- `--regime-filter`
  - `on` / `off`
- `--choppy-max-positions`
  - CHOPPY 時的最大持股數，預設 4

---

## 14. 回測中的資料下載與快取

### 14.1 OHLCV 快取

`backtest.py` 使用本地 pickle 快取：

- 目錄：`backtest_cache`
- 依 tickers + years 產生 md5 簽名檔名
- 24 小時內重複執行會直接載入 cache

目的：

- 避免重複下載大批資料
- 加快多次參數測試

### 14.2 benchmark

benchmark 固定為：

- `^GSPC`

回測中的 regime 完全由 S&P 500 建立，不使用 NASDAQ。

這點和 scanner 的 market regime 稍有不同。

---

## 15. 回測中的每日 prefilter

函式：`_build_daily_prefilter()`

這一步會先對每一天、每一檔股票建立下列布林矩陣：

- `f1`
- `f2`
- `f4`
- `rs_rank`
- `rs_rising`

其中：

- F1 與 scanner 相同
- F2 與 scanner 相同
- F4 與 scanner 相同
- RS 分數與 PR rank 的計算方式和 scanner 相同
- `rs_gate = rs_rank >= threshold AND rs_rising`

最後：

`prefilter = f1 & f2 & f4 & rs_gate`

注意：

- 這裡故意還沒做 F5
- 因為 VCP 計算最貴，所以先用前四關把候選日縮小

---

## 16. 回測中的快速 VCP + R:R

函式：`_vcp_rr_fast()`

這是回測性能關鍵。它把 scanner 中較慢的 DataFrame 流程改成 numpy array + sliding window 實作。

### 16.1 功能

在某一檔股票的某一天 `end`：

- 檢查最近 200 日內是否有有效 VCP
- 若有，計算 R:R
- 若 `R:R >= rr_min`，回傳 `(rr_value, pivot)`
- 否則回傳 `(0, 0)`

### 16.2 R:R 的選擇

回測取：

`rr_value = max(rr_52w, rr_breakout)`

也就是只要兩種目標中有一種足夠吸引，就允許進場。

### 16.3 與 scanner 的差異

scanner 的 `estimate_rr_ratios()` 主要是為了展示資訊。

回測的 `_vcp_rr_fast()`：

- 把 VCP 與 R:R 放在同一條熱路徑
- 更早做 fail fast
- 效率更高
- 能大量平行運算

---

## 17. Signal map 與平行運算

函式：`_build_signal_map()`

流程：

1. 先把每檔股票 DataFrame 轉成 numpy arrays
2. 找出 prefilter 為 True 的 day index
3. 每檔股票打包成一個 task
4. 用：
   - `ProcessPoolExecutor`
   - `ThreadPoolExecutor`
   - 或單執行緒
   進行訊號預運算
5. 把結果存成：
   - `signal_map[ticker][day_idx] = SignalPoint(rr_value, pivot)`

接著再反轉成：

- `day_signals[day_idx] = [(ticker, SignalPoint), ...]`

這讓主回測 loop 在某一天查訊號時是 O(1) lookup，而不是重新遍歷所有股票。

---

## 18. 回測進場規則

進場邏輯在 `run_backtest()` 的 simulation loop 中。

### 18.1 基本前提

只有當天 day index 存在訊號才可能進場，代表：

- F1 pass
- F2 pass
- F3 pass
- F4 pass
- F5 / VCP pass
- R:R >= rr_min

### 18.2 regime gate

若 `regime_filter=on`：

- `DOWNTREND`
  - 完全不開新倉
- `CHOPPY`
  - 可開倉，但最多 `choppy_max_positions`
- `UPTREND`
  - 允許到 `max_positions`

### 18.3 CHOPPY 的額外限制

在 `CHOPPY` regime 下，候選股還要額外滿足：

- 目前 close 必須已經 `BREAK_ABOVE` 最近三年主要壓力位

也就是說：

- 震盪市不是完全不做
- 但只做已經明確突破主要歷史壓力的股票

這是一個很重要的風險收縮設計。

### 18.4 候選排序

若同一天候選股很多，排序依據是：

1. 當天 `rs_rank`
2. `rr_value`

兩者都高者優先。

### 18.5 真正成交價

不是當天收盤，而是：

- 下一個交易日的 `Open`

### 18.6 Breakout buffer

若設定 `breakout_buffer_pct > 0`：

- 必須滿足 `entry_price >= pivot * (1 + breakout_buffer_pct)`

預設為 0，所以只要求 next open 不低於 pivot。

---

## 19. 回測部位 sizing

### 19.1 固定資金配置

每筆新倉：

`position_value = current_equity * position_pct`

預設：

- `position_pct = 0.125`

也就是 12.5% 倉位。

### 19.2 股數

`shares = position_value / entry_price`

可為小數，代表回測允許 fractional shares。

### 19.3 停損

回測中的停損不是 VCP 最後收斂深度，而是固定參數：

`stop_price = entry_price * (1 - stop_pct)`

預設：

- `stop_pct = 0.08`

所以這裡和 scanner 的觀察型 R:R 邏輯不同。

### 19.4 每股風險

`risk_per_share = entry_price - stop_price`

用於計算之後的 R multiple。

---

## 20. 回測出場規則

### 20.1 Stop loss

若當日最低價 `low_price <= stop_price`：

- 以 `stop_price` 出場
- 原因：`STOP`

### 20.2 Break-even stop

若當日最高價先達到：

`entry_price + risk_per_share`

也就是 +1R，

則把停損抬到：

- `entry_price`

並標記：

- `moved_to_breakeven = True`

因此這套回測內建一個保本機制：

- 漲到 +1R 後，不再允許單筆交易回吐成虧損

### 20.3 F4 fail exit

若啟用 `f4_exit_days > 0`：

- 只要某檔股票連續 N 天 F4 不成立
- 就在該日收盤價出場

預設：

- 2 天

出場原因：

- `F4_FAIL`

### 20.4 F3 fail exit

若啟用 `f3_exit_days > 0`：

- 若 RS rank 連續 N 天低於所屬門檻
- 就在該日收盤出場

預設：

- 3 天

出場原因：

- `F3_FAIL`

### 20.5 回測結束強制平倉

最後一天仍持有的部位：

- 以最後可用收盤價強制結束
- 原因：`FORCE_CLOSE`

---

## 21. R multiple 定義

每筆交易的：

`r_multiple = (exit_price - entry_price) / risk_per_share`

因為 `risk_per_share` 是固定 8% 停損對應的每股風險，所以：

- 停損出場大致接近 `-1R`
- 漲到 +8% 約等於 `+1R`
- 漲到 +24% 約等於 `+3R`

這使得不同價格、不同股數的交易可以用同一個風險尺度比較。

---

## 22. 績效統計方式

### 22.1 權益曲線

每日先：

- 更新出場
- 再 mark-to-market 未平倉部位
- 算出當日 equity

輸出至：

- `equity_curve.csv`

### 22.2 績效指標

`_equity_stats()` 會計算：

- `total_return_pct`
- `cagr_pct`
- `sharpe`
- `sortino`
- `mdd_pct`
- `calmar`

### 22.3 交易統計

`_trade_stats()` 會計算：

- `trades`
- `win_rate_pct`
- `avg_r`
- `profit_factor`
- `avg_hold_days`

### 22.4 regime 統計

還會額外按進場當時 regime 分組：

- `UPTREND`
- `CHOPPY`
- `DOWNTREND`

統計：

- 交易數
- 勝率
- 平均 R

---

## 23. 目前已有回測結果可讀出的訊息

以下是 repo 內現成摘要的兩個關鍵比較：

### 23.1 開啟 regime filter

檔案：`backtest_regime/backtest_summary.json`

主要結果：

- 10 年、SP1500
- 總報酬 `124.46%`
- CAGR `8.62%`
- Sharpe `0.604`
- MDD `28.9%`
- 248 筆交易
- 勝率 `25.4%`
- 平均 `0.37R`
- Profit factor `1.929`

regime breakdown：

- `UPTREND`
  - 181 筆
  - 勝率 27.07%
  - 平均 0.45R
- `CHOPPY`
  - 67 筆
  - 勝率 20.9%
  - 平均 0.153R
- `DOWNTREND`
  - 0 筆

### 23.2 關閉 regime filter

檔案：`backtest_no_regime/backtest_summary.json`

主要結果：

- 總報酬 `72.51%`
- CAGR `5.73%`
- Sharpe `0.436`
- MDD `31.66%`
- 283 筆交易
- 勝率 `24.73%`
- 平均 `0.221R`
- Profit factor `1.491`

### 23.3 可推導的結論

從目前結果看，regime filter 對這套策略是有明顯正面影響的：

- 報酬更高
- Sharpe 更高
- 最大回撤更低
- 平均 R 更高
- Profit factor 更高

換句話說，這套策略不是單靠 stock selection 就足夠，市場環境過濾本身就是 alpha / risk control 的重要來源。

---

## 24. 掃描器與回測器的差異

這部分非常重要，因為很多人會誤以為 scanner 輸出的邏輯就是 backtest 的邏輯，但其實不完全一樣。

### 24.1 停損基準不同

scanner：

- 觀察型 R:R 以 `last_contraction` 當停損比例

backtest：

- 真實交易停損固定為 `stop_pct`，預設 8%

### 24.2 regime 判斷不同

scanner：

- 使用 S&P500 + NASDAQ

backtest：

- 只用 S&P500

### 24.3 resistance 的用途不同

scanner：

- 只是報告資訊

backtest：

- 在 `CHOPPY` 中作為硬性 entry gate

### 24.4 scanner 是單次 snapshot

backtest 是逐日滾動模擬。

因此：

- scanner 比較像每日盤後選股器
- backtest 才是策略績效驗證器

---

## 25. 策略優點

### 25.1 強弱分層清楚

不是只看價格趨勢，而是把：

- 橫截面相對強弱
- 相對 benchmark 的 RS line
- 結構型整理

一起納入。

### 25.2 VCP 把「亂追高」過濾掉

單純 momentum 策略常會買在延伸過頭的位置，但這套策略要求收斂結構，讓進場點更偏向整理末端。

### 25.3 regime filter 有實證幫助

從現成回測結果來看，regime filter 明顯改善表現。

### 25.4 風控簡潔

- 固定倉位
- 固定停損
- 最多持股數
- CHOPPY 降低持股上限
- +1R 後移保本

這讓整體策略行為穩定且容易解釋。

---

## 26. 策略限制與風險

### 26.1 資料來源依賴 yfinance

- 如果資料缺漏或 corporate action 處理不完整，訊號會受影響

### 26.2 VCP 定義偏硬

- 很多主觀上漂亮的 base 可能因量縮條件或 swing 切點差異而被排除

### 26.3 未納入基本面

目前完全是技術面 / 相對強弱驅動，沒有 EPS、營收、產業催化等基本面條件。

### 26.4 固定 8% 停損可能與實際型態不一致

有些 setup 其實應該用更緊的 pivot stop，有些又需要更寬的波動容忍；固定 8% 是一致但不一定最適配。

### 26.5 沒有交易成本與滑價

目前輸出裡沒有看到顯式扣除：

- 手續費
- 滑價
- 稅負

所以真實績效通常會略低於回測結果。

### 26.6 同日多檔進場時仍可能高度相關

雖有最大持股數限制，但沒有 sector exposure 或 factor correlation 限制，仍可能在某些主題行情中集中曝險。

---

## 27. 如何實際使用這套策略

### 27.1 每日掃描

先跑：

```bash
python scanner.py
```

或指定股票池與 RS 門檻：

```bash
python scanner.py sp1500 85
python scanner.py sp500 90
python scanner.py NVDA,TSLA,PLTR
```

看輸出重點：

- 市場 regime
- PASS 候選股
- Near miss
- `scan_results.json`
- `charts/`

### 27.2 觀察 candidate 的關鍵欄位

先看：

- `rs_rank`
- `distance_from_high`
- `rally_from_base`
- `vcp.depths`
- `rr_breakout` / `rr_to_52w`
- `resistance_status`

### 27.3 做參數驗證

用 `backtest.py` 比較不同設定：

```bash
python backtest.py --universe sp1500 --years 10 --output backtest_test
python backtest.py --universe sp1500 --years 10 --rr-min 2.5 --output backtest_rr25
python backtest.py --universe sp1500 --years 10 --regime-filter off --output backtest_no_regime
```

### 27.4 讀取回測結果

每個 output 資料夾有：

- `backtest_summary.json`
- `trades.csv`
- `equity_curve.csv`

最先看：

- CAGR
- MDD
- Sharpe
- Profit factor
- Avg R
- regime_stats

---

## 28. 建議後續優化方向

以下是從目前程式結構延伸出來、最值得繼續測試的方向：

1. 讓 scanner 與 backtest 的停損邏輯一致
   - 目前 scanner 用 VCP 最後收斂深度，backtest 用固定 8%
2. 將 breakout entry 改成更明確的 pivot 突破條件
   - 例如收盤突破、量增突破、buffer 突破
3. 加入交易成本與滑價
4. 加入 sector concentration 限制
5. 測試不同 CHOPPY 規則
   - 例如完全不交易 vs 僅交易 BREAK_ABOVE
6. 測試更細的出場機制
   - 分批停利
   - trailing stop
   - 以均線或 pivot low 作為動態停損
7. 讓 resistance 偵測更穩健
   - 加入成交量確認或週線級別壓力
8. 納入基本面條件
   - EPS growth、營收年增、產業主題等

---

## 29. 最後總結

這套專案中的策略，本質上是一套「強勢股突破前整理」的量化版本：

- 用市場 regime 控制大方向
- 用加權 RS + RS line 找真正強勢股
- 用接近高點、從 base 啟動、均線多頭排列做趨勢確認
- 用 VCP 偵測要求波動收斂與量縮
- 用 R:R 篩掉不划算的 setup
- 用固定停損、固定倉位、持股上限與保本機制控制風險

從目前 repo 內已有回測來看，這個架構最關鍵的增益來源之一是 regime filter；而最有個人特色的部分則是 VCP + resistance 的結合。

如果把這份文件當成一句話摘要，可以寫成：

「這是一套以市場環境過濾為前提、專找相對強勢且具 VCP 收斂結構之突破型股票的中期動能策略。」
