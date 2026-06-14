from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(slots=True)
class OpenAIConfig:
    model: str = "gpt-4.1-mini"
    enabled: bool = True
    temperature: float = 0.1


@dataclass(slots=True)
class AssistantConfig:
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    default_year: Optional[int] = None
    today: date = field(default_factory=date.today)
    manual_parse_review: bool = True
    manual_apply_review: bool = True
    prune_past_days: bool = True
    allow_append_new_day: bool = True
    blank_note_placeholder: str = ""
    blank_address_placeholder: str = ""
    timezone_name: str = "Asia/Taipei"
