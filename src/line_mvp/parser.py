from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Iterable

from openai import OpenAI

from .models import ActionType, ScheduleAction

DELETE_KEYWORDS = ("刪除", "取消", "delete", "delete_day")
ADDRESS_LABELS = ("地點", "地址", "位於", "在")
NOTE_LABELS = ("備註", "說明", "提醒", "建議")

DATE_RE = re.compile(r"(?:(?P<year>\d{2,4})[年/-])?(?P<month>\d{1,2})[月/-](?P<day>\d{1,2})日?")
COMPACT_MMDD_RE = re.compile(r"(?<!\d)(?P<month>\d{2})(?P<day>\d{2})(?!\d)")
TIME_RE = re.compile(
    r"(?P<prefix>上午|下午|中午|凌晨|晚上|早上|AM|PM|am|pm)?\s*"
    r"(?P<hour>\d{1,2})"
    r"(?:[:：](?P<minute>\d{1,2}))?"
)
MULTI_DAY_RE = re.compile(r"^\s*(?P<month>\d{1,2})/(?P<first>\d{1,2})(?P<rest>(?:[、,，]\d{1,2})+)(?P<tail>.*)$")
DAY_ONLY_RE = re.compile(r"^\s*(?P<day>\d{1,2})(?!\d)")
NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\s*[.)、]\s*")
WEEKDAY_RE = re.compile(r"[（(]?[一二三四五六日天][)）]?")
TIME_RANGE_RE = re.compile(r"(?P<start>上午|下午|中午|凌晨|晚上|早上|AM|PM|am|pm)?\s*(?P<hour>\d{1,2})(?:[:：](?P<minute>\d{1,2}))?\s*[-~到至]\s*\d{1,2}(?:[:：]\d{1,2})?")


