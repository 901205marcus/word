from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionType(str, Enum):
    ADD = "add"
    DELETE = "delete"
    DELETE_DAY = "delete_day"


class MessageStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"
    APPLIED = "applied"
    ERROR = "error"


@dataclass(slots=True)
class ScheduleAction:
    action: ActionType
    date: str
    time: str = ""
    event: str = ""
    address: str = ""
    note: str = ""
    source_text: str = ""
    confidence: float = 0.5
    requires_review: bool = True


@dataclass(slots=True)
class InboxMessage:
    id: str
    received_at: str
    sender_id: str
    source_type: str
    raw_text: str
    status: MessageStatus = MessageStatus.PENDING
    actions: list[ScheduleAction] = field(default_factory=list)
    error: str = ""
    output_path: str = ""
    preview_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ApplySummary:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    output_path: str | None = None
    preview_paths: list[str] = field(default_factory=list)
