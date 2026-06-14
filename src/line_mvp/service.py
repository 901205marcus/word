from __future__ import annotations

import hmac
import json
import os
from base64 import b64encode
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib import error, request

from .attachment_reader import AttachmentReader
from .docx_preview import DocxPreviewRenderer
from .map_guidance import MapGuidanceGenerator
from .models import InboxMessage, MessageStatus, ScheduleAction
from .parser import MessageParser
from .storage import InboxStore
from .word_editor import ScheduleWordEditor


class LineMVPService:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "data" / "line_mvp"
        self.output_dir = self.base_dir / "outputs"
        self.upload_dir = self.data_dir / "uploads"
        self.store = InboxStore(self.data_dir / "inbox.json")
        self.parser = MessageParser(openai_enabled=True)
        self.attachment_reader = AttachmentReader(base_dir)
        self.map_guidance = MapGuidanceGenerator()
        self.preview_renderer = DocxPreviewRenderer()

    def verify_signature(self, body: bytes, signature: str) -> bool:
        secret = os.getenv("LINE_CHANNEL_SECRET", "")
        if not secret:
            return True
        digest = hmac.new(secret.encode("utf-8"), body, sha256).digest()
        expected = b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    def ingest_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        created: list[InboxMessage] = []
        replies: list[dict[str, str]] = []
        for event in payload.get("events", []):
            if event.get("type") != "message":
                continue
            message = event.get("message", {})
            if message.get("type") != "text":
                continue
            source = event.get("source", {})
            raw_text = message.get("text", "").strip()
            if not raw_text:
                continue
            sender_id = source.get("userId") or source.get("groupId") or "unknown"
            reply_text = self._handle_line_text(sender_id, source.get("type", "unknown"), raw_text)
            reply_token = event.get("replyToken", "")
            if reply_token and reply_text:
                success = self._reply_line_message(reply_token, reply_text)
                replies.append({"sender_id": sender_id, "ok": "true" if success else "false"})
            latest = self.store.find_message(sender_id)
            if latest and latest.raw_text == raw_text:
                created.append(latest)
        return {"created": created, "replies": replies}

    def create_manual_message(self, raw_text: str, sender_id: str = "manual") -> InboxMessage:
        item = self.store.add_message(sender_id=sender_id, source_type="manual", raw_text=raw_text.strip())
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        self.store.update_message(item)
        return item

    def append_attachment_text(self, message_id: str, filename: str, content: bytes) -> InboxMessage:
        item = self._require(message_id)
        extracted = self.attachment_reader.extract_text(filename, content)
        if not extracted:
            raise ValueError("附件沒有擷取到可用文字。")
        merged_text = f"{item.raw_text}\n\n[附件：{filename}]\n{extracted}".strip()
        item.raw_text = merged_text
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        self.store.update_message(item)
        return item

    def create_message_from_attachment(self, filename: str, content: bytes) -> InboxMessage:
        extracted = self.attachment_reader.extract_text(filename, content)
        if not extracted:
            raise ValueError("附件沒有擷取到可用文字。")
        return self.create_manual_message(f"[附件：{filename}]\n{extracted}", sender_id="upload")

    def refresh_parse(self, message_id: str) -> InboxMessage:
        item = self._require(message_id)
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        self.store.update_message(item)
        return item

    def mark_status(self, message_id: str, status: MessageStatus) -> InboxMessage:
        item = self._require(message_id)
        item.status = status
        self.store.update_message(item)
        return item

    def apply_to_word(
        self,
        message_id: str,
        docx_path: str | None = None,
        uploaded_filename: str | None = None,
        uploaded_content: bytes | None = None,
    ) -> InboxMessage:
        item = self._require(message_id)
        resolved_path = self._resolve_docx_path(docx_path, uploaded_filename, uploaded_content)
        editor = ScheduleWordEditor(resolved_path)
        actions = self._enrich_actions(item.actions)
        summary = editor.apply(actions, self.output_dir)
        if summary.output_path:
            summary.preview_paths = self.preview_renderer.render(summary.output_path, self.output_dir)
        item.status = MessageStatus.APPLIED
        item.output_path = summary.output_path or ""
        item.preview_paths = list(summary.preview_paths)
        item.error = json.dumps(asdict(summary), ensure_ascii=False)
        self.store.update_message(item)
        return item

    def list_messages(self) -> list[InboxMessage]:
        return self.store.list_messages()

    def _handle_line_text(self, sender_id: str, source_type: str, raw_text: str) -> str:
        command, code = self._parse_line_command(raw_text)
        if command:
            return self._execute_line_command(sender_id, command, code)

        item = self.store.add_message(
            sender_id=sender_id,
            source_type=source_type,
            raw_text=raw_text,
        )
        item.actions = self._enrich_actions(self.parser.parse(raw_text))
        self.store.update_message(item)
        return self._format_created_reply(item)

    def _resolve_docx_path(
        self,
        docx_path: str | None,
        uploaded_filename: str | None,
        uploaded_content: bytes | None,
    ) -> Path:
        if uploaded_filename and uploaded_content:
            suffix = Path(uploaded_filename).suffix.lower()
            if suffix != ".docx":
                raise ValueError("套用 Word 時只接受 .docx 檔。")
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            target = self.upload_dir / uploaded_filename
            target.write_bytes(uploaded_content)
            return target

        if docx_path and docx_path.strip():
            return Path(docx_path.strip())

        env_path = os.getenv("SCHEDULE_DOCX_PATH", "").strip()
        if env_path:
            return Path(env_path)

        raise ValueError("請輸入 Word 路徑，或直接上傳 .docx 檔。")

    def _enrich_actions(self, actions: list[ScheduleAction]) -> list[ScheduleAction]:
        enriched: list[ScheduleAction] = []
        for action in actions:
            enriched.append(self.map_guidance.enrich_action(replace(action)))
        return enriched

    def _require(self, message_id: str) -> InboxMessage:
        item = self.store.get_message(message_id)
        if item is None:
            raise KeyError(message_id)
        return item

    def _parse_line_command(self, text: str) -> tuple[str, str]:
        normalized = " ".join(text.replace("\n", " ").split())
        if not normalized:
            return "", ""
        parts = normalized.split(" ", 1)
        command = parts[0].strip()
        code = parts[1].strip() if len(parts) > 1 else ""
        aliases = {
            "幫助": "help",
            "help": "help",
            "HELP": "help",
            "指令": "help",
            "查看": "view",
            "查詢": "view",
            "狀態": "view",
            "確認": "approve",
            "核准": "approve",
            "略過": "skip",
            "跳過": "skip",
            "套用": "apply",
            "寫入": "apply",
            "輸出": "apply",
        }
        return aliases.get(command, ""), code

    def _execute_line_command(self, sender_id: str, command: str, code: str) -> str:
        if command == "help":
            return self._help_text()

        item = self.store.find_message(sender_id, code or None)
        if item is None:
            target = f"代碼 {code}" if code else "最近一筆訊息"
            return f"目前找不到{target}。請先傳送行程內容，或輸入「查看」確認最近一筆資料。"

        if command == "view":
            return self._format_item_summary(item)

        if command == "approve":
            item.status = MessageStatus.APPROVED
            self.store.update_message(item)
            return (
                f"已確認行程，代碼 {self._message_code(item)}。\n"
                f"若要正式寫入 Word，請回覆「套用 {self._message_code(item)}」。"
            )

        if command == "skip":
            item.status = MessageStatus.SKIPPED
            self.store.update_message(item)
            return f"這筆行程已先略過，代碼 {self._message_code(item)}。"

        if command == "apply":
            try:
                applied = self.apply_to_word(item.id)
            except Exception as exc:
                item.status = MessageStatus.ERROR
                item.error = str(exc)
                self.store.update_message(item)
                return (
                    f"寫入 Word 失敗：{exc}\n"
                    "請確認 Render 已設定 SCHEDULE_DOCX_PATH，且伺服器可以讀取該 .docx 檔案。"
                )
            self._push_apply_assets(sender_id, applied)
            return self._format_apply_reply(applied)

        return self._help_text()

    def _reply_line_message(self, reply_token: str, text: str) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token or not reply_token or not text:
            return False
        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/reply",
            token,
            {
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text[:5000]}],
            },
        )
        try:
            with request.urlopen(req, timeout=10) as response:
                return 200 <= response.status < 300
        except error.URLError:
            return False

    def _push_apply_assets(self, to: str, item: InboxMessage) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not token or not to:
            return False

        messages: list[dict[str, str]] = []
        text_lines = [
            f"已完成寫入，案件代碼 {self._message_code(item)}。",
            f"Word 檔：{Path(item.output_path).name if item.output_path else '未輸出'}",
        ]
        output_url = self._public_output_url(item.output_path, base_url)
        if output_url:
            text_lines.append(f"Word 下載：{output_url}")
        messages.append({"type": "text", "text": "\n".join(text_lines)[:5000]})

        preview_url = self._public_output_url(item.preview_paths[0], base_url) if item.preview_paths else ""
        if preview_url:
            messages.append(
                {
                    "type": "image",
                    "originalContentUrl": preview_url,
                    "previewImageUrl": preview_url,
                }
            )

        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/push",
            token,
            {
                "to": to,
                "messages": messages,
            },
        )
        try:
            with request.urlopen(req, timeout=15) as response:
                return 200 <= response.status < 300
        except error.URLError:
            return False

    def _make_line_request(self, url: str, token: str, payload_obj: dict[str, Any]) -> request.Request:
        payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
        return request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )

    def _public_output_url(self, path: str, base_url: str) -> str:
        if not path or not base_url:
            return ""
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self.output_dir)
        except ValueError:
            return ""
        return f"{base_url}/files/{relative.as_posix()}"

    def _format_created_reply(self, item: InboxMessage) -> str:
        code = self._message_code(item)
        if not item.actions:
            return (
                f"已收到訊息，案件代碼 {code}。\n"
                "目前尚未成功辨識出可用行程。\n"
                f"請回覆「查看 {code}」檢查內容，或補充更完整的日期、時間與地點。"
            )
        first = item.actions[0]
        return (
            f"已收到並完成初步整理，案件代碼 {code}。\n"
            f"本次共解析 {len(item.actions)} 筆行程。\n"
            f"首筆內容：{first.date} {first.time or '時間待補'} {first.event or '行程待補'}\n"
            f"可直接回覆：查看 {code}、確認 {code}、略過 {code}、套用 {code}"
        )

    def _format_item_summary(self, item: InboxMessage) -> str:
        code = self._message_code(item)
        status_labels = {
            MessageStatus.PENDING: "待確認",
            MessageStatus.APPROVED: "已確認",
            MessageStatus.SKIPPED: "已略過",
            MessageStatus.APPLIED: "已寫入 Word",
            MessageStatus.ERROR: "處理失敗",
        }
        lines = [
            f"案件代碼：{code}",
            f"目前狀態：{status_labels.get(item.status, item.status.value)}",
            f"原始內容：{item.raw_text[:120]}",
        ]
        if item.actions:
            lines.append(f"解析結果：共 {len(item.actions)} 筆")
            for index, action in enumerate(item.actions[:3], start=1):
                lines.append(
                    f"{index}. {action.date} {action.time or '時間待補'} {action.event or '行程待補'}"
                )
        else:
            lines.append("目前尚未解析出可直接套用的行程。")
        if item.error:
            lines.append(f"系統備註：{item.error[:120]}")
        return "\n".join(lines)

    def _format_apply_reply(self, item: InboxMessage) -> str:
        code = self._message_code(item)
        path = Path(item.output_path).name if item.output_path else "未輸出"
        preview = Path(item.preview_paths[0]).name if item.preview_paths else "未產生"
        return (
            f"已完成寫入，案件代碼 {code}。\n"
            f"Word 檔：{path}\n"
            f"預覽圖：{preview}\n"
            "建議再回到管理頁確認版面與排序是否正確。"
        )

    def _message_code(self, item: InboxMessage) -> str:
        return item.id.split("-")[0].upper()

    def _help_text(self) -> str:
        return (
            "可用指令如下：\n"
            "查看 或 查看 代碼\n"
            "確認 或 確認 代碼\n"
            "略過 或 略過 代碼\n"
            "套用 或 套用 代碼\n"
            "你也可以直接傳送行程內容，系統會先整理後回覆案件代碼。"
        )
