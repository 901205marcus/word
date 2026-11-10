from __future__ import annotations

import hmac
import json
import mimetypes
import os
import threading
import time
from base64 import b64encode
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib import request

from .attachment_reader import AttachmentReader
from .docx_preview import DocxPreviewRenderer
from .image_text_composer import ImageTextComposer
from .map_guidance import MapGuidanceGenerator
from .models import InboxMessage, MessageStatus, ScheduleAction
from .parser import MessageParser
from .storage import InboxStore
from .word_editor import ScheduleWordEditor


class LineMVPService:
    APPLYING_MARKER = "[APPLYING]"

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
        self.image_text_composer = ImageTextComposer()
        self.overlay_pair_seconds = max(60, int(os.getenv("OVERLAY_PAIR_SECONDS", "600")))

        self.apply_retry_delay = max(3, int(os.getenv("LINE_APPLY_RETRY_DELAY", "5")))
        self.apply_retry_max_delay = max(
            self.apply_retry_delay,
            int(os.getenv("LINE_APPLY_RETRY_MAX_DELAY", "60")),
        )
        self.apply_max_retries = max(0, int(os.getenv("LINE_APPLY_MAX_RETRIES", "0")))
        self.line_push_retries = max(1, int(os.getenv("LINE_PUSH_RETRIES", "5")))

        self._job_lock = threading.Lock()
        self._active_jobs: set[str] = set()

    def verify_signature(self, body: bytes, signature: str) -> bool:
        secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
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
            source = event.get("source", {})
            sender_id = source.get("userId") or source.get("groupId") or "unknown"
            source_type = source.get("type", "unknown")
            message_type = message.get("type", "")
            raw_text = message.get("text", "").strip() if message_type == "text" else ""

            reply_text = self._handle_line_event(sender_id, source_type, message)
            if reply_text:
                reply_token = event.get("replyToken", "")
                success = False
                if reply_token:
                    success = self._reply_line_message(reply_token, reply_text)
                if not success:
                    success = self._push_text_message(sender_id, reply_text)
                replies.append({"sender_id": sender_id, "ok": "true" if success else "false"})

            latest = self.store.find_active_draft(sender_id) or self.store.find_message(sender_id)
            if latest and latest.raw_text == raw_text:
                created.append(latest)

        return {"created": created, "replies": replies}

    def create_manual_message(self, raw_text: str, sender_id: str = "manual") -> InboxMessage:
        item = self.store.add_message(sender_id=sender_id, source_type="manual", raw_text=raw_text.strip())
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        item.last_error = ""
        item.last_progress_at = self._now_iso()
        self.store.update_message(item)
        return item

    def append_attachment_text(self, message_id: str, filename: str, content: bytes) -> InboxMessage:
        item = self._require(message_id)
        extracted = self.attachment_reader.extract_text(filename, content)
        if not extracted:
            raise ValueError("附件沒有讀出可用文字。")
        item.raw_text = self._merge_text(item.raw_text, f"[附件：{filename}]\n{extracted}")
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        item.last_error = ""
        item.last_progress_at = self._now_iso()
        self.store.update_message(item)
        return item

    def create_message_from_attachment(self, filename: str, content: bytes) -> InboxMessage:
        extracted = self.attachment_reader.extract_text(filename, content)
        if not extracted:
            raise ValueError("附件沒有讀出可用文字。")
        return self.create_manual_message(f"[附件：{filename}]\n{extracted}", sender_id="upload")

    def refresh_parse(self, message_id: str) -> InboxMessage:
        item = self._require(message_id)
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        item.last_error = ""
        item.last_progress_at = self._now_iso()
        self.store.update_message(item)
        return item

    def mark_status(self, message_id: str, status: MessageStatus) -> InboxMessage:
        item = self._require(message_id)
        item.status = status
        item.last_progress_at = self._now_iso()
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
        resolved_path = self._resolve_docx_path(
            docx_path,
            uploaded_filename,
            uploaded_content,
            fallback_item=item,
        )
        editor = ScheduleWordEditor(resolved_path)
        actions = self._enrich_actions(item.actions)
        summary = editor.apply(actions, self.output_dir)
        if summary.output_path:
            summary.preview_paths = self.preview_renderer.render(summary.output_path, self.output_dir)

        item.status = MessageStatus.APPLIED
        item.processing = False
        item.output_path = summary.output_path or ""
        item.preview_paths = list(summary.preview_paths)
        item.error = json.dumps(asdict(summary), ensure_ascii=False)
        item.last_error = ""
        item.last_progress_at = self._now_iso()
        self.store.update_message(item)
        return item

    def list_messages(self) -> list[InboxMessage]:
        return self.store.list_messages()

    def recover_pending_jobs(self) -> int:
        recovered = 0
        for item in self.store.list_messages():
            if not item.processing:
                continue
            self._start_apply_job(item.sender_id, item.id)
            recovered += 1
        return recovered

    def _handle_line_event(self, sender_id: str, source_type: str, message: dict[str, Any]) -> str:
        message_type = message.get("type", "")
        if message_type == "text":
            raw_text = message.get("text", "").strip()
            if not raw_text:
                return ""
            return self._handle_line_text(sender_id, source_type, raw_text)
        if message_type in {"file", "image"}:
            return self._handle_line_attachment(sender_id, source_type, message)
        return "目前只支援文字、圖片與檔案訊息。"

    def _handle_line_text(self, sender_id: str, source_type: str, raw_text: str) -> str:
        command, code = self._parse_line_command(raw_text)
        if command:
            return self._execute_line_command(sender_id, command, code)

        item = self.store.find_active_draft(sender_id)
        if item is None:
            item = self.store.add_message(sender_id=sender_id, source_type=source_type, raw_text=raw_text)
        else:
            item.raw_text = self._merge_text(item.raw_text, raw_text)
            item.last_progress_at = self._now_iso()

        item.overlay_text = raw_text
        item.overlay_text_at = self._now_iso()
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        item.last_error = ""
        self.store.update_message(item)
        overlay_reply = self._try_create_overlay(sender_id, item)
        if overlay_reply:
            return overlay_reply
        return self._format_created_reply(item)

    def _handle_line_attachment(self, sender_id: str, source_type: str, message: dict[str, Any]) -> str:
        message_id = message.get("id", "")
        if not message_id:
            return "沒有收到可下載的 LINE 附件編號。"

        try:
            content, content_type = self._download_line_message_content(message_id)
        except Exception as exc:
            return f"附件下載失敗：{exc}"

        item = self.store.find_active_draft(sender_id)
        if item is None:
            item = self.store.add_message(sender_id=sender_id, source_type=source_type, raw_text="")

        filename = self._infer_line_filename(message, content_type)
        saved_path = self._save_upload(filename, content)
        item.uploaded_files.append(str(saved_path))
        item.last_progress_at = self._now_iso()

        if message.get("type") == "image":
            item.overlay_image_path = str(saved_path)
            item.overlay_image_at = self._now_iso()
            self.store.update_message(item)
            overlay_reply = self._try_create_overlay(sender_id, item)
            if overlay_reply:
                return overlay_reply

        if saved_path.suffix.lower() == ".docx":
            item.template_docx_path = str(saved_path)
            item.error = ""
            item.last_error = ""
            self.store.update_message(item)
            return self._format_template_reply(item, saved_path.name)

        try:
            extracted = self.attachment_reader.extract_text(saved_path.name, content)
        except Exception as exc:
            item.error = str(exc)
            item.last_error = str(exc)
            self.store.update_message(item)
            if message.get("type") == "image":
                return "圖片已收到。請在 10 分鐘內再傳送要放在照片中央的文字。"
            return f"附件已收到，但文字辨識失敗：{saved_path.name}\n錯誤：{exc}"

        item.raw_text = self._merge_text(item.raw_text, f"[附件：{saved_path.name}]\n{extracted}")
        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        item.last_error = ""
        self.store.update_message(item)
        return self._format_attachment_reply(item, saved_path.name)

    def _resolve_docx_path(
        self,
        docx_path: str | None,
        uploaded_filename: str | None,
        uploaded_content: bytes | None,
        fallback_item: InboxMessage | None = None,
    ) -> Path:
        if uploaded_filename and uploaded_content is not None:
            suffix = Path(uploaded_filename).suffix.lower()
            if suffix != ".docx":
                raise ValueError("只支援 .docx Word 檔。")
            return self._save_upload(uploaded_filename, uploaded_content)

        if docx_path and docx_path.strip():
            return Path(docx_path.strip())

        if fallback_item and fallback_item.template_docx_path.strip():
            return Path(fallback_item.template_docx_path.strip())

        env_path = os.getenv("SCHEDULE_DOCX_PATH", "").strip()
        if env_path:
            return Path(env_path)

        raise ValueError("找不到可套用的 Word 模板，請先上傳 .docx。")

    def _enrich_actions(self, actions: list[ScheduleAction]) -> list[ScheduleAction]:
        return [self.map_guidance.enrich_action(replace(action)) for action in actions]

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
        command = parts[0].strip().lower()
        code = parts[1].strip() if len(parts) > 1 else ""
        aliases = {
            "help": "help",
            "幫助": "help",
            "指令": "help",
            "查看": "view",
            "檢視": "view",
            "view": "view",
            "確認": "approve",
            "approve": "approve",
            "略過": "skip",
            "skip": "skip",
            "套用": "apply",
            "apply": "apply",
        }
        return aliases.get(command, ""), code

    def _execute_line_command(self, sender_id: str, command: str, code: str) -> str:
        if command == "help":
            return self._help_text()

        item = self.store.find_message(sender_id, code or None)
        if item is None:
            return "找不到對應案件，請確認案件代碼是否正確。"

        if command == "view":
            return self._format_item_summary(item)

        if command == "approve":
            item.status = MessageStatus.APPROVED
            item.error = ""
            item.last_error = ""
            item.last_progress_at = self._now_iso()
            self.store.update_message(item)
            template_hint = (
                f"已綁定 Word 模板：{Path(item.template_docx_path).name}"
                if item.template_docx_path
                else "尚未綁定 Word 模板，請先上傳 .docx。"
            )
            return (
                "系統狀態更新：已完成人工確認。\n"
                f"案件代碼：{self._message_code(item)}\n"
                f"{template_hint}\n"
                f"目前累積行程：{len(item.actions)} 筆\n"
                f"下一步：如果要正式寫入 Word，請回覆「套用 {self._message_code(item)}」。"
            )

        if command == "skip":
            item.status = MessageStatus.SKIPPED
            item.error = ""
            item.last_error = ""
            item.last_progress_at = self._now_iso()
            self.store.update_message(item)
            return (
                "系統狀態更新：此案件已標記略過。\n"
                f"案件代碼：{self._message_code(item)}\n"
                "原始資料仍會保留，之後如果要恢復處理，可以再傳新的內容。"
            )

        if command == "apply":
            if item.processing:
                return (
                    "系統通知：這個案件正在背景處理中。\n"
                    f"案件代碼：{self._message_code(item)}\n"
                    "目前會自動重試直到成功或達到系統限制，不需要重複輸入套用。"
                )

            if not item.template_docx_path.strip() and not os.getenv("SCHEDULE_DOCX_PATH", "").strip():
                return (
                    "系統通知：還沒有可套用的 Word 模板。\n"
                    f"案件代碼：{self._message_code(item)}\n"
                    "請先上傳 .docx，再重新輸入套用。"
                )

            item.processing = True
            item.apply_attempts = 0
            item.last_error = ""
            item.last_progress_at = self._now_iso()
            item.error = f"{self.APPLYING_MARKER} background apply started"
            self.store.update_message(item)
            self._start_apply_job(sender_id, item.id)
            return (
                "系統狀態更新：已收到套用指令，開始背景處理。\n"
                f"案件代碼：{self._message_code(item)}\n"
                "我會自動刪除今天以前的日期區塊，補齊從今天起往後兩個月的日期表格；"
                "若行程超出這段期間，也會額外補上那些日期。\n"
                "完成後會自動把 Word 與 JPG 預覽推送回 LINE；途中失敗也會自動重試。"
            )

        return self._help_text()

    def _start_apply_job(self, sender_id: str, message_id: str) -> None:
        with self._job_lock:
            if message_id in self._active_jobs:
                return
            self._active_jobs.add(message_id)

        worker = threading.Thread(
            target=self._run_apply_job,
            args=(sender_id, message_id),
            daemon=False,
            name=f"line-apply-{message_id[:8]}",
        )
        worker.start()

    def _run_apply_job(self, sender_id: str, message_id: str) -> None:
        try:
            attempt = 0
            while True:
                attempt += 1
                item = self._require(message_id)
                item.processing = True
                item.apply_attempts = attempt
                item.last_error = ""
                item.last_progress_at = self._now_iso()
                item.error = f"{self.APPLYING_MARKER} attempt {attempt}"
                self.store.update_message(item)

                try:
                    applied = self.apply_to_word(message_id)
                    applied.processing = False
                    applied.last_error = ""
                    applied.last_progress_at = self._now_iso()
                    self.store.update_message(applied)

                    if not self._push_apply_assets(sender_id, applied):
                        raise RuntimeError("LINE 推播失敗")
                    return
                except Exception as exc:
                    item = self._require(message_id)
                    item.status = MessageStatus.ERROR
                    item.processing = True
                    item.last_error = str(exc)
                    item.last_progress_at = self._now_iso()
                    item.error = str(exc)
                    self.store.update_message(item)

                    if self._should_stop_retry(attempt):
                        item.processing = False
                        item.last_progress_at = self._now_iso()
                        self.store.update_message(item)
                        self._push_text_message(
                            sender_id,
                            (
                                "系統通知：自動套用多次重試後仍失敗。\n"
                                f"案件代碼：{self._message_code(item)}\n"
                                f"最後錯誤：{exc}\n"
                                "案件資料已保留，你可以稍後再重新輸入套用。"
                            ),
                        )
                        return

                    wait_seconds = self._retry_delay_seconds(attempt)
                    self._push_text_message(
                        sender_id,
                        (
                            "系統通知：本次套用失敗，已排入自動重試。\n"
                            f"案件代碼：{self._message_code(item)}\n"
                            f"第 {attempt} 次失敗：{exc}\n"
                            f"預計 {wait_seconds} 秒後再次嘗試。"
                        ),
                    )
                    time.sleep(wait_seconds)
        finally:
            with self._job_lock:
                self._active_jobs.discard(message_id)

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
        return self._send_line_request(req, timeout=10, retries=2)

    def _push_text_message(self, to: str, text: str) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token or not to or not text:
            return False
        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/push",
            token,
            {
                "to": to,
                "messages": [{"type": "text", "text": text[:5000]}],
            },
        )
        return self._send_line_request(req, timeout=15, retries=self.line_push_retries)

    def _try_create_overlay(self, sender_id: str, item: InboxMessage) -> str:
        if not item.overlay_text.strip() or not item.overlay_image_path.strip():
            return ""

        try:
            text_time = datetime.fromisoformat(item.overlay_text_at)
            image_time = datetime.fromisoformat(item.overlay_image_at)
        except (TypeError, ValueError):
            return ""

        if abs((text_time - image_time).total_seconds()) > self.overlay_pair_seconds:
            if text_time > image_time:
                item.overlay_image_path = ""
                item.overlay_image_at = ""
            else:
                item.overlay_text = ""
                item.overlay_text_at = ""
            self.store.update_message(item)
            return ""

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_path = self.output_dir / f"line_overlay_{self._message_code(item).lower()}_{timestamp}.jpg"
        try:
            result_path = self.image_text_composer.compose(
                item.overlay_image_path,
                item.overlay_text,
                output_path,
            )
        except Exception as exc:
            item.last_error = f"圖片文字合成失敗：{exc}"
            item.last_progress_at = self._now_iso()
            self.store.update_message(item)
            return f"圖片文字合成失敗：{exc}"

        item.overlay_output_path = result_path
        item.last_progress_at = self._now_iso()
        if not self._push_image_message(sender_id, result_path):
            item.last_error = "圖片已合成，但 LINE 圖片回傳失敗。"
            self.store.update_message(item)
            return "圖片已合成，但 LINE 回傳失敗；系統會保留照片與文字，請再傳一次文字重試。"

        item.overlay_text = ""
        item.overlay_text_at = ""
        item.overlay_image_path = ""
        item.overlay_image_at = ""
        item.last_error = ""
        self.store.update_message(item)
        return "圖片文字合成完成，已把大字置中後回傳。"

    def _push_image_message(self, to: str, image_path: str) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        image_url = self._public_output_url(image_path, base_url)
        if not token or not to or not image_url:
            return False

        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/push",
            token,
            {
                "to": to,
                "messages": [
                    {
                        "type": "image",
                        "originalContentUrl": image_url,
                        "previewImageUrl": image_url,
                    }
                ],
            },
        )
        return self._send_line_request(req, timeout=20, retries=self.line_push_retries)

    def _push_apply_assets(self, to: str, item: InboxMessage) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not token or not to:
            return False

        text_lines = [
            "系統通知：Word 套用完成。",
            f"案件代碼：{self._message_code(item)}",
            f"Word 檔名：{Path(item.output_path).name if item.output_path else '未產出'}",
        ]
        output_url = self._public_output_url(item.output_path, base_url)
        if output_url:
            text_lines.append(f"Word 連結：{output_url}")

        messages: list[dict[str, str]] = [{"type": "text", "text": "\n".join(text_lines)[:5000]}]
        for preview_path in item.preview_paths[:5]:
            preview_url = self._public_output_url(preview_path, base_url)
            if not preview_url:
                continue
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
            {"to": to, "messages": messages},
        )
        return self._send_line_request(req, timeout=20, retries=self.line_push_retries)

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

    def _send_line_request(self, req: request.Request, timeout: int, retries: int) -> bool:
        for attempt in range(1, retries + 1):
            try:
                with request.urlopen(req, timeout=timeout) as response:
                    return 200 <= response.status < 300
            except Exception:
                if attempt >= retries:
                    return False
                time.sleep(min(10, attempt * 2))
        return False

    def _public_output_url(self, path: str, base_url: str) -> str:
        if not path or not base_url:
            return ""
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self.output_dir)
        except ValueError:
            return ""
        return f"{base_url}/files/{relative.as_posix()}"

    def _download_line_message_content(self, message_id: str) -> tuple[bytes, str]:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token:
            raise ValueError("缺少 LINE_CHANNEL_ACCESS_TOKEN。")
        req = request.Request(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with request.urlopen(req, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            return response.read(), content_type

    def _infer_line_filename(self, message: dict[str, Any], content_type: str) -> str:
        if message.get("type") == "file":
            file_name = message.get("fileName", "").strip()
            if file_name:
                return file_name
        suffix = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        if suffix == ".jpe":
            suffix = ".jpg"
        return f"line_{message.get('id', 'upload')}{suffix}"

    def _save_upload(self, filename: str, content: bytes) -> Path:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name or "line_upload.bin"
        target = self.upload_dir / safe_name
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = self.upload_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        target.write_bytes(content)
        return target

    @staticmethod
    def _merge_text(current: str, incoming: str) -> str:
        base = current.strip()
        extra = incoming.strip()
        if not base:
            return extra
        if not extra:
            return base
        return f"{base}\n{extra}"

    def _format_created_reply(self, item: InboxMessage) -> str:
        code = self._message_code(item)
        template_status = (
            f"已綁定 Word 模板：{Path(item.template_docx_path).name}"
            if item.template_docx_path
            else "尚未綁定 Word 模板"
        )
        if not item.actions:
            return (
                "系統通知：已收到內容，但暫時還沒拆出明確行程。\n"
                f"案件代碼：{code}\n"
                f"{template_status}\n"
                "建議下一步：\n"
                "1. 繼續補充行程文字\n"
                "2. 上傳 Word 模板 .docx\n"
                f"3. 若內容已齊，回覆「確認 {code}」"
            )

        first = item.actions[0]
        return (
            "系統通知：已收到內容並完成初步整理。\n"
            f"案件代碼：{code}\n"
            f"{template_status}\n"
            f"目前共解析：{len(item.actions)} 筆\n"
            f"首筆摘要：{first.date} {first.time or '時間待補'} {first.event or '行程待補'}\n"
            "可用操作：\n"
            f"1. 查看 {code}\n"
            f"2. 確認 {code}\n"
            f"3. 略過 {code}\n"
            f"4. 套用 {code}"
        )

    def _format_template_reply(self, item: InboxMessage, filename: str) -> str:
        code = self._message_code(item)
        return (
            "系統通知：已收到 Word 模板。\n"
            f"案件代碼：{code}\n"
            f"模板檔名：{filename}\n"
            f"目前累積行程：{len(item.actions)} 筆\n"
            "建議下一步：若內容已齊，回覆「確認 案件代碼」。"
        )

    def _format_attachment_reply(self, item: InboxMessage, filename: str) -> str:
        code = self._message_code(item)
        return (
            "系統通知：已收到附件並轉成文字。\n"
            f"案件代碼：{code}\n"
            f"附件檔名：{filename}\n"
            f"目前累積行程：{len(item.actions)} 筆\n"
            f"若要看摘要，可回覆「查看 {code}」。\n"
            "如果這是要加字的照片，請在 10 分鐘內傳送文字，系統會自動把大字放在照片正中央。"
        )

    def _format_item_summary(self, item: InboxMessage) -> str:
        status_labels = {
            MessageStatus.PENDING: "待確認",
            MessageStatus.APPROVED: "已確認",
            MessageStatus.SKIPPED: "已略過",
            MessageStatus.APPLIED: "已套用",
            MessageStatus.ERROR: "錯誤待重試",
        }
        code = self._message_code(item)
        lines = [
            f"案件代碼：{code}",
            f"目前狀態：{status_labels.get(item.status, item.status.value)}",
            f"Word 模板：{Path(item.template_docx_path).name if item.template_docx_path else '未上傳'}",
            f"處理中：{'是' if item.processing else '否'}",
            f"重試次數：{item.apply_attempts}",
        ]
        if item.actions:
            lines.append(f"解析結果：共 {len(item.actions)} 筆")
            for index, action in enumerate(item.actions[:5], start=1):
                lines.append(
                    f"{index}. {action.date} {action.time or '時間待補'} {action.event or '行程待補'}"
                )
        else:
            lines.append("解析結果：目前尚未拆出可寫入的行程。")
        if item.last_error:
            lines.append(f"最後錯誤：{item.last_error[:160]}")
        if item.uploaded_files:
            lines.append(f"附件數量：{len(item.uploaded_files)}")
        lines.append("可用操作：")
        lines.append(f"1. 確認 {code}")
        lines.append(f"2. 略過 {code}")
        lines.append(f"3. 套用 {code}")
        return "\n".join(lines)

    def _message_code(self, item: InboxMessage) -> str:
        return item.id.split("-")[0].upper()

    def _help_text(self) -> str:
        return (
            "可用指令：\n"
            "1. 查看 案件代碼\n"
            "2. 確認 案件代碼\n"
            "3. 略過 案件代碼\n"
            "4. 套用 案件代碼\n"
            "也可以直接傳新的行程文字、圖片、PDF、Word 模板給我。\n"
            "圖片加字：照片與文字在 10 分鐘內先後傳送，會自動合成並回傳。"
        )

    def _retry_delay_seconds(self, attempt: int) -> int:
        return min(self.apply_retry_max_delay, self.apply_retry_delay * attempt)

    def _should_stop_retry(self, attempt: int) -> bool:
        return self.apply_max_retries > 0 and attempt >= self.apply_max_retries

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")
