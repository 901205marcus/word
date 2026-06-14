from __future__ import annotations

import json
import os
import re
from typing import Iterable, Optional

from openai import OpenAI

from .models import ActionType, ScheduleAction

DELETE_KEYWORDS = ("取消", "刪除", "移除", "不用了", "改期", "延後")
DATE_PATTERN = re.compile(r"(?P<date>\d{1,2}/\d{1,2})")
TIME_PATTERN = re.compile(
    r"(?P<time>(?:上午|下午|晚上|中午)?\s*\d{1,2}(?::|：|\.|點)\d{0,2}(?:\s*[-~～]\s*\d{1,2}(?::|：)\d{2})?)"
)
NUMBERED_LINE_PATTERN = re.compile(r"^\s*\d+[.、]\s*")


def normalize_date(value: str | None) -> str:
    if not value:
        return ""
    match = re.match(r"^\s*(\d{1,2})/(\d{1,2})\s*$", str(value))
    if not match:
        return str(value).strip()
    return f"{int(match.group(1))}/{int(match.group(2))}"


def normalize_time(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    text = text.replace("：", ":").replace("點", ":").replace("．", ":").replace(".", ":")
    text = text.replace("OO", "00").replace("O", "0").replace("o", "0")
    text = re.sub(r"\s+", "", text)
    match = re.match(r"^(上午|下午|晚上|中午)?(\d{1,2})(?::(\d{1,2}))?$", text)
    if not match:
        return text
    prefix, hour_text, minute_text = match.groups()
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if prefix in {"下午", "晚上"} and hour < 12:
        hour += 12
    if prefix == "中午" and hour < 11:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def clean_line(line: str) -> str:
    line = NUMBERED_LINE_PATTERN.sub("", line.strip())
    line = re.sub(r"^[：:]", "", line).strip()
    return line


class MessageParser:
    def __init__(self, model: str = "gpt-4.1-mini", openai_enabled: bool = True):
        self.model = model
        self.openai_enabled = openai_enabled

    def parse(self, message: str) -> list[ScheduleAction]:
        rule_actions = self._rule_parse(message)
        if not self.openai_enabled or not os.getenv("OPENAI_API_KEY"):
            return self._merge(rule_actions, [])
        ai_actions = self._openai_parse(message)
        return self._merge(rule_actions, ai_actions)

    def _rule_parse(self, message: str) -> list[ScheduleAction]:
        structured = self._structured_notice_parse(message)
        segmented = self._multi_date_short_parse(message)
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        if segmented and len(lines) <= 2 and not structured:
            return segmented
        actions: list[ScheduleAction] = list(structured) + list(segmented)
        current_date: Optional[str] = None

        for raw_line in lines:
            date_match = DATE_PATTERN.search(raw_line)
            if date_match:
                current_date = normalize_date(date_match.group("date"))

            if not current_date:
                continue

            line = clean_line(raw_line)

            if any(keyword in line for keyword in DELETE_KEYWORDS):
                time_match = TIME_PATTERN.search(line)
                actions.append(
                    ScheduleAction(
                        action=ActionType.DELETE if time_match else ActionType.DELETE_DAY,
                        date=current_date,
                        time=normalize_time(time_match.group("time")) if time_match else "",
                        source_text=raw_line,
                        confidence=0.9 if time_match else 0.92,
                        requires_review=False if time_match else True,
                    )
                )
                continue

            time_match = TIME_PATTERN.search(line)
            event_text = DATE_PATTERN.sub("", line, count=1).strip(" ：:")
            if time_match:
                actions.append(
                    ScheduleAction(
                        action=ActionType.ADD,
                        date=current_date,
                        time=normalize_time(time_match.group("time")),
                        event=event_text,
                        source_text=raw_line,
                        confidence=0.78,
                        requires_review=True,
                    )
                )
            elif event_text:
                actions.append(
                    ScheduleAction(
                        action=ActionType.ADD,
                        date=current_date,
                        event=event_text,
                        source_text=raw_line,
                        confidence=0.62,
                        requires_review=True,
                    )
                )

        return actions

    def _multi_date_short_parse(self, message: str) -> list[ScheduleAction]:
        compact = message.replace("\n", "，")
        matches = list(re.finditer(r"(\d{1,2}/\d{1,2})", compact))
        if len(matches) < 2:
            return []

        actions: list[ScheduleAction] = []
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(compact)
            segment = compact[start:end].strip("，,。；; ")
            date_text = normalize_date(match.group(1))
            without_date = segment[len(match.group(1)):].strip("，,。；; ")
            time_match = TIME_PATTERN.search(without_date)
            event = without_date
            if not event:
                continue
            actions.append(
                ScheduleAction(
                    action=ActionType.ADD,
                    date=date_text,
                    time=normalize_time(time_match.group("time")) if time_match else "",
                    event=event,
                    source_text=segment,
                    confidence=0.72,
                    requires_review=True,
                )
            )
        return actions

    def _structured_notice_parse(self, message: str) -> list[ScheduleAction]:
        lines = [clean_line(line) for line in message.splitlines() if line.strip()]
        if not lines:
            return []

        time_line = next((line for line in lines if "時間" in line), "")
        if not time_line:
            return []

        date_text = ""
        time_text = ""
        compact_match = re.search(
            r"時間[:：]?\s*(\d{2})(\d{2})(?:[\(（][^\)）]*[\)）])?\s*(\d{1,2}[:：]\d{2})",
            time_line,
        )
        slash_match = re.search(
            r"時間[:：]?\s*(\d{1,2})/(\d{1,2})(?:[\(（][^\)）]*[\)）])?\s*(\d{1,2}[:：]\d{2})",
            time_line,
        )
        if compact_match:
            date_text = normalize_date(f"{compact_match.group(1)}/{compact_match.group(2)}")
            time_text = normalize_time(compact_match.group(3))
        elif slash_match:
            date_text = normalize_date(f"{slash_match.group(1)}/{slash_match.group(2)}")
            time_text = normalize_time(slash_match.group(3))

        if not date_text:
            return []

        event = next(
            (
                line
                for line in lines
                if all(token not in line for token in ("時間", "地點", "參加人員", "說明"))
            ),
            "未命名行程",
        )
        address_line = next((line for line in lines if "地點" in line), "")
        address = re.sub(r"^.*?地點[:：]?", "", address_line).strip()
        note_bits = []
        participant_line = next((line for line in lines if "參加人員" in line), "")
        if participant_line:
            note_bits.append(participant_line)
        if "敬邀" in lines[0] and lines[0] != event:
            note_bits.insert(0, lines[0])

        return [
            ScheduleAction(
                action=ActionType.ADD,
                date=date_text,
                time=time_text,
                event=event,
                address=address,
                note="；".join(note_bits),
                source_text=message.strip(),
                confidence=0.88,
                requires_review=True,
            )
        ]

    def _openai_parse(self, message: str) -> list[ScheduleAction]:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
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
                            "action": {"type": "string", "enum": ["add", "delete", "delete_day"]},
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
            "請把中文 LINE 行程訊息轉成結構化 JSON。"
            "action 只允許 add/delete/delete_day。"
            "日期格式一律 M/D，沒有時間就空字串。"
            "資訊不完整時 requires_review 設為 true。"
        )
        response = client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": message},
            ],
            text={"format": {"type": "json_schema", "name": "schedule_actions", "strict": True, "schema": schema}},
        )
        if not getattr(response, "output_text", ""):
            return []
        payload = json.loads(response.output_text)
        actions = []
        for item in payload.get("actions", []):
            try:
                actions.append(
                    ScheduleAction(
                        action=ActionType(item["action"]),
                        date=normalize_date(item["date"]),
                        time=normalize_time(item.get("time")),
                        event=item.get("event", "").strip(),
                        address=item.get("address", "").strip(),
                        note=item.get("note", "").strip(),
                        source_text=item.get("source_text", "").strip() or message.strip(),
                        confidence=float(item.get("confidence", 0.5)),
                        requires_review=bool(item.get("requires_review", False)),
                    )
                )
            except Exception:
                continue
        return actions

    def _merge(
        self,
        rule_actions: Iterable[ScheduleAction],
        ai_actions: Iterable[ScheduleAction],
    ) -> list[ScheduleAction]:
        merged: dict[tuple[str, str, str, str], ScheduleAction] = {}
        for action in list(rule_actions) + list(ai_actions):
            key = (action.action.value, normalize_date(action.date), normalize_time(action.time), action.event.strip())
            current = merged.get(key)
            if current is None or action.confidence >= current.confidence:
                merged[key] = action
        return sorted(merged.values(), key=lambda x: (x.date, x.time, x.action.value, x.event))
