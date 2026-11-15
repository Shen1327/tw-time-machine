# 台股時光機

用一檔股票自己的歷史日 K 重演一萬次，看你的進出場規則（停損 −10%、獲利超過 +10% 後沒續漲就賣、最長持有 20 天）會遇到什麼。
涵蓋全部上市＋上櫃普通股，每個交易日收盤後由 GitHub Actions 自動更新資料、重算全市場，並發佈到 GitHub Pages。

**模擬不是預測。** 回測顯示它對「會不會漲」幾乎沒有預測力（相關係數 0.08），但對「會不會被停損掃出場」校準得很好。詳見網頁最下方的回測說明。

## 架構

```
data/daily/YYYYMMDD.json   每個交易日的全市場收盤行情（上市：證交所 MI_INDEX；上櫃：櫃買中心 otc）
data/holidays.json         已確認沒有交易的日期（避免重複查詢）
scripts/fetch_daily.py     補抓缺少的交易日（每次請求間隔 4 秒、單次最多 25 天，避免被證交所封鎖；第一次部署會先用 Yahoo 補歷史）
scripts/engine.py          模擬引擎：區塊重抽日 K、漂移收縮、波動調整、漲跌停截斷、盤中觸價停損、費用
scripts/build_site.py      產生 site/：每檔日 K（data/bars/XXXX.json）、全市場掃描（data/scan.json）、meta.json
web/index.html             前端（同一套引擎的 JavaScript 版，模擬在瀏覽器裡跑）
.github/workflows/update.yml  排程：台北時間 15:40 與 18:10（週一～五）
```

## 第一次部署

1. 在 GitHub 建一個 **public** repo（public 的 Actions 分鐘數不限，Pages 也免費），把這整個資料夾推上去。
2. 到 repo 的 **Settings → Pages → Build and deployment → Source** 選 **GitHub Actions**。
3. 到 **Actions** 分頁，點「每日更新台股資料並部署網站」→ **Run workflow** 跑第一次（之後每個交易日自動跑）。
4. 跑完後網址會顯示在 workflow 的 `deploy` 步驟，格式是 `https://<帳號>.github.io/<repo 名>/`。

## 本機執行

```bash
pip install numpy pandas yfinance
python scripts/fetch_daily.py --days 110   # 第一次約 10 分鐘
python scripts/build_site.py
cd site && python -m http.server 8000      # 打開 http://localhost:8000
```

## 回測（可重現）

`scripts/engine.py` 的 `actual_outcome()` 用真實後續走勢套同一套規則，可以做走動式回測。
2026-11 以 77 檔高流動性股票、2025/5～2026/10 共 5,236 筆的結果：

| 模擬說的停損機率 | 實際被停損比例 |
|---|---|
| 15% | 15% |
| 35% | 33% |
| 45% | 43% |
| 54% | 50% |

而「獲利機率」與實際獲利的相關係數只有 0.08；影片原版做法（60 天、完整外推近期走勢）說 80% 以上會獲利的 313 筆，實際只有 46% 獲利。

僅供研究，不是投資建議。
