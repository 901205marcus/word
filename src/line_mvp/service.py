from __future__ import annotations

import hmac
import json
import os
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

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
        import base64

        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    def ingest_webhook(self, payload: dict[str, Any]) -> list[InboxMessage]:
        created: list[InboxMessage] = []
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
            item = self.store.add_message(
                sender_id=source.get("userId") or source.get("groupId") or "unknown",
                source_type=source.get("type", "unknown"),
                raw_text=raw_text,
            )
            item.actions = self._enrich_actions(self.parser.parse(raw_text))
            self.store.update_message(item)
            created.append(item)
        return created

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
