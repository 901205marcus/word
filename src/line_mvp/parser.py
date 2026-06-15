from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Iterable

from openai import OpenAI

from .models import ActionType, ScheduleAction

DELETE_KEYWORDS = ("刪除", "取消", "移除", "延後取消", "不用", "刪掉")
ADDRESS_LABELS = ("地點", "地址", "地區", "會址")
NOTE_LABELS = ("備註", "說明", "提醒", "參加人員", "出席", "附件")
DATE_TOKEN_RE = re.compile(
    r"(?P<value>(?:\d{2,4}年)?\d{1,2}[/-]\d{1,2}|(?:\d{2,4}年)?\d{1,2}月\d{1,2}日)"
)
COMPACT_DATE_RE = re.compile(r"(?<!\d)(?P<value>\d{4})(?=[（(一二三四五六日天\s]|$)")
TIME_TOKEN_RE = re.compile(
    r"(?P<value>("
    r"(?:(?:上|下)午|中午|晚上|早上|凌晨|傍晚|AM|PM|am|pm)\s*\d{1,2}(?:(?::|：)\d{1,2})?(?:\s*點半|\s*點)?"
    r"|"
    r"\d{1,2}(?:(?::|：)\d{1,2})"
    r"|"
    r"\d{1,2}\s*點半"
    r"|"
    r"\d{1,2}\s*點"
    r"))"
)
NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\s*[.)、]\s*")
DATE_ONLY_DELETE_RE = re.compile(r"^\s*(?P<date>[^ ]+?)\s*(?:刪除|取消|移除)\s*$")
WEEKDAY_RE = re.compile(r"[（(][一二三四五六日天][）)]")
MULTI_DAY_LINE_RE = re.compile(
    r"^\s*(?P<month>\d{1,2})/(?P<first_day>\d{1,2})(?P<more>(?:[、,，]\d{1,2})+)(?P<tail>.*)$"
)
DAY_ONLY_RE = re.compile(r"^\s*(?P<day>\d{1,2})(?=[^\d:：點])")


