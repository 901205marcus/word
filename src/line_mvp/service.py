from __future__ import annotations

import hmac
import json
import logging
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
from .models import InboxMessage, MessageStatus, OverlaySession, ScheduleAction
from .overlay_store import OverlaySessionStore
from .parser import MessageParser
from .rich_menu import LineRichMenuManager
from .storage import InboxStore
from .word_editor import ScheduleWordEditor


logger = logging.getLogger(__name__)


class LineMVPService:
    APPLYING_MARKER = "[APPLYING]"
    FUNCTION_MENU_SENTINEL = "__LINE_FUNCTION_MENU__"
    OVERLAY_COMPLETE_SENTINEL = "__LINE_OVERLAY_COMPLETE__\n"
    OVERLAY_COMMANDS = {
        "overlay_start",
        "overlay_continue",
        "overlay_end",
        "overlay_cancel",
        "overlay_status",
        "overlay_retry",
        "schedule_mode",
    }

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "data" / "line_mvp"
        self.output_dir = self.base_dir / "outputs"
        self.upload_dir = self.data_dir / "uploads"
        self.store = InboxStore(self.data_dir / "inbox.json")
        self.overlay_store = OverlaySessionStore(self.data_dir / "overlay_sessions.json")
        self.parser = MessageParser(openai_enabled=True)
        self.attachment_reader = AttachmentReader(base_dir)
        self.map_guidance = MapGuidanceGenerator()
        self.preview_renderer = DocxPreviewRenderer()
        self.image_text_composer = ImageTextComposer()
        self.rich_menu_manager = LineRichMenuManager(self.base_dir)
        self.overlay_pair_seconds = max(60, int(os.getenv("OVERLAY_PAIR_SECONDS", "1800")))
        self.apply_retry_delay = max(3, int(os.getenv("LINE_APPLY_RETRY_DELAY", "5")))
        self.apply_retry_max_delay = max(
            self.apply_retry_delay,
            int(os.getenv("LINE_APPLY_RETRY_MAX_DELAY", "60")),
        )
        self.apply_max_retries = max(0, int(os.getenv("LINE_APPLY_MAX_RETRIES", "0")))
        self.line_push_retries = max(1, int(os.getenv("LINE_PUSH_RETRIES", "5")))
        self._job_lock = threading.Lock()
        self._active_jobs: set[str] = set()
        self._sender_locks_guard = threading.Lock()
        self._sender_locks: dict[str, threading.RLock] = {}
        # Kept as empty compatibility markers; image overlays no longer start workers.
        self._active_overlay_jobs: set[str] = set()
        self._active_overlay_retry_jobs: set[str] = set()

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
            event_type = event.get("type")
            if event_type == "follow":
                reply_token = event.get("replyToken", "")
                source = event.get("source", {})
                sender_id = source.get("userId") or "unknown"
                success = self._reply_line_function_menu(reply_token)
                if not success:
                    success = self._push_line_function_menu(sender_id)
                replies.append({"sender_id": sender_id, "ok": "true" if success else "false"})
                continue

            if event_type != "message":
                continue

            message = event.get("message", {})
            source = event.get("source", {})
            sender_id = source.get("userId") or source.get("groupId") or "unknown"
            source_type = source.get("type", "unknown")
            message_type = message.get("type", "")
            raw_text = message.get("text", "").strip() if message_type == "text" else ""

            reply_text = self._handle_line_event(
                sender_id,
                source_type,
                message,
                event_timestamp=event.get("timestamp"),
                event_id=str(event.get("webhookEventId", "")),
            )
            if reply_text:
                reply_token = event.get("replyToken", "")
                success = False
                if reply_text == self.FUNCTION_MENU_SENTINEL:
                    if reply_token:
                        success = self._reply_line_function_menu(reply_token)
                    if not success:
                        success = self._push_line_function_menu(sender_id)
                elif reply_text.startswith(self.OVERLAY_COMPLETE_SENTINEL):
                    choice_text = reply_text.removeprefix(self.OVERLAY_COMPLETE_SENTINEL)
                    if reply_token:
                        success = self._reply_line_overlay_choice(reply_token, choice_text)
                    if not success:
                        success = self._push_line_overlay_choice(sender_id, choice_text)
                else:
                    if reply_token:
                        success = self._reply_line_message(reply_token, reply_text)
                    if not success:
                        success = self._push_text_message(sender_id, reply_text)
                replies.append({"sender_id": sender_id, "ok": "true" if success else "false"})

            latest = self.store.find_active_draft(sender_id) or self.store.find_message(sender_id)
            if latest and latest.raw_text == raw_text:
                created.append(latest)

        return {"created": created, "replies": replies}

    def ensure_line_rich_menu(self) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        enabled = os.getenv("LINE_RICH_MENU_ENABLED", "true").strip().lower()
        if not token or enabled in {"0", "false", "no"}:
            return False
        for attempt in range(1, 6):
            if self.rich_menu_manager.ensure_default():
                return True
            if attempt < 5:
                time.sleep(min(30, attempt * 5))
        return False

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

    def recover_overlay_jobs(self) -> int:
        return 0

    def _handle_line_event(
        self,
        sender_id: str,
        source_type: str,
        message: dict[str, Any],
        event_timestamp: int | None = None,
        event_id: str = "",
    ) -> str:
        message_type = message.get("type", "")
        if message_type == "text":
            raw_text = message.get("text", "").strip()
            if not raw_text:
                return ""
            return self._handle_line_text(
                sender_id,
                source_type,
                raw_text,
                event_timestamp=event_timestamp,
                message_id=str(message.get("id", "")),
                event_id=event_id,
            )
        if message_type in {"file", "image"}:
            return self._handle_line_attachment(
                sender_id,
                source_type,
                message,
                event_timestamp=event_timestamp,
                event_id=event_id,
            )
        return "目前只支援文字、圖片與檔案訊息。"

    def _handle_line_text(
        self,
        sender_id: str,
        source_type: str,
        raw_text: str,
        event_timestamp: int | None = None,
        message_id: str = "",
        event_id: str = "",
    ) -> str:
        with self._sender_lock(sender_id):
            return self._handle_line_text_locked(
                sender_id,
                source_type,
                raw_text,
                event_timestamp=event_timestamp,
                message_id=message_id,
                event_id=event_id,
            )

    def _handle_line_text_locked(
        self,
        sender_id: str,
        source_type: str,
        raw_text: str,
        event_timestamp: int | None = None,
        message_id: str = "",
        event_id: str = "",
    ) -> str:
        command, code = self._parse_line_command(raw_text)
        if command in self.OVERLAY_COMMANDS or command == "menu":
            return self._execute_line_command(sender_id, command, code)

        overlay_session = self.overlay_store.get(sender_id)
        if overlay_session and overlay_session.active:
            self._prune_overlay_texts(overlay_session)
            if event_id and event_id in overlay_session.seen_event_ids:
                return "這段加字文字已經收到，不會重複加入配對佇列。"
            overlay_session.text_queue.append(
                {
                    "text": raw_text,
                    "at": self._now_iso(),
                    "event_timestamp": str(event_timestamp or ""),
                    "message_id": message_id,
                    "event_id": event_id,
                }
            )
            if event_id:
                overlay_session.seen_event_ids.append(event_id)
                overlay_session.seen_event_ids = overlay_session.seen_event_ids[-500:]
            self._sort_overlay_text_queue(overlay_session)
            overlay_session.updated_at = self._now_iso()
            overlay_session.last_error = ""
            self.overlay_store.update(overlay_session)
            return (
                f"加字文字已加入：第 {len(overlay_session.text_queue)} 段待配對，"
                f"目前共 {len(overlay_session.text_queue)} 段。\n"
                "系統會依 LINE 實際傳送時間排序，不使用 webhook 到達順序。\n"
                "可以繼續傳文字；全部文字傳完後，再按照相同順序傳送照片。\n"
                "這段文字不會加入行程。"
            )

        if command:
            return self._execute_line_command(sender_id, command, code)

        item = self.store.find_active_draft(sender_id)
        if item is None:
            item = self.store.add_message(sender_id=sender_id, source_type=source_type, raw_text=raw_text)
        else:
            item.raw_text = self._merge_text(item.raw_text, raw_text)
            item.last_progress_at = self._now_iso()

        item.actions = self._enrich_actions(self.parser.parse(item.raw_text))
        item.error = ""
        item.last_error = ""
        self.store.update_message(item)
        return self._format_created_reply(item)

    def _handle_line_attachment(
        self,
        sender_id: str,
        source_type: str,
        message: dict[str, Any],
        event_timestamp: int | None = None,
        event_id: str = "",
    ) -> str:
        with self._sender_lock(sender_id):
            return self._handle_line_attachment_locked(
                sender_id,
                source_type,
                message,
                event_timestamp=event_timestamp,
                event_id=event_id,
            )

    def _download_and_queue_overlay_image(
        self,
        sender_id: str,
        message: dict[str, Any],
        event_timestamp: int | None = None,
        event_id: str = "",
    ) -> str:
        message_id = str(message.get("id", ""))
        download_started = time.perf_counter()
        try:
            content, content_type = self._download_line_message_content(message_id)
        except Exception as exc:
            return f"照片下載失敗，文字仍保留等待配對：{exc}"

        filename = self._infer_line_filename(message, content_type)
        saved_path = self._save_upload(filename, content)
        logger.info(
            "LINE overlay image downloaded sender=%s seconds=%.3f bytes=%d",
            sha256(sender_id.encode("utf-8")).hexdigest()[:8],
            time.perf_counter() - download_started,
            len(content),
        )

        with self._sender_lock(sender_id):
            overlay_session = self.overlay_store.get(sender_id)
            if not overlay_session or not overlay_session.active:
                return "照片下載完成，但目前已切換到行程模式；這張照片沒有加入貼字佇列。"
            rejection = self._overlay_image_rejection(overlay_session, event_id)
            if rejection:
                return rejection
            return self._queue_overlay_image(
                sender_id,
                overlay_session,
                saved_path,
                event_timestamp=event_timestamp,
                message_id=message_id,
                event_id=event_id,
            )

    def _overlay_image_rejection(
        self,
        session: OverlaySession,
        event_id: str,
    ) -> str:
        self._prune_overlay_texts(session)
        if not session.text_queue:
            self.overlay_store.update(session)
            return (
                "目前沒有等待配對的文字，因此這張照片沒有加入佇列。\n"
                "請先傳送一段或多段文字，再依相同順序重新傳送照片。"
            )
        if event_id and event_id in session.seen_image_event_ids:
            return "這張照片已經收到，不會重複加入處理佇列。"
        if len(session.image_queue) >= len(session.text_queue):
            return (
                "本批照片已收齊，這張額外照片沒有加入佇列。\n"
                "請等待本批完成，或切換回行程模式後重新開始。"
            )
        return ""

    def _handle_line_attachment_locked(
        self,
        sender_id: str,
        source_type: str,
        message: dict[str, Any],
        event_timestamp: int | None = None,
        event_id: str = "",
    ) -> str:
        message_id = message.get("id", "")
        if not message_id:
            return "沒有收到可下載的 LINE 附件編號。"

        overlay_session = self.overlay_store.get(sender_id)
        if overlay_session and overlay_session.active:
            if message.get("type") != "image":
                return "目前是加字模式，只接受照片。請輸入「結束加字」後再傳 Word 或其他附件。"
            self._prune_overlay_texts(overlay_session)
            if not overlay_session.text_queue:
                self.overlay_store.update(overlay_session)
                return (
                    "目前沒有等待配對的文字，因此這張照片沒有加入佇列。\n"
                    "請先傳送一段或多段文字，再依相同順序重新傳送照片。"
                )
            if event_id and event_id in overlay_session.seen_image_event_ids:
                return "這張照片已經處理過，不會重複配對文字。"
            try:
                content, content_type = self._download_line_message_content(message_id)
            except Exception as exc:
                return f"照片下載失敗，文字仍保留等待配對：{exc}"

            filename = self._infer_line_filename(message, content_type)
            saved_path = self._save_upload(filename, content)
            result = self._create_overlay_from_image(sender_id, overlay_session, saved_path)
            if event_id and "圖片文字合成失敗" not in result:
                overlay_session.seen_image_event_ids.append(event_id)
                overlay_session.seen_image_event_ids = overlay_session.seen_image_event_ids[-500:]
                self.overlay_store.update(overlay_session)
            return result

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
            "menu": "menu",
            "選單": "menu",
            "功能選單": "menu",
            "行程模式": "schedule_mode",
            "行程管理": "schedule_mode",
            "/加字": "overlay_start",
            "加字模式": "overlay_start",
            "開始加字": "overlay_start",
            "圖片加字": "overlay_start",
            "圖片加字模式": "overlay_start",
            "繼續貼字": "overlay_continue",
            "繼續加字": "overlay_continue",
            "/結束加字": "overlay_end",
            "結束加字": "overlay_end",
            "回到行程模式": "schedule_mode",
            "/取消加字": "overlay_cancel",
            "取消加字": "overlay_cancel",
            "/加字狀態": "overlay_status",
            "加字狀態": "overlay_status",
            "/補送圖片": "overlay_retry",
            "補送圖片": "overlay_retry",
            "重試圖片": "overlay_retry",
            "重試回傳": "overlay_retry",
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
        if command == "menu":
            return self.FUNCTION_MENU_SENTINEL

        if command in self.OVERLAY_COMMANDS:
            return self._execute_overlay_command(sender_id, command)

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

    def _execute_overlay_command(self, sender_id: str, command: str) -> str:
        session = self.overlay_store.get_or_create(sender_id)
        self._prune_overlay_texts(session)
        cleared_images, missing_outputs = self._clean_legacy_overlay_state(session)

        if command == "overlay_start":
            session.active = True
            session.updated_at = self._now_iso()
            session.last_error = ""
            self.overlay_store.update(session)
            lines = [
                "已進入圖片加字模式，現在和行程功能完全分開。\n"
                "步驟 1：先依順序傳完所有文字，每則 LINE 訊息算一段。\n"
                "步驟 2：再依相同順序傳送照片。\n"
                "配對規則：第 1 段文字＋第 1 張照片，依此類推。\n"
                f"目前等待文字：{len(session.text_queue)} 段。完成後輸入「結束加字」。"
            ]
            if cleared_images or missing_outputs:
                lines.append(
                    f"系統整理：已清除舊版待處理照片 {cleared_images} 張、"
                    f"失效成品 {missing_outputs} 張。"
                )
            return "\n".join(lines)

        if command == "overlay_continue":
            session.active = True
            session.updated_at = self._now_iso()
            session.last_error = ""
            self.overlay_store.update(session)
            return (
                "已繼續貼字模式，本批紀錄保留。\n"
                f"目前尚待照片：{len(session.text_queue)} 段文字。\n"
                "可繼續傳文字或照片；完成後仍會再次顯示模式選項。"
            )

        if command == "overlay_cancel":
            cleared = len(session.text_queue)
            cleared_images = len(session.image_queue)
            session.active = False
            session.text_queue = []
            session.seen_event_ids = []
            session.image_queue = []
            session.seen_image_event_ids = []
            session.pending_outputs = []
            session.completed_count = 0
            session.output_path = ""
            session.last_error = ""
            session.updated_at = self._now_iso()
            self.overlay_store.update(session)
            return (
                f"已取消圖片加字並清空 {cleared} 段未配對文字、{cleared_images} 張待處理照片，"
                "現在恢復行程模式。"
            )

        if command in {"overlay_end", "schedule_mode"}:
            waiting = len(session.text_queue)
            completed = session.completed_count
            pending = len(session.pending_outputs)
            session.active = False
            session.text_queue = []
            session.seen_event_ids = []
            session.image_queue = []
            session.seen_image_event_ids = []
            session.pending_outputs = []
            session.completed_count = 0
            session.output_path = ""
            session.last_error = ""
            session.updated_at = self._now_iso()
            self.overlay_store.update(session)
            return (
                "已切換至行程模式。\n"
                f"本批貼字紀錄已歸零：完成 {completed} 張、未配文字 {waiting} 段、"
                f"待補送 {pending} 張。\n"
                "現在傳送的文字、圖片與 Word 會交給行程功能。"
            )

        if command == "overlay_retry":
            if not session.pending_outputs:
                self.overlay_store.update(session)
                return "目前沒有等待補送的圖片。可以繼續傳文字與照片。"

            output_path = session.pending_outputs[0]
            session.last_error = "正在手動補送第 1 張圖片"
            session.updated_at = self._now_iso()
            self.overlay_store.update(session)
            if self._push_image_message(sender_id, output_path):
                session.pending_outputs.pop(0)
                session.last_error = ""
                session.updated_at = self._now_iso()
                self.overlay_store.update(session)
                return (
                    "圖片補送成功。\n"
                    f"剩餘等待補送：{len(session.pending_outputs)} 張。\n"
                    "系統沒有啟動背景重試；如仍有圖片，請再次輸入「補送圖片」。"
                )

            session.last_error = "手動補送失敗；成品仍安全保留"
            session.updated_at = self._now_iso()
            self.overlay_store.update(session)
            return (
                "圖片補送失敗，但成品沒有遺失。\n"
                f"仍等待補送：{len(session.pending_outputs)} 張。\n"
                "請稍後再次輸入「補送圖片」，不需要重新傳文字或照片。"
            )

        mode = "加字模式" if session.active else "行程模式"
        lines = [
            f"目前模式：{mode}",
            f"等待照片的文字：{len(session.text_queue)} 段",
            f"已完成圖片：{session.completed_count} 張",
            f"等待 LINE 補送：{len(session.pending_outputs)} 張",
        ]
        if session.last_error:
            lines.append(f"最後錯誤：{session.last_error}")
        if cleared_images or missing_outputs:
            lines.append(
                f"系統整理：清除舊版待處理照片 {cleared_images} 張、"
                f"失效成品 {missing_outputs} 張"
            )
        if session.pending_outputs:
            lines.append("可輸入「補送圖片」單次重試，不會在背景自動重送。")
        self.overlay_store.update(session)
        return "\n".join(lines)

    @staticmethod
    def _clean_legacy_overlay_state(session: OverlaySession) -> tuple[int, int]:
        cleared_images = len(session.image_queue)
        session.image_queue = []

        valid_outputs: list[str] = []
        missing_outputs = 0
        for output_path in session.pending_outputs:
            if Path(output_path).is_file():
                valid_outputs.append(output_path)
            else:
                missing_outputs += 1
        session.pending_outputs = valid_outputs
        return cleared_images, missing_outputs

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

    def _reply_line_function_menu(self, reply_token: str) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token or not reply_token:
            return False
        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/reply",
            token,
            {
                "replyToken": reply_token,
                "messages": [self._function_menu_message()],
            },
        )
        return self._send_line_request(req, timeout=10, retries=2)

    def _reply_line_overlay_choice(self, reply_token: str, text: str) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token or not reply_token or not text:
            return False
        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/reply",
            token,
            {
                "replyToken": reply_token,
                "messages": [self._overlay_choice_message(text)],
            },
        )
        return self._send_line_request(req, timeout=10, retries=2)

    def _push_line_function_menu(self, to: str) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token or not to:
            return False
        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/push",
            token,
            {"to": to, "messages": [self._function_menu_message()]},
        )
        return self._send_line_request(req, timeout=15, retries=self.line_push_retries)

    def _push_line_overlay_choice(self, to: str, text: str) -> bool:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        if not token or not to or not text:
            return False
        req = self._make_line_request(
            "https://api.line.me/v2/bot/message/push",
            token,
            {"to": to, "messages": [self._overlay_choice_message(text)]},
        )
        return self._send_line_request(req, timeout=15, retries=self.line_push_retries)

    @staticmethod
    def _function_menu_message() -> dict[str, Any]:
        return {
            "type": "template",
            "altText": "功能選單：請選擇行程管理或圖片加字",
            "template": {
                "type": "buttons",
                "title": "Marcus Schedule Bot",
                "text": "請先選擇功能，再依系統提示進行下一步。",
                "actions": [
                    {"type": "message", "label": "行程管理", "text": "行程模式"},
                    {"type": "message", "label": "圖片加字", "text": "加字模式"},
                    {"type": "message", "label": "加字狀態", "text": "加字狀態"},
                    {"type": "message", "label": "使用說明", "text": "幫助"},
                ],
            },
        }

    @staticmethod
    def _overlay_choice_message(text: str) -> dict[str, Any]:
        actions = [
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": "繼續貼字",
                    "text": "繼續貼字",
                },
            },
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": "回到行程模式",
                    "text": "回到行程模式",
                },
            },
        ]
        if "回傳失敗" in text or "等待補送" in text:
            actions.insert(
                0,
                {
                    "type": "action",
                    "action": {
                        "type": "message",
                        "label": "補送圖片",
                        "text": "補送圖片",
                    },
                },
            )
        return {
            "type": "text",
            "text": text[:5000],
            "quickReply": {"items": actions},
        }

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

    def _sender_lock(self, sender_id: str) -> threading.RLock:
        with self._sender_locks_guard:
            return self._sender_locks.setdefault(sender_id, threading.RLock())

    def _prune_overlay_texts(self, session: OverlaySession) -> int:
        now = datetime.now()
        valid: list[dict[str, str]] = []
        expired = 0
        for part in session.text_queue:
            try:
                created_at = datetime.fromisoformat(part["at"])
                if (now - created_at).total_seconds() <= self.overlay_pair_seconds:
                    valid.append(part)
                else:
                    expired += 1
            except (KeyError, TypeError, ValueError):
                expired += 1
        session.text_queue = valid
        return expired

    def _queue_overlay_image(
        self,
        sender_id: str,
        session: OverlaySession,
        image_path: Path,
        event_timestamp: int | None = None,
        message_id: str = "",
        event_id: str = "",
    ) -> str:
        expected = len(session.text_queue)
        if len(session.image_queue) >= expected:
            return (
                "本批照片已收齊，這張額外照片沒有加入佇列。\n"
                "請等待本批完成，或切換回行程模式後重新開始。"
            )

        session.image_queue.append(
            {
                "path": str(image_path),
                "at": self._now_iso(),
                "event_timestamp": str(event_timestamp or ""),
                "message_id": message_id,
                "event_id": event_id,
            }
        )
        if event_id:
            session.seen_image_event_ids.append(event_id)
            session.seen_image_event_ids = session.seen_image_event_ids[-500:]
        self._sort_overlay_image_queue(session)
        session.updated_at = self._now_iso()
        session.last_error = ""
        self.overlay_store.update(session)

        received = len(session.image_queue)
        self._start_overlay_batch_job(sender_id)
        if received >= expected:
            return (
                f"照片已收齊：目前佇列 {received} 張、待配文字 {expected} 段。\n"
                "數量相同，系統會依 LINE 傳送時間排序後立即在背景處理。"
            )
        return (
            f"照片已收到：目前佇列 {received} 張、待配文字 {expected} 段。\n"
            f"系統會短暫等待約 {self.overlay_image_settle_seconds:g} 秒，依 LINE 傳送時間排序後"
            "立即在背景處理目前已有照片，不必等全部照片傳完。"
        )

    def _start_overlay_batch_job(self, sender_id: str) -> None:
        with self._overlay_job_lock:
            if sender_id in self._active_overlay_jobs:
                return
            self._active_overlay_jobs.add(sender_id)

        worker = threading.Thread(
            target=self._run_overlay_batch_job,
            args=(sender_id,),
            daemon=False,
            name=f"line-overlay-{sha256(sender_id.encode('utf-8')).hexdigest()[:8]}",
        )
        worker.start()

    def _run_overlay_batch_job(self, sender_id: str) -> None:
        final_reply = ""
        failed = False
        try:
            previous_signature: tuple[tuple[str, str, str], ...] | None = None
            while True:
                session = self.overlay_store.get(sender_id)
                if not session or not session.active or not session.image_queue:
                    return
                if len(session.image_queue) >= len(session.text_queue):
                    break
                signature = tuple(
                    (
                        str(part.get("event_timestamp", "")),
                        str(part.get("message_id", "")),
                        str(part.get("path", "")),
                    )
                    for part in session.image_queue
                )
                if signature == previous_signature:
                    break
                previous_signature = signature
                time.sleep(self.overlay_image_settle_seconds)

            while True:
                with self._sender_lock(sender_id):
                    session = self.overlay_store.get(sender_id)
                    if not session or not session.active:
                        return
                    if not session.text_queue or not session.image_queue:
                        break

                    self._sort_overlay_text_queue(session)
                    self._sort_overlay_image_queue(session)
                    image_part = session.image_queue.pop(0)
                    self.overlay_store.update(session)
                    result = self._create_overlay_from_image(
                        sender_id,
                        session,
                        Path(image_part["path"]),
                    )
                    if "圖片文字合成失敗" in result:
                        failed = True
                        session.image_queue.insert(0, image_part)
                        self._sort_overlay_image_queue(session)
                        self.overlay_store.update(session)
                        self._push_text_message(
                            sender_id,
                            result + "\n照片與文字都已保留原位，可輸入「繼續貼字」重新嘗試。",
                        )
                        return
                    final_reply = result.removeprefix(self.OVERLAY_COMPLETE_SENTINEL)

            session = self.overlay_store.get(sender_id)
            if not session or not session.active or not final_reply:
                return
            if not session.text_queue and not session.image_queue:
                self._push_line_overlay_choice(sender_id, final_reply)
            else:
                self._push_text_message(sender_id, final_reply)
        except Exception as exc:
            failed = True
            session = self.overlay_store.get(sender_id)
            if session:
                session.last_error = f"背景圖片批次失敗：{exc}"
                session.updated_at = self._now_iso()
                self.overlay_store.update(session)
            self._push_text_message(
                sender_id,
                f"圖片批次處理暫時失敗：{exc}\n資料已保留，請輸入「繼續貼字」重試。",
            )
        finally:
            with self._overlay_job_lock:
                self._active_overlay_jobs.discard(sender_id)
            if not failed:
                session = self.overlay_store.get(sender_id)
                if session and session.active and session.text_queue and session.image_queue:
                    self._start_overlay_batch_job(sender_id)

    def _create_overlay_from_image(
        self,
        sender_id: str,
        session: OverlaySession,
        image_path: Path,
    ) -> str:
        expired = self._prune_overlay_texts(session)
        if not session.text_queue:
            self.overlay_store.update(session)
            return "等待配對的文字已逾時，請重新傳送文字後再傳照片。"

        self._sort_overlay_text_queue(session)
        delivered = 0
        text_part = session.text_queue.pop(0)
        pair_number = session.completed_count + 1
        sender_key = sha256(sender_id.encode("utf-8")).hexdigest()[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_path = self.output_dir / (
            f"line_overlay_{sender_key}_{pair_number:04d}_{timestamp}.jpg"
        )
        try:
            result_path = self.image_text_composer.compose(
                image_path,
                text_part["text"],
                output_path,
            )
        except Exception as exc:
            session.text_queue.insert(0, text_part)
            self._sort_overlay_text_queue(session)
            session.last_error = f"第 {pair_number} 組合成失敗：{exc}"
            session.updated_at = self._now_iso()
            self.overlay_store.update(session)
            return (
                f"第 {pair_number} 組圖片文字合成失敗：{exc}\n"
                "這段文字與照片已保留在同一組，不會拿去配下一張。"
            )
        session.completed_count = pair_number
        session.output_path = result_path
        session.updated_at = self._now_iso()
        session.last_error = ""
        pushed = self._push_image_message(sender_id, result_path)
        if not pushed:
            if result_path not in session.pending_outputs:
                session.pending_outputs.append(result_path)
            session.last_error = f"第 {pair_number} 組已完成，等待手動補送"
        self.overlay_store.update(session)

        lines = [
            f"第 {pair_number} 組完成：第 {pair_number} 段文字已配第 {pair_number} 張照片。",
            f"尚待照片：{len(session.text_queue)} 段文字。",
        ]
        if pushed:
            lines.append("合成圖片已回傳；本組文字與照片不會再被使用。")
        else:
            lines.append(
                "圖片已完成但 LINE 回傳失敗，系統已保留成品；"
                "請輸入「補送圖片」單次重試。"
            )
        if delivered:
            lines.append(f"另外補送了先前等待中的圖片：{delivered} 張。")
        if expired:
            lines.append(f"已清除逾時文字：{expired} 段。")
        if not session.text_queue:
            lines.append("本批文字已全部配完，請選擇繼續貼字或回到行程模式。")
        else:
            lines.append("可直接傳下一張照片，或選擇回到行程模式。")
        return self.OVERLAY_COMPLETE_SENTINEL + "\n".join(lines)

    def _start_overlay_output_retry_job(self, sender_id: str) -> None:
        with self._overlay_retry_lock:
            if sender_id in self._active_overlay_retry_jobs:
                return
            self._active_overlay_retry_jobs.add(sender_id)

        worker = threading.Thread(
            target=self._run_overlay_output_retry_job,
            args=(sender_id,),
            daemon=False,
            name=f"line-overlay-retry-{sha256(sender_id.encode('utf-8')).hexdigest()[:8]}",
        )
        worker.start()

    def _run_overlay_output_retry_job(self, sender_id: str) -> None:
        failed_attempts = 0
        try:
            while failed_attempts < self.overlay_retry_attempts:
                session = self.overlay_store.get(sender_id)
                if not session or not session.pending_outputs:
                    return

                delay = min(60, self.overlay_retry_delay * (2**failed_attempts))
                time.sleep(delay)
                with self._sender_lock(sender_id):
                    session = self.overlay_store.get(sender_id)
                    if not session or not session.pending_outputs:
                        return
                    delivered = self._retry_overlay_outputs(sender_id, session, limit=1)
                    if delivered:
                        failed_attempts = 0
                        session.last_error = ""
                    else:
                        failed_attempts += 1
                        session.last_error = (
                            f"LINE 圖片補送第 {failed_attempts} 次失敗，系統仍會自動重試"
                        )
                    session.updated_at = self._now_iso()
                    self.overlay_store.update(session)
        finally:
            with self._overlay_retry_lock:
                self._active_overlay_retry_jobs.discard(sender_id)

    def _retry_overlay_outputs(
        self,
        sender_id: str,
        session: OverlaySession,
        limit: int | None = None,
    ) -> int:
        delivered = 0
        still_pending: list[str] = []
        pending = list(session.pending_outputs)
        candidates = pending if limit is None else pending[:limit]
        still_pending.extend(pending[len(candidates) :])
        for output_path in candidates:
            if self._push_image_message(sender_id, output_path):
                delivered += 1
            else:
                still_pending.append(output_path)
        session.pending_outputs = still_pending
        return delivered

    @staticmethod
    def _line_event_sort_key(part: dict[str, str]) -> tuple[int, int, int | str]:
        timestamp_text = str(part.get("event_timestamp", "")).strip()
        try:
            timestamp = int(timestamp_text)
        except ValueError:
            try:
                timestamp = int(datetime.fromisoformat(part.get("at", "")).timestamp() * 1000)
            except (TypeError, ValueError):
                timestamp = 0

        message_id = str(part.get("message_id", "")).strip()
        numeric_message_id = "".join(character for character in message_id if character.isdigit())
        if numeric_message_id:
            return timestamp, 0, int(numeric_message_id)
        return timestamp, 1, ""

    @staticmethod
    def _sort_overlay_text_queue(session: OverlaySession) -> None:
        session.text_queue.sort(key=LineMVPService._line_event_sort_key)

    @staticmethod
    def _sort_overlay_image_queue(session: OverlaySession) -> None:
        session.image_queue.sort(key=LineMVPService._line_event_sort_key)

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
        return self._send_line_request(req, timeout=12, retries=1)

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
            "若要使用圖片加字，請先輸入「加字模式」；一般附件只會交給行程功能處理。"
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
            "5. 加字模式：進入獨立圖片加字功能\n"
            "6. 加字狀態：查看待配對數量\n"
            "7. 結束加字：返回行程模式\n"
            "8. 取消加字：清空本批未配對文字\n"
            "也可以直接傳新的行程文字、圖片、PDF、Word 模板給我。\n"
            "圖片加字必須先傳完文字，再傳照片；第 1 段配第 1 張，依序一對一回傳。"
        )

    def _retry_delay_seconds(self, attempt: int) -> int:
        return min(self.apply_retry_max_delay, self.apply_retry_delay * attempt)

    def _should_stop_retry(self, attempt: int) -> bool:
        return self.apply_max_retries > 0 and attempt >= self.apply_max_retries

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")