def normalize_date(value: str | None, *, default_year: int | None = None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    text = WEEKDAY_RE.sub("", text)
    text = text.replace("／", "/").replace("-", "/").replace(".", "/")

    match = DATE_RE.search(text)
    if match:
        year_text = match.group("year")
        if year_text:
            year = int(year_text)
            if year >= 1911:
                default_year = year
            elif year >= 100:
                default_year = year + 1911
        return f"{int(match.group('month'))}/{int(match.group('day'))}"

    match = COMPACT_MMDD_RE.fullmatch(text)
    if match:
        return f"{int(match.group('month'))}/{int(match.group('day'))}"

    slash_match = re.search(r"(?:(\d{2,4})/)?(\d{1,2})/(\d{1,2})", text)
    if slash_match:
        year_text, month_text, day_text = slash_match.groups()
        if year_text:
            year = int(year_text)
            if year >= 1911:
                default_year = year
            elif year >= 100:
                default_year = year + 1911
        return f"{int(month_text)}/{int(day_text)}"

    return text


def normalize_time(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    text = text.replace("：", ":").replace(".", ":")
    text = text.replace("OO", "00").replace("O", "0").replace("o", "0")
    text = re.sub(r"\s+", "", text)

    prefix = ""
    for token in ("上午", "下午", "中午", "凌晨", "晚上", "早上", "AM", "PM", "am", "pm"):
        if text.startswith(token):
            prefix = token.lower()
            text = text[len(token) :]
            break

    if re.fullmatch(r"\d{1,2}點半", text):
        hour = int(text[:-2])
        minute = 30
    elif re.fullmatch(r"\d{1,2}點\d{1,2}分?", text):
        match = re.match(r"(\d{1,2})點(\d{1,2})", text)
        hour = int(match.group(1))
        minute = int(match.group(2))
    elif re.fullmatch(r"\d{1,2}點", text):
        hour = int(text[:-1])
        minute = 0
    else:
        match = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", text)
        if not match:
            return text
        hour = int(match.group(1))
        minute = int(match.group(2) or "0")

    if prefix in {"pm", "下午", "晚上"} and hour < 12:
        hour += 12
    elif prefix == "中午" and hour < 12:
        hour += 12
    elif prefix in {"am", "凌晨", "早上", "上午"} and hour == 12:
        hour = 0

    return f"{hour:02d}:{minute:02d}"


def time_sort_key(value: str) -> tuple[int, int]:
    normalized = normalize_time(value)
    match = re.fullmatch(r"(\d{2}):(\d{2})", normalized)
    if not match:
        return (99, 99)
    return int(match.group(1)), int(match.group(2))


def clean_line(line: str) -> str:
    line = NUMBER_PREFIX_RE.sub("", line.strip())
    return line.lstrip("-•").strip()


def detect_address(text: str) -> str:
    for label in ADDRESS_LABELS:
        match = re.search(rf"{label}[:：]?\s*([^\n，。,；;]+)", text)
        if match:
            return match.group(1).strip()
    address_match = re.search(r"([^\s，。,；;]*(?:路|街|段|巷|弄|號|樓|市|區|鄉|鎮)[^\s，。,；;]*)", text)
    return address_match.group(1).strip() if address_match else ""


def detect_note(text: str) -> str:
    parts: list[str] = []
    for label in NOTE_LABELS:
        match = re.search(rf"{label}[:：]?\s*([^\n]+)", text)
        if match:
            parts.append(f"{label}：{match.group(1).strip()}")
    return " | ".join(parts)


class MessageParser:
    def __init__(self, model: str = "gpt-4.1-mini", openai_enabled: bool = True):
        self.model = model
        self.openai_enabled = openai_enabled

    def parse(self, message: str) -> list[ScheduleAction]:
        rule_actions = self._rule_parse(message)
        if not self.openai_enabled or not os.getenv("OPENAI_API_KEY"):
            return self._sort_and_dedupe(rule_actions)
        ai_actions = self._openai_parse(message)
        return self._sort_and_dedupe(self._merge(rule_actions, ai_actions))

    def _rule_parse(self, message: str) -> list[ScheduleAction]:
        cleaned_message = message.strip()
        if not cleaned_message:
            return []

        actions: list[ScheduleAction] = []
        current_date = ""
        lines = [clean_line(line) for line in cleaned_message.splitlines() if line.strip()]

        structured = self._structured_notice_parse(cleaned_message)
        if structured:
            return structured

        for line in lines:
            current_date = self._update_current_date(line, current_date)

            expanded = self._expand_multi_day_line(line)
            if expanded:
                actions.extend(expanded)
                current_date = expanded[-1].date
                continue

            delete_action = self._parse_delete_line(line, current_date)
            if delete_action:
                actions.append(delete_action)
                continue

            date_in_line = self._extract_date(line, current_date=current_date) or current_date
            if not date_in_line:
                continue

            time_in_line = self._extract_time(line)
            event = self._strip_known_tokens(line, date_in_line, time_in_line)
            address = detect_address(line)
            note = detect_note(line)
            if not event and not address and not note:
                continue

            actions.append(
                ScheduleAction(
                    action=ActionType.ADD,
                    date=date_in_line,
                    time=time_in_line,
                    event=event,
                    address=address,
                    note=note,
                    source_text=line,
                    confidence=0.8 if time_in_line else 0.7,
                    requires_review=True,
                )
            )

        if not actions:
            actions.extend(self._compact_multi_date_parse(cleaned_message))
        return actions

    def _structured_notice_parse(self, message: str) -> list[ScheduleAction]:
        date_text = self._extract_date(message)
        if not date_text:
            return []
        if not any(label in message for label in ("時間", "地點", "地址", "參加", "行程")):
            return []

        time_text = self._extract_time(message)
        address = detect_address(message)
        note = detect_note(message)

        event = ""
        for line in [clean_line(line) for line in message.splitlines() if line.strip()]:
            if any(keyword in line for keyword in ("時間", "地點", "地址", "備註", "說明")):
                continue
            if len(line) >= 4:
                event = line
                break
        if not event:
            event = "未命名行程"

        return [
            ScheduleAction(
                action=ActionType.ADD,
                date=date_text,
                time=time_text,
                event=event,
                address=address,
                note=note,
                source_text=message.strip(),
                confidence=0.9,
                requires_review=True,
            )
        ]

    def _compact_multi_date_parse(self, message: str) -> list[ScheduleAction]:
        text = message.replace("\n", " ")
        matches = list(DATE_RE.finditer(text))
        if len(matches) < 2:
            return []

        actions: list[ScheduleAction] = []
        for index, match in enumerate(matches):
            segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segment = text[match.start() : segment_end].strip(" ，,;；")
            date_text = normalize_date(match.group(0))
            time_text = self._extract_time(segment)
            event = self._strip_known_tokens(segment, date_text, time_text)
            if not event:
                continue
            actions.append(
                ScheduleAction(
                    action=ActionType.ADD,
                    date=date_text,
                    time=time_text,
                    event=event,
                    source_text=segment,
                    confidence=0.75,
                    requires_review=True,
                )
            )
        return actions

    def _parse_delete_line(self, line: str, current_date: str) -> ScheduleAction | None:
        lowered = line.lower()
        if not any(keyword in line or keyword in lowered for keyword in DELETE_KEYWORDS):
            return None

        date_text = self._extract_date(line, current_date=current_date) or current_date
        if not date_text:
            return None
        time_text = self._extract_time(line)
        return ScheduleAction(
            action=ActionType.DELETE if time_text else ActionType.DELETE_DAY,
            date=date_text,
            time=time_text,
            source_text=line,
            confidence=0.95,
            requires_review=False,
        )

    def _extract_date(self, text: str, current_date: str = "") -> str:
        match = DATE_RE.search(text)
        if match:
            return normalize_date(match.group(0))
        compact_match = COMPACT_MMDD_RE.search(text)
        if compact_match:
            return normalize_date(compact_match.group(0))
        if current_date:
            inferred = self._infer_same_month_date(text, current_date)
            if inferred:
                return inferred
        return ""

    def _extract_time(self, text: str) -> str:
        candidate_text = text
        candidate_text = re.sub(r"\b\d{1,2}/\d{1,2}\b", " ", candidate_text)
        candidate_text = re.sub(r"\b\d{2,4}年\d{1,2}月\d{1,2}日?\b", " ", candidate_text)
        candidate_text = re.sub(r"\b\d{1,2}月\d{1,2}日?\b", " ", candidate_text)
        candidate_text = COMPACT_MMDD_RE.sub(" ", candidate_text)

        patterns = [
            r"(上午|下午|中午|凌晨|晚上|早上)\s*\d{1,2}[:：]\d{1,2}",
            r"(上午|下午|中午|凌晨|晚上|早上)\s*\d{1,2}點半",
            r"(上午|下午|中午|凌晨|晚上|早上)\s*\d{1,2}點\d{0,2}分?",
            r"\d{1,2}[:：]\d{1,2}\s*[-~到至]\s*\d{1,2}[:：]\d{1,2}",
            r"(上午|下午|中午|凌晨|晚上|早上)\s*\d{1,2}[:：]\d{1,2}\s*[-~到至]\s*\d{1,2}[:：]\d{1,2}",
            r"\b\d{1,2}[:：]\d{1,2}\b",
            r"(上午|下午|中午|凌晨|晚上|早上)\s*\d{1,2}點",
            r"\b\d{1,2}點半\b",
            r"\b\d{1,2}點\b",
        ]

        for pattern in patterns:
            match = re.search(pattern, candidate_text)
            if not match:
                continue
            raw = match.group(0).strip()
            raw = re.split(r"[-~到至]", raw, maxsplit=1)[0].strip()
            normalized = normalize_time(raw)
            if re.fullmatch(r"\d{2}:\d{2}", normalized):
                return normalized
        return ""

    def _update_current_date(self, line: str, current_date: str) -> str:
        extracted = self._extract_date(line, current_date=current_date)
        return extracted or current_date

    def _expand_multi_day_line(self, line: str) -> list[ScheduleAction]:
        match = MULTI_DAY_RE.match(line)
        if not match:
            return []

        month = int(match.group("month"))
        days = [int(match.group("first"))]
        days.extend(int(token) for token in re.findall(r"\d{1,2}", match.group("rest")))
        tail = match.group("tail").strip(" ，,;；")
        if not tail:
            return []

        time_text = self._extract_time(tail)
        address = detect_address(tail)
        note = detect_note(tail)
        event = self._strip_multi_day_prefix(line, month, days, time_text)
        if not event and not address and not note:
            return []

        return [
            ScheduleAction(
                action=ActionType.ADD,
                date=f"{month}/{day}",
                time=time_text,
                event=event,
                address=address,
                note=note,
                source_text=line,
                confidence=0.85 if time_text else 0.78,
                requires_review=True,
            )
            for day in days
        ]

    @staticmethod
    def _infer_same_month_date(text: str, current_date: str) -> str:
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", normalize_date(current_date))
        if not match:
            return ""
        month = int(match.group(1))
        day_match = DAY_ONLY_RE.match(text.strip())
        if not day_match:
            return ""
        return f"{month}/{int(day_match.group('day'))}"

    def _strip_known_tokens(self, text: str, date_text: str, time_text: str) -> str:
        result = text

        if date_text:
            month, day = date_text.split("/")
            date_candidates = {
                date_text,
                f"{int(month)}/{int(day)}",
                f"{int(month):02d}/{int(day):02d}",
                f"{int(month):02d}{int(day):02d}",
            }
            for candidate in date_candidates:
                result = result.replace(candidate, " ")

        if time_text:
            hour = int(time_text.split(":")[0])
            minute = int(time_text.split(":")[1])
            meridiem_hour = hour - 12 if hour > 12 else hour
            time_candidates = {
                time_text,
                f"{hour}:{minute:02d}",
                f"{hour}點" if minute == 0 else "",
                f"{hour}點{minute:02d}",
                f"{hour}點半" if minute == 30 else "",
                f"下午{meridiem_hour}:{minute:02d}" if hour >= 12 else "",
                f"下午{meridiem_hour}點" if hour >= 12 and minute == 0 else "",
                f"下午{meridiem_hour}點{minute:02d}" if hour >= 12 and minute else "",
                f"上午{hour}:{minute:02d}" if hour < 12 else "",
                f"上午{hour}點" if hour < 12 and minute == 0 else "",
            }
            for candidate in time_candidates:
                if candidate:
                    result = result.replace(candidate, " ")

        for token in DELETE_KEYWORDS + ADDRESS_LABELS + NOTE_LABELS + ("時間", "參加人員", "報到", "請安排"):
            result = result.replace(token, " ")

        result = re.sub(r"[:：()（）,，;；]", " ", result)
        result = re.sub(r"\s+", " ", result).strip()
        return result

    def _strip_multi_day_prefix(self, text: str, month: int, days: list[int], time_text: str) -> str:
        result = text
        result = result.replace(f"{month}/{days[0]}", " ", 1)
        for day in days[1:]:
            result = re.sub(rf"^[\s、,，]*{day}(?!\d)", " ", result, count=1)
            result = result.replace(f"、{day}", " ", 1).replace(f",{day}", " ", 1).replace(f"，{day}", " ", 1)
        return self._strip_known_tokens(result, "", time_text)

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
        current_year = datetime.now().year
        prompt = (
            "請把使用者的行程訊息拆成 JSON actions。"
            "日期統一輸出 M/D，時間統一 24 小時 HH:MM。"
            f"若文字沒有年份，預設當年 {current_year}。"
            "action 只能是 add、delete、delete_day。"
        )
        response = client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": message},
            ],
            text={"format": {"type": "json_schema", "name": "schedule_actions", "strict": True, "schema": schema}},
        )
        payload = json.loads(getattr(response, "output_text", "") or '{"actions":[]}')
        actions: list[ScheduleAction] = []
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
                        requires_review=bool(item.get("requires_review", True)),
                    )
                )
            except Exception:
                continue
        return actions

    def _merge(self, rule_actions: Iterable[ScheduleAction], ai_actions: Iterable[ScheduleAction]) -> list[ScheduleAction]:
        merged: dict[tuple[str, str, str, str], ScheduleAction] = {}
        for action in list(rule_actions) + list(ai_actions):
            key = (
                action.action.value,
                normalize_date(action.date),
                normalize_time(action.time),
                action.event.strip(),
            )
            current = merged.get(key)
            if current is None or action.confidence >= current.confidence:
                merged[key] = action
        return list(merged.values())

    def _sort_and_dedupe(self, actions: Iterable[ScheduleAction]) -> list[ScheduleAction]:
        merged = self._merge(actions, [])
        return sorted(
            merged,
            key=lambda item: (
                self._date_sort_key(item.date),
                time_sort_key(item.time),
                item.action.value,
                item.event,
            ),
        )

    @staticmethod
    def _date_sort_key(value: str) -> tuple[int, int]:
        normalized = normalize_date(value)
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", normalized)
        if not match:
            return (99, 99)
        return int(match.group(1)), int(match.group(2))
