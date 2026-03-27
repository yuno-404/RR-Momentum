# 開發任務

## 目前策略調整
- [x] 1. 增加台股上市上櫃公司 universe，讓 scanner / backtest 都可直接使用
- [x] 2. 放寬台股版 VCP 條件，避免過度嚴格導致已起漲股票被排除
- [x] 3. 停利邏輯改為達到 1R 後啟動 5MA / 10MA 出場規則
- [x] 4. 整合 `My-TW-Coverage` 的產業資料，加入台股產業龍頭動能判斷
- [x] 5. 熊市不交易，`CHOPPY` regime 僅允許動用 50% 資金
- [x] 6. 讓 backtest 與 scanner / 策略條件盡量一致，包含 pivot breakout 對齊

## 本輪任務
- [ ] 1. 把 15 年回測與 RR 門檻比較需求寫進任務檔
- [ ] 2. 調整 backtest，支援單次資料下載後批次測試多組 `rr_min`
- [ ] 3. 跑 15 年回測，測試 `RR > 2.5`、`RR > 3.0`、`RR > 3.5`
- [ ] 4. 輸出比較結果並判斷哪個設定較好
- [ ] 5. 完成後更新 checklist

## 驗證清單
- [x] `scanner_tw.py`、`scanner_runner.py`、`scanner_filters.py`、`scanner_regime.py`、`backtest.py` 通過 `py_compile`
- [x] 已可從 `My-TW-Coverage/Pilot_Reports` 解析出台股 coverage、sector 與 leader 資訊
- [ ] 批次 RR 回測功能通過靜態檢查
- [ ] 15 年三組 RR 回測完成
- [ ] 已整理比較摘要並標記較佳設定
