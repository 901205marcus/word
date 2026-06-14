from __future__ import annotations

import hmac
import json
import os
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from .models import InboxMessage, MessageStatus
from .parser import MessageParser
from .storage import InboxStore
from .word_editor import ScheduleWordEditor


class LineMVPService:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "data" / "line_mvp"
        self.output_dir = self.base_dir / "outputs"
        self.store = InboxStore(self.data_dir / "inbox.json")
        self.parser = MessageParser(openai_enabled=True)

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
            item.actions = self.parser.parse(raw_text)
            self.store.update_message(item)
            created.append(item)
        return created

    def refresh_parse(self, message_id: str) -> InboxMessage:
        item = self._require(message_id)
        item.actions = self.parser.parse(item.raw_text)
        item.error = ""
        self.store.update_message(item)
        return item

    def mark_status(self, message_id: str, status: MessageStatus) -> InboxMessage:
        item = self._require(message_id)
        item.status = status
        self.store.update_message(item)
        return item

    def apply_to_word(self, message_id: str, docx_path: str) -> InboxMessage:
        item = self._require(message_id)
        editor = ScheduleWordEditor(docx_path)
        summary = editor.apply(item.actions, self.output_dir)
        item.status = MessageStatus.APPLIED
        item.output_path = summary.output_path or ""
        item.error = json.dumps(asdict(summary), ensure_ascii=False)
        self.store.update_message(item)
        return item

    def list_messages(self) -> list[InboxMessage]:
        return self.store.list_messages()

    def _require(self, message_id: str) -> InboxMessage:
        item = self.store.get_message(message_id)
        if item is None:
            raise KeyError(message_id)
        return item
