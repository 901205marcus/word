from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ActionType(str, Enum):
    ADD = "add"
    DELETE = "delete"
    DELETE_DAY = "delete_day"


@dataclass(slots=True)
class ScheduleAction:
    action: ActionType
    date: str
    time: Optional[str] = None
    event: str = ""
    address: str = ""
    note: str = ""
    source_text: str = ""
    confidence: float = 1.0
    requires_review: bool = False


@dataclass(slots=True)
class ApplyResult:
    applied: list[ScheduleAction] = field(default_factory=list)
    skipped: list[ScheduleAction] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
