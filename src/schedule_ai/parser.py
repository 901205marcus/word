from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from typing import Iterable, Optional

from .config import AssistantConfig
from .models import ActionType, ScheduleAction

DELETE_KEYWORDS = ("取消", "刪除", "移除", "取消掉", "不用了")
TIME_PATTERN = re.compile(r"(?P<time>(?:上午|下午|晚上)?\s*\d{1,2}[:：.]\d{2}(?:\s*[-~～]\s*\d{1,2}[:：.]\d{2})?)")
DATE_PATTERN = re.compile(r"(?P<date>\d{1,2}/\d{1,2})")
NUMBERED_LINE_PATTERN = re.compile(r"^\s*\d+[.、]\s*")


def normalize_time(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    text = text.replace("：", ":").replace("．", ":").replace("。", ":")
    text = text.replace("OO", "00").replace("O", "0").replace("o", "0")
    text = re.sub(r"\s+", "", text)
    return text


def normalize_date(value: str | None) -> str:
    if not value:
        return ""
    match = re.match(r"^\s*(\d{1,2})/(\d{1,2})\s*$", str(value))
    if not match:
        return str(value).strip()
    return f"{int(match.group(1))}/{int(match.group(2))}"


def clean_event_text(text: str) -> str:
    text = NUMBERED_LINE_PATTERN.sub("", text.strip())
    text = DATE_PATTERN.sub("", text, count=1).strip()
    return text.lstrip("：: ")


class MessageParser:
    def __init__(self, config: AssistantConfig):
        self.config = config

    def parse(self, message: str) -> list[ScheduleAction]:
        actions = self._rule_based_parse(message)
        if self.config.openai.enabled:
            ai_actions = self._parse_with_openai(message)
            actions = self._merge_actions(actions, ai_actions)
        return actions

    def _rule_based_parse(self, message: str) -> list[ScheduleAction]:
        structured_actions = self._structured_invite_parse(message)
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        actions: list[ScheduleAction] = list(structured_actions)
        current_date: Optional[str] = None

        for raw_line in lines:
            date_match = DATE_PATTERN.search(raw_line)
            if date_match:
                current_date = normalize_date(date_match.group("date"))

            if not current_date:
                continue

            normalized_line = raw_line.replace("｜", "|")
            if any(keyword in normalized_line for keyword in DELETE_KEYWORDS):
                time_match = TIME_PATTERN.search(normalized_line)
                if time_match:
                    actions.append(
                        ScheduleAction(
                            action=ActionType.DELETE,
                            date=current_date,
                            time=normalize_time(time_match.group("time")),
                            source_text=raw_line,
                            confidence=0.9,
                        )
                    )
                else:
                    actions.append(
                        ScheduleAction(
                            action=ActionType.DELETE_DAY,
                            date=current_date,
                            source_text=raw_line,
                            confidence=0.92,
                        )
                    )
                continue

            time_match = TIME_PATTERN.search(normalized_line)
            event = clean_event_text(normalized_line)
            if time_match:
                actions.append(
                    ScheduleAction(
                        action=ActionType.ADD,
                        date=current_date,
                        time=normalize_time(time_match.group("time")),
                        event=event,
                        source_text=raw_line,
                        confidence=0.78,
                        requires_review=True,
                    )
                )
                continue

            event = clean_event_text(normalized_line)
            if event:
                actions.append(
                    ScheduleAction(
                        action=ActionType.ADD,
                        date=current_date,
                        time="",
                        event=event,
                        source_text=raw_line,
                        confidence=0.6,
                        requires_review=True,
                    )
                )

        return actions

    def _structured_invite_parse(self, message: str) -> list[ScheduleAction]:
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        if not lines:
            return []

        time_line = next((line for line in lines if "時間" in line), "")
        if not time_line:
            return []

        compact_match = re.search(
            r"時間[:：]?\s*(\d{2})(\d{2})(?:[\(（][^\)）]*[\)）])?\s*(\d{1,2}[:：.]\d{2})",
            time_line,
        )
        slash_match = re.search(
            r"時間[:：]?\s*(\d{1,2})/(\d{1,2})(?:[\(（][^\)）]*[\)）])?\s*(\d{1,2}[:：.]\d{2})",
            time_line,
        )

        date_text = ""
        time_text = ""
        if compact_match:
            date_text = normalize_date(f"{int(compact_match.group(1))}/{int(compact_match.group(2))}")
            time_text = normalize_time(compact_match.group(3))
        elif slash_match:
            date_text = normalize_date(f"{int(slash_match.group(1))}/{int(slash_match.group(2))}")
            time_text = normalize_time(slash_match.group(3))

        if not date_text:
            return []

        event = ""
        for line in lines:
            candidate = NUMBERED_LINE_PATTERN.sub("", line)
            if any(token in candidate for token in ("時間", "地點", "參加人員", "說明")):
                continue
            event = candidate.strip("：: ")
            if event:
                break

        address = ""
        address_line = next((line for line in lines if "地點" in line), "")
        if address_line:
            address = re.sub(r"^.*?地點[:：]?", "", address_line).strip()

        note_parts = []
        participant_line = next((line for line in lines if "參加人員" in line), "")
        if participant_line:
            note_parts.append(NUMBERED_LINE_PATTERN.sub("", participant_line))
        if "請副主委參批" in message and "請副主委參批" not in participant_line:
            note_parts.append("請副主委參批")

        if not event:
            event = "未命名行程"

        return [
            ScheduleAction(
                action=ActionType.ADD,
                date=date_text,
                time=time_text,
                event=event,
                address=address,
                note="；".join(note_parts),
                source_text=message.strip(),
                confidence=0.88,
                requires_review=True,
            )
        ]

    def _parse_with_openai(self, message: str) -> list[ScheduleAction]:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return []

        try:
            from openai import OpenAI
        except ImportError:
            return []

        client = OpenAI(api_key=api_key)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["add", "delete", "delete_day"],
                            },
                            "date": {"type": "string"},
                            "time": {"type": "string"},
                            "event": {"type": "string"},
                            "address": {"type": "string"},
                            "note": {"type": "string"},
                            "confidence": {"type": "number"},
                            "requires_review": {"type": "boolean"},
                            "source_text": {"type": "string"},
                        },
                        "required": [
                            "action",
                            "date",
                            "time",
                            "event",
                            "address",
                            "note",
                            "confidence",
                            "requires_review",
                            "source_text",
                        ],
                    },
                }
            },
            "required": ["actions"],
        }

        prompt = (
            "你是一個中文行程助理。請把輸入訊息拆成行程操作 JSON。"
            "刪除整天用 delete_day，刪除單筆用 delete，新增用 add。"
            "若資訊不完整，仍可輸出，但 requires_review 要設為 true。"
            "日期固定輸出 M/D，沒有時間就輸出空字串。"
        )

        response = client.responses.create(
            model=self.config.openai.model,
            temperature=self.config.openai.temperature,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": message},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "schedule_actions",
                    "strict": True,
                    "schema": schema,
                }
            },
        )

        output_text = getattr(response, "output_text", "") or ""
        if not output_text:
            return []

        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError:
            return []

        results: list[ScheduleAction] = []
        for item in payload.get("actions", []):
            try:
                results.append(
                    ScheduleAction(
                        action=ActionType(item["action"]),
                        date=normalize_date(item["date"]),
                        time=normalize_time(item.get("time")),
                        event=item.get("event", "").strip(),
                        address=item.get("address", "").strip(),
                        note=item.get("note", "").strip(),
                        confidence=float(item.get("confidence", 0.5)),
                        requires_review=bool(item.get("requires_review", False)),
                        source_text=item.get("source_text", "").strip() or message.strip(),
                    )
                )
            except Exception:
                continue
        return results

    def _merge_actions(
        self,
        rule_actions: Iterable[ScheduleAction],
        ai_actions: Iterable[ScheduleAction],
    ) -> list[ScheduleAction]:
        merged: dict[tuple[str, str, str, str], ScheduleAction] = {}

        for action in list(rule_actions) + list(ai_actions):
            key = (
                action.action.value,
                normalize_date(action.date),
                normalize_time(action.time or ""),
                action.event.strip(),
            )
            existing = merged.get(key)
            if not existing or action.confidence >= existing.confidence:
                merged[key] = action

        return sorted(
            merged.values(),
            key=lambda item: (item.date, item.time or "", item.action.value, item.event),
        )

    @staticmethod
    def format_actions(actions: Iterable[ScheduleAction]) -> str:
        lines = []
        for index, action in enumerate(actions, start=1):
            payload = asdict(action)
            lines.append(f"{index}. {json.dumps(payload, ensure_ascii=False)}")
        return "\n".join(lines)
