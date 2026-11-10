from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path

from .models import OverlaySession


class OverlaySessionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def get(self, sender_id: str) -> OverlaySession | None:
        with self._lock:
            raw = self._load().get(sender_id)
            if raw is None:
                return None
            return OverlaySession(
                sender_id=sender_id,
                active=bool(raw.get("active", False)),
                text_queue=list(raw.get("text_queue", [])),
                seen_event_ids=list(raw.get("seen_event_ids", [])),
                image_queue=list(raw.get("image_queue", [])),
                seen_image_event_ids=list(raw.get("seen_image_event_ids", [])),
                pending_outputs=list(raw.get("pending_outputs", [])),
                queue_version=int(raw.get("queue_version", 0)),
                batch_requested=bool(raw.get("batch_requested", False)),
                batch_processing=bool(raw.get("batch_processing", False)),
                completed_count=int(raw.get("completed_count", 0)),
                output_path=raw.get("output_path", ""),
                last_error=raw.get("last_error", ""),
                updated_at=raw.get("updated_at", ""),
            )

    def get_or_create(self, sender_id: str) -> OverlaySession:
        return self.get(sender_id) or OverlaySession(sender_id=sender_id)

    def update(self, session: OverlaySession) -> None:
        with self._lock:
            payload = self._load()
            payload[session.sender_id] = asdict(session)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.path)

    def list_sessions(self) -> list[OverlaySession]:
        with self._lock:
            sender_ids = list(self._load())
        return [session for sender_id in sender_ids if (session := self.get(sender_id)) is not None]

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))
