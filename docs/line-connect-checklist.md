# LINE 連接檢查清單

## 1. 推上 GitHub

把整個專案推到 GitHub。

## 2. 部署到 Render

這個 repo 已附上：

- [render.yaml](C:\Users\90120\Desktop\codex\render.yaml)

在 Render 建立新 Web Service 時，直接選這個 repo 即可。

## 3. 設定 Render 環境變數

至少填：

- `OPENAI_API_KEY`
- `LINE_CHANNEL_SECRET`
- `SCHEDULE_DOCX_PATH`

參考範例：

- [.env.example](C:\Users\90120\Desktop\codex\.env.example)

## 4. 確認服務有啟動

部署完成後，先開：

```text
https://你的服務網址/health
```

正常應看到：

```json
{"ok": true}
```

## 5. 設定 LINE webhook

到 LINE Developers Console：

1. 打開你的 Messaging API channel
2. 找到 `Webhook URL`
3. 填入：

```text
https://你的服務網址/webhook/line
```

4. 啟用 `Use webhook`
5. 測試 webhook

## 6. 驗證收件匣

打開：

```text
https://你的服務網址/
```

之後把訊息轉貼到 Official Account，系統就應該能把它收進收件匣。

## 7. 目前你需要自己操作的部分

我不能直接替你登入：

- LINE Developers Console
- Render
- GitHub

所以最後的帳號設定和 webhook 綁定，還是要你自己按。