def normalize_date(value: str | None, *, default_year: int | None = None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    text = text.replace("／", "/").replace("-", "/").replace(".", "/")
    text = WEEKDAY_RE.sub("", text)

    roc_match = re.search(r"(\d{2,4})年\s*(\d{1,2})月\s*(\d{1,2})日", text)
    if roc_match:
        year = int(roc_match.group(1))
        month = int(roc_match.group(2))
        day = int(roc_match.group(3))
        if year >= 1911:
            default_year = year
        elif year >= 100:
            default_year = year + 1911
        return f"{month}/{day}"

    md_match = re.search(r"(\d{1,2})月\s*(\d{1,2})日", text)
    if md_match:
        return f"{int(md_match.group(1))}/{int(md_match.group(2))}"

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

    compact_match = re.fullmatch(r"(\d{2})(\d{2})", text)
    if compact_match:
        return f"{int(compact_match.group(1))}/{int(compact_match.group(2))}"

    return text


def normalize_time(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    text = text.replace("：", ":").replace("．", ":").replace(".", ":")
    text = re.sub(r"(\d{1,2})點半", r"\1:30", text)
    text = re.sub(r"(\d{1,2})點(\d{1,2})", r"\1:\2", text)
    text = re.sub(r"(\d{1,2})點", r"\1", text)
    text = text.replace("OO", "00").replace("O", "0").replace("o", "0")
    text = re.sub(r"\s+", "", text)
    text = re.split(r"[-~至到]", text, maxsplit=1)[0]

    meridiem = ""
    for token in ("上午", "下午", "中午", "晚上", "早上", "凌晨", "傍晚", "AM", "PM", "am", "pm"):
        if text.startswith(token):
            meridiem = token.lower()
            text = text[len(token):]
            break

    match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", text)
    if not match:
        return text

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")

    if meridiem in {"pm", "下午", "晚上", "傍晚"} and hour < 12:
        hour += 12
    elif meridiem == "中午":
        if 1 <= hour <= 5:
            hour += 12
    elif meridiem in {"am", "凌晨"} and hour == 12:
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
    line = line.lstrip("-‧●").strip()
    return line


def detect_address(text: str) -> str:
    for label in ADDRESS_LABELS:
        match = re.search(rf"{label}[:：]?\s*([^\n]+)", text)
        if match:
            return match.group(1).strip()
    fallback = re.search(r"(?:假|於|在)\s*([^，。；\n]+(?:路|街|段|巷|號|樓)[^，。；\n]*)", text)
    if fallback:
        address = fallback.group(1).strip()
        address = re.split(r"(舉辦|召開|辦理|參加|出席)", address, maxsplit=1)[0].strip()
        return address
    return ""


def detect_note(text: str) -> str:
    note_parts: list[str] = []
    for label in NOTE_LABELS:
        match = re.search(rf"{label}[:：]?\s*([^\n]+)", text)
        if match:
            note_parts.append(f"{label}：{match.group(1).strip()}")
    return "；".join(note_parts)


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
        actions: list[ScheduleAction] = []
        cleaned_message = message.strip()
        if not cleaned_message:
            return actions

        actions.extend(self._structured_notice_parse(cleaned_message))
        if actions:
            return actions

        lines = [clean_line(line) for line in cleaned_message.splitlines() if line.strip()]
        current_date = ""

        for line in lines:
            current_date = self._update_current_date(line, current_date)
            expanded = self._expand_multi_day_line(line)
            if expanded:
                actions.extend(expanded)
                current_date = expanded[-1].date
                continue
            delete_match = DATE_ONLY_DELETE_RE.match(line)
            if delete_match:
                actions.append(
                    ScheduleAction(
                        action=ActionType.DELETE_DAY,
                        date=normalize_date(delete_match.group("date")),
                        source_text=line,
                        confidence=0.96,
                        requires_review=False,
                    )
                )
                continue

            date_in_line = self._extract_date(line, current_date=current_date) or current_date
            if not date_in_line:
                continue

            time_in_line = self._extract_time(line)
            if any(keyword in line for keyword in DELETE_KEYWORDS):
                actions.append(
                    ScheduleAction(
                        action=ActionType.DELETE if time_in_line else ActionType.DELETE_DAY,
                        date=date_in_line,
                        time=time_in_line,
                        source_text=line,
                        confidence=0.9,
                        requires_review=not bool(time_in_line),
                    )
                )
                continue

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
                    confidence=0.76 if time_in_line else 0.66,
                    requires_review=True,
                )
            )

        if not actions:
            compact = self._compact_multi_date_parse(cleaned_message)
            actions.extend(compact)

        return actions

    def _structured_notice_parse(self, message: str) -> list[ScheduleAction]:
        lines = [clean_line(line) for line in message.splitlines() if line.strip()]
        if not lines:
            return []

        joined = "\n".join(lines)
        date_text = self._extract_date(joined)
        time_text = self._extract_time(joined)
        if not date_text:
            return []

        if not any(label in joined for label in ("時間", "地點", "地址", "參加人員", "敬邀", "敬請")):
            return []

        event = ""
        for line in lines:
            if any(label in line for label in ("時間", "地點", "地址", "參加人員")):
                continue
            if len(line) >= 4:
                event = line
                break
        if not event:
            keyword_match = re.search(r"(餐會|餐敘|會議|會勘|拜會|剪綵|記者會|活動)", joined)
            event = keyword_match.group(1) if keyword_match else "未命名行程"

        address = detect_address(joined)
        note = detect_note(joined)
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
        matches = list(DATE_TOKEN_RE.finditer(text))
        if len(matches) < 2:
            return []

        actions: list[ScheduleAction] = []
        for index, match in enumerate(matches):
            segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segment = text[match.start():segment_end].strip(" 、，;；")
            date_text = normalize_date(match.group("value"))
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
                    confidence=0.72,
                    requires_review=True,
                )
            )
        return actions

    def _extract_date(self, text: str, current_date: str = "") -> str:
        match = DATE_TOKEN_RE.search(text)
        if match:
            return normalize_date(match.group("value"))
        compact_match = COMPACT_DATE_RE.search(text)
        if compact_match:
            return normalize_date(compact_match.group("value"))
        if current_date:
            inferred = self._infer_same_month_date(text, current_date)
            if inferred:
                return inferred
        return ""

    def _extract_time(self, text: str) -> str:
        match = TIME_TOKEN_RE.search(text)
        if not match:
            return ""
        normalized = normalize_time(match.group("value"))
        return normalized if re.fullmatch(r"\d{2}:\d{2}", normalized) else ""

    def _update_current_date(self, line: str, current_date: str) -> str:
        extracted = self._extract_date(line, current_date=current_date)
        return extracted or current_date

    def _expand_multi_day_line(self, line: str) -> list[ScheduleAction]:
        match = MULTI_DAY_LINE_RE.match(line)
        if not match:
            return []

        month = int(match.group("month"))
        day_values = [int(match.group("first_day"))]
        day_values.extend(int(token) for token in re.findall(r"\d{1,2}", match.group("more")))
        tail = match.group("tail").strip(" 、，,;；")
        if not tail:
            return []

        time_text = self._extract_time(tail)
        address = detect_address(tail)
        note = detect_note(tail)
        event = self._strip_multi_day_prefix(
            line,
            month=month,
            days=day_values,
            time_text=time_text,
        )
        if not event and not address and not note:
            return []

        actions: list[ScheduleAction] = []
        for day in day_values:
            actions.append(
                ScheduleAction(
                    action=ActionType.ADD,
                    date=f"{month}/{day}",
                    time=time_text,
                    event=event,
                    address=address,
                    note=note,
                    source_text=line,
                    confidence=0.84 if time_text else 0.78,
                    requires_review=True,
                )
            )
        return actions

    @staticmethod
    def _infer_same_month_date(text: str, current_date: str) -> str:
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", normalize_date(current_date))
        if not match:
            return ""
        month = int(match.group(1))
        day_match = DAY_ONLY_RE.match(text.strip())
        if not day_match:
            return ""
        day = int(day_match.group("day"))
        return f"{month}/{day}"

    def _strip_known_tokens(self, text: str, date_text: str, time_text: str) -> str:
        result = text
        if date_text:
            month, day = date_text.split("/")
            candidates = [
                f"{int(month)}/{int(day)}",
                f"{int(month):02d}{int(day):02d}",
                f"{int(month)}月{int(day)}日",
            ]
            for candidate in candidates:
                result = result.replace(candidate, " ")
        if time_text:
            hour = int(time_text.split(":")[0])
            minute = int(time_text.split(":")[1])
            alt_candidates = {
                time_text,
                f"{hour}:{minute:02d}",
                f"{hour}點" if minute == 0 else f"{hour}點{minute:02d}",
                f"{hour}點半" if minute == 30 else "",
                f"下午{hour - 12}:{minute:02d}" if hour > 12 else "",
                f"上午{hour}:{minute:02d}" if hour < 12 else "",
            }
            for candidate in alt_candidates:
                if candidate:
                    result = result.replace(candidate, " ")
        for token in DELETE_KEYWORDS + ADDRESS_LABELS + NOTE_LABELS + ("時間", "日期", "於", "在", "敬邀", "請", "辦理"):
            result = result.replace(token, " ")
        result = re.sub(r"[:：()（）]", " ", result)
        result = re.sub(r"\s+", " ", result).strip(" ，,;；")
        return result

    def _strip_multi_day_prefix(self, text: str, month: int, days: list[int], time_text: str) -> str:
        result = text
        for index, day in enumerate(days):
            if index == 0:
                result = result.replace(f"{month}/{day}", " ", 1)
            result = re.sub(rf"^[\s、,，]*{day}(?!\d)", " ", result, count=1)
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
            "你是台灣行程助理。請把使用者訊息拆成 JSON actions。"
            "日期一律輸出 M/D，例如 6/20。"
            "時間一律輸出 24 小時制 HH:MM，例如 下午4點輸出 16:00。"
            f"如果文字出現民國年，請用西元 {current_year} 年脈絡理解，但輸出只保留月日。"
            "action 只能是 add、delete、delete_day。"
            "刪除整天用 delete_day；刪除單筆用 delete。"
            "地址放 address，提醒或出席名單放 note。"
            "若有不確定內容，requires_review 設為 true。"
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

    def _merge(
        self,
        rule_actions: Iterable[ScheduleAction],
        ai_actions: Iterable[ScheduleAction],
    ) -> list[ScheduleAction]:
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
