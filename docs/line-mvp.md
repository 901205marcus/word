# LINE Official Account MVP

這版 MVP 的目標很單純：

1. 你把老闆訊息轉貼到 LINE Official Account
2. webhook 收到訊息
3. 系統先解析
4. 你在收件匣確認
5. 確認後才輸出 Word

## 為什麼這條路比較適合

- 不碰私人 LINE 讀取問題
- 不需要抓既有聊天紀錄
- 可以保留人工確認
- 比 Colab 更接近正式工作流程

## 目前包含的功能

- `POST /webhook/line`
  - 接 LINE Messaging API webhook
  - 驗證 `x-line-signature`
  - 只處理文字訊息

- `GET /`
  - 顯示收件匣
  - 預覽解析結果
  - 重新解析
  - 更新狀態
  - 確認後輸出 Word

- `GET /health`
  - 健康檢查

## 主要檔案

- [app.py](C:\Users\90120\Desktop\codex\src\line_mvp\app.py)
- [service.py](C:\Users\90120\Desktop\codex\src\line_mvp\service.py)
- [parser.py](C:\Users\90120\Desktop\codex\src\line_mvp\parser.py)
- [storage.py](C:\Users\90120\Desktop\codex\src\line_mvp\storage.py)
- [word_editor.py](C:\Users\90120\Desktop\codex\src\line_mvp\word_editor.py)
- [line_review.html](C:\Users\90120\Desktop\codex\templates\line_review.html)

## 需要的環境變數

- `LINE_CHANNEL_SECRET`
- `OPENAI_API_KEY`
- `SCHEDULE_DOCX_PATH`

範例請看：

- [.env.example](C:\Users\90120\Desktop\codex\.env.example)

## 本機測試

```bash
python -m pip install -r requirements.txt
python line_mvp_run.py
```

打開：

```text
http://127.0.0.1:8000
```

## 正式部署建議

建議平台：

- Render
- Railway
- Google Cloud Run
- VPS

正式 webhook 需要固定的 HTTPS 網址，所以不要把 Colab 當正式 webhook 主機。

## 目前限制

- Word 輸出仍以既有格式表格為前提
- 收件匣介面是 MVP，重點在流程與安全確認
- 收件資料預設存於本機 JSON，不是資料庫
