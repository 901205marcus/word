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

- 預設展開的商家式 Rich Menu 提供「行程管理、圖片加字、加字狀態、使用說明」四個按鈕
- 加好友時會主動顯示功能卡片；之後可輸入 `選單` 再次叫出
- 直接傳行程內容，系統會回一個代碼
- 可以連續補傳多則 LINE 行程內容
- 可以直接上傳本次要套用的 `.docx` Word 模板
- 圖片或其他附件會先轉文字，再併入同一筆整理
- 輸入 `加字模式` 後，先批次傳文字、再批次傳照片，系統會按照順序一對一合成並回傳
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

- 先把本次所有行程內容傳到 LINE
- 再把本次要修改的 `.docx` 模板傳到 LINE
- 確認整理內容無誤後，回覆 `確認 代碼`
- 最後回覆 `套用 代碼`
- 系統寫入 Word
- 產生 JPG 預覽
- LINE 主動推送 `Word 下載連結 + JPG 預覽圖`

圖片加字批次流程：

- 先輸入 `加字模式`，此後的文字不會送進行程解析器
- 先一次傳完所有文字，每則 LINE 訊息算一段
- 再依照文字順序一次傳完所有照片
- 第 1 段文字配第 1 張照片、第 2 段文字配第 2 張照片，依此類推
- 文字會依 LINE 的實際傳送時間與訊息編號排序，不受 webhook 到達先後影響
- 照片與文字必須由同一位 LINE 使用者在 30 分鐘內傳送
- 每張照片與每段文字只使用一次，依 FIFO 順序嚴格一對一配對
- 照片不能早於文字；沒有待配文字時傳來的照片不會加入佇列
- 系統會自動換行，在照片上方中央加入白字與黑色描邊，不使用黑色底幕
- 完成後會直接把合成 JPG 推送回 LINE
- 輸入 `加字狀態` 可查看待配對數量；輸入 `結束加字` 返回行程模式
- 輸入 `取消加字` 會清空未配對文字並返回行程模式
- 配對時間可用 `OVERLAY_PAIR_SECONDS` 調整，預設為 `1800` 秒
- 專案已內建 Noto Sans TC 與 Noto Sans SC，會依文字自動支援繁體、簡體及兩者混用；如需改用其他字型，可用 `OVERLAY_FONT_PATH` 指定 `.ttf` 或 `.ttc`

功能選單說明：

- LINE 不會在每次開啟聊天室時傳送 webhook，因此機器人無法偵測真正的「每次登入」
- 專案會透過 Messaging API 自動設定 `selected: true` 的預設 Rich Menu，重新開啟聊天室時會預設展開
- 若選單被手動收合，可點聊天列的 `選擇功能`，或傳送 `選單`
- 可用 `LINE_RICH_MENU_ENABLED=false` 停用自動建立 Rich Menu

健康檢查：

```text
https://你的服務網址/health
```

## 注意事項

- `outputs/`、`data/`、`__pycache__/` 不應上傳 GitHub
- 正式接 LINE 需要穩定 HTTPS 網址，Colab 不適合當正式 webhook 主機
- 目前 MVP 強調流程可控，不追求全自動
