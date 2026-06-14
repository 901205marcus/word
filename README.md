# Word Schedule Assistant

這個專案目前有兩條主線：

1. `Colab 原型`
   - 手動上傳 Word
   - 手動貼上訊息
   - 解析後更新行程表

2. `LINE Official Account MVP`
   - 你把老闆訊息轉貼到 LINE Official Account
   - LINE webhook 收進系統
   - 先進人工確認收件匣
   - 確認後才輸出新的 Word

目前如果你要往正式上線走，建議以 `LINE Official Account MVP` 為主。

## 目錄

```text
.
├─ colab_runner.py
├─ line_mvp_run.py
├─ requirements.txt
├─ render.yaml
├─ docs/
│  ├─ line-connect-checklist.md
│  └─ line-mvp.md
├─ notebooks/
│  └─ colab_quickstart.ipynb
├─ src/
│  ├─ line_mvp/
│  └─ schedule_ai/
└─ templates/
   └─ line_review.html
```

## 推薦使用路線

### 1. LINE Official Account MVP

主要檔案：

- [line_mvp_run.py](C:\Users\90120\Desktop\codex\line_mvp_run.py)
- [app.py](C:\Users\90120\Desktop\codex\src\line_mvp\app.py)
- [service.py](C:\Users\90120\Desktop\codex\src\line_mvp\service.py)
- [parser.py](C:\Users\90120\Desktop\codex\src\line_mvp\parser.py)
- [word_editor.py](C:\Users\90120\Desktop\codex\src\line_mvp\word_editor.py)
- [line-mvp.md](C:\Users\90120\Desktop\codex\docs\line-mvp.md)
- [line-connect-checklist.md](C:\Users\90120\Desktop\codex\docs\line-connect-checklist.md)

用途：

- 接收 LINE webhook
- 存成待確認收件匣
- 解析短句型與正式通知型訊息
- 人工確認後輸出新的 Word

本機啟動：

```bash
python -m pip install -r requirements.txt
python line_mvp_run.py
```

然後打開：

```text
http://127.0.0.1:8000
```

### 2. Colab 原型

如果你只是想先測 Word 編修邏輯，還可以用：

- [colab_runner.py](C:\Users\90120\Desktop\codex\colab_runner.py)
- [colab_quickstart.ipynb](C:\Users\90120\Desktop\codex\notebooks\colab_quickstart.ipynb)

## 環境變數

請參考：

- [.env.example](C:\Users\90120\Desktop\codex\.env.example)

至少會用到：

- `OPENAI_API_KEY`
- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `PUBLIC_BASE_URL`
- `SCHEDULE_DOCX_PATH`

## 部署

這個 repo 已經附上 Render 設定：

- [render.yaml](C:\Users\90120\Desktop\codex\render.yaml)

部署後，把 LINE Developers Console 的 webhook URL 指向：

```text
https://你的服務網址/webhook/line
```

目前也支援直接在 LINE 內操作：

- 直接傳行程內容，系統會回一個代碼
- `查看`
- `查看 代碼`
- `確認 代碼`
- `略過 代碼`
- `套用 代碼`

如果你要讓 LINE 顯示的是我們自己的回覆，而不是官方預設那句「無法個別回覆」，還要確認：

- Render 已設定 `LINE_CHANNEL_ACCESS_TOKEN`
- Render 已設定 `PUBLIC_BASE_URL`
- LINE Official Account Manager 內把預設自動回應關掉或改掉

如果要在 LINE 內直接收到輸出結果，建議流程是：

- 在 LINE 輸入 `套用 代碼`
- 系統寫入 Word
- 產生 JPG 預覽
- LINE 主動推送 `Word 下載連結 + JPG 預覽圖`

健康檢查：

```text
https://你的服務網址/health
```

## 注意事項

- `outputs/`、`data/`、`__pycache__/` 不應上傳 GitHub
- 正式接 LINE 需要穩定 HTTPS 網址，Colab 不適合當正式 webhook 主機
- 目前 MVP 強調流程可控，不追求全自動
