from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .models import InboxMessage, MessageStatus, ScheduleAction


class InboxStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def list_messages(self) -> list[InboxMessage]:
        return [self._from_dict(item) for item in json.loads(self.path.read_text(encoding="utf-8"))]

    def get_message(self, message_id: str) -> InboxMessage | None:
        for item in self.list_messages():
            if item.id == message_id:
                return item
        return None

    def add_message(self, sender_id: str, source_type: str, raw_text: str) -> InboxMessage:
        item = InboxMessage(
            id=str(uuid4()),
            received_at=datetime.now().isoformat(timespec="seconds"),
            sender_id=sender_id,
            source_type=source_type,
            raw_text=raw_text,
        )
        items = self.list_messages()
        items.insert(0, item)
        self._save(items)
        return item

    def update_message(self, updated: InboxMessage) -> None:
        items = self.list_messages()
        for index, item in enumerate(items):
            if item.id == updated.id:
                items[index] = updated
                self._save(items)
                return
        raise KeyError(updated.id)

    def _save(self, items: list[InboxMessage]) -> None:
        payload = []
        for item in items:
            raw = asdict(item)
            raw["status"] = item.status.value
            payload.append(raw)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _from_dict(item: dict) -> InboxMessage:
        return InboxMessage(
            id=item["id"],
            received_at=item["received_at"],
            sender_id=item["sender_id"],
            source_type=item["source_type"],
            raw_text=item["raw_text"],
            status=MessageStatus(item.get("status", "pending")),
            actions=[ScheduleAction(**action) for action in item.get("actions", [])],
            error=item.get("error", ""),
            output_path=item.get("output_path", ""),
        )
