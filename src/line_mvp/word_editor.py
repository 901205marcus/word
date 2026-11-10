from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from docx import Document

from .models import ActionType, ApplySummary, ScheduleAction
from .parser import normalize_date, normalize_time, time_sort_key

DAY_HEADER_RE = re.compile(r"(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:\s*[（(]?(?P<weekday>[一二三四五六日])?[）)])?")
YEAR_RE = re.compile(r"(20\d{2})年")
ROC_YEAR_RE = re.compile(r"(?<!\d)(1\d{2})年")
WEEKDAY_TEXT = "一二三四五六日"


@dataclass(slots=True)
class DayHeader:
    row_idx: int
    year: int
    month: int
    day: int
    raw_text: str

    @property
    def date_text(self) -> str:
        return f"{self.month}/{self.day}"


def cell_text(cell) -> str:
    return "\n".join(p.text.strip() for p in cell.paragraphs).strip()


def row_texts(table, row_idx: int) -> list[str]:
    return [cell_text(cell) for cell in table.rows[row_idx].cells]


def clear_cell(cell) -> None:
    for paragraph in cell.paragraphs:
        for run in paragraph.runs:
            run.text = ""
    if not cell.paragraphs:
        cell.add_paragraph("")


def set_cell_text(cell, text: str) -> None:
    clear_cell(cell)
    if cell.paragraphs:
        paragraph = cell.paragraphs[0]
        if paragraph.runs:
            paragraph.runs[0].text = str(text)
        else:
            paragraph.add_run(str(text))
    else:
        cell.add_paragraph(str(text))


class ScheduleWordEditor:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.doc = Document(str(self.path))
        if not self.doc.tables:
            raise ValueError("Word 檔內沒有表格，無法處理行程。")
        self.table = self.doc.tables[0]
        self.document_year = self._detect_document_year()

    def save_as(self, output_path: str | Path) -> str:
        self.doc.save(str(output_path))
        return str(output_path)

    def apply(self, actions: list[ScheduleAction], output_dir: str | Path) -> ApplySummary:
        summary = ApplySummary()
        summary.messages.extend(self.autofix_template(actions))

        for action in actions:
            try:
                if action.action == ActionType.ADD:
                    summary.messages.append(self.add_action(action))
                    summary.applied.append(f"{action.action.value}:{action.date}:{action.time}:{action.event}")
                else:
                    ok = self.delete_action(action)
                    tag = f"{action.action.value}:{action.date}:{action.time}:{action.event}"
                    if ok:
                        summary.messages.append(f"已刪除 {action.date} {action.time}".strip())
                        summary.applied.append(tag)
                    else:
                        summary.messages.append(f"找不到可刪除目標：{action.date} {action.time}".strip())
                        summary.skipped.append(tag)
            except Exception as exc:
                summary.messages.append(str(exc))
                summary.skipped.append(f"{action.action.value}:{action.date}:{action.time}:{action.event}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"{self.path.stem}_line_mvp_{timestamp}{self.path.suffix}"
        summary.output_path = self.save_as(output_path)
        return summary

    def autofix_template(self, actions: list[ScheduleAction] | None = None) -> list[str]:
        actions = actions or []
        messages: list[str] = []

        removed = self.prune_past_days()
        if removed:
            messages.append(f"已刪除過期日期區塊 {removed} 筆")

        self.correct_all_header_weekdays(messages)

        today = self._today_in_document_year()
        planning_end = self._add_months(today, 2)

        inserted = self.ensure_date_range(today, planning_end)
        if inserted:
            messages.append(
                f"已補齊兩個月日期區間：{today.month}/{today.day} 到 {planning_end.month}/{planning_end.day}，新增 {len(inserted)} 天"
            )

        extra_inserted: list[str] = []
        for action_date in sorted(self._collect_action_dates(actions)):
            if action_date <= planning_end:
                continue
            if self.find_day_row(self._format_mmdd(action_date)) is None:
                self.insert_isolated_day_block(action_date)
                extra_inserted.append(self._format_mmdd(action_date))
        if extra_inserted:
            messages.append(f"已補上超出兩個月範圍的行程日期：{', '.join(extra_inserted)}")

        self.correct_all_header_weekdays(messages)
        return self._dedupe_messages(messages)

    def list_day_headers(self) -> list[DayHeader]:
        headers: list[DayHeader] = []
        inferred_year = self.document_year
        previous_month: int | None = None
        for row_idx in range(len(self.table.rows)):
            text = " | ".join(row_texts(self.table, row_idx))
            match = DAY_HEADER_RE.search(text)
            if not match:
                continue
            month = int(match.group("month"))
            if previous_month is not None and month < previous_month:
                inferred_year += 1
            headers.append(
                DayHeader(
                    row_idx=row_idx,
                    year=inferred_year,
                    month=month,
                    day=int(match.group("day")),
                    raw_text=text,
                )
            )
            previous_month = month
        return headers

    def find_day_row(self, date_text: str) -> int | None:
        target = normalize_date(date_text)
        for header in self.list_day_headers():
            if header.date_text == target:
                return header.row_idx
        return None

    def find_next_day_row(self, start_row: int) -> int:
        for idx in range(start_row + 1, len(self.table.rows)):
            if DAY_HEADER_RE.search(" | ".join(row_texts(self.table, idx))):
                return idx
        return len(self.table.rows)

    def add_action(self, action: ScheduleAction) -> str:
        day_row = self.ensure_day_row(action.date)
        entries = self._read_day_entries(day_row)
        normalized_time = normalize_time(action.time)
        replaced = False

        for entry in entries:
            if normalized_time and entry["time"] == normalized_time:
                entry["time"] = normalized_time
                entry["event"] = action.event
                entry["address"] = action.address
                entry["note"] = action.note
                replaced = True
                break

        if not replaced:
            entries.append(
                {
                    "time": normalized_time,
                    "event": action.event,
                    "address": action.address,
                    "note": action.note,
                }
            )

        self._write_day_entries(day_row, entries)
        return f"{'已覆蓋' if replaced else '已加入'} {action.date} {normalized_time or action.event}".strip()

    def delete_action(self, action: ScheduleAction) -> bool:
        day_row = self.find_day_row(action.date)
        if day_row is None:
            return False

        if action.action == ActionType.DELETE_DAY:
            self._remove_day_block(day_row)
            return True

        target_time = normalize_time(action.time)
        entries = self._read_day_entries(day_row)
        kept = [entry for entry in entries if entry["time"] != target_time]
        if len(kept) == len(entries):
            return False
        self._write_day_entries(day_row, kept)
        return True

    def ensure_day_row(self, date_text: str) -> int:
        normalized = normalize_date(date_text)
        existing = self.find_day_row(normalized)
        if existing is not None:
            return existing

        target_date = self._date_in_document_year(normalized)
        self.insert_isolated_day_block(target_date)
        created = self.find_day_row(normalized)
        if created is None:
            raise ValueError(f"無法建立日期區塊：{normalized}")
        return created

    def prune_past_days(self) -> int:
        today = self._today_in_document_year()
        removed = 0

        headers = self.list_day_headers()
        for header in reversed(headers):
            if self._header_date(header) < today:
                self._remove_day_block(header.row_idx)
                removed += 1
        return removed

    def ensure_date_range(self, start_date: date, end_date: date) -> list[str]:
        inserted: list[str] = []
        cursor = start_date
        while cursor <= end_date:
            if self.find_day_row(self._format_mmdd(cursor)) is None:
                self.insert_isolated_day_block(cursor)
                inserted.append(self._format_mmdd(cursor))
            cursor += timedelta(days=1)
        return inserted

    def insert_isolated_day_block(self, target_date: date) -> int:
        existing = self.find_day_row(self._format_mmdd(target_date))
        if existing is not None:
            return existing

        previous_header, next_header = self._find_neighbor_headers(target_date)
        template_header = previous_header or next_header
        if template_header is None:
            raise ValueError("Word 模板中找不到任何可複製的日期區塊。")

        insert_before = next_header.row_idx if next_header else len(self.table.rows)
        return self._clone_day_block(template_header.row_idx, insert_before, target_date)

    def correct_all_header_weekdays(self, messages: list[str]) -> None:
        for header in self.list_day_headers():
            corrected = self._correct_header_weekday(header.row_idx)
            if corrected:
                messages.append(corrected)

    def _read_day_entries(self, day_row: int) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for row_idx in range(day_row + 1, self.find_next_day_row(day_row)):
            texts = row_texts(self.table, row_idx)
            time_text = normalize_time(texts[1] if len(texts) > 1 else "")
            event = texts[2].strip() if len(texts) > 2 else ""
            address = texts[3].strip() if len(texts) > 3 else ""
            note = texts[4].strip() if len(texts) > 4 else ""
            if not any((time_text, event, address, note)):
                continue
            entries.append(
                {
                    "time": time_text,
                    "event": event,
                    "address": address,
                    "note": note,
                }
            )
        entries.sort(key=lambda item: (time_sort_key(item["time"]), item["event"]))
        return entries

    def _write_day_entries(self, day_row: int, entries: list[dict[str, str]]) -> None:
        next_day_row = self.find_next_day_row(day_row)
        row_indexes = list(range(day_row + 1, next_day_row))
        if not row_indexes:
            raise ValueError("日期區塊缺少可寫入的行程列。")

        while len(row_indexes) < len(entries):
            template_row_idx = row_indexes[-1]
            inserted_idx = self._insert_row_before(next_day_row, template_row_idx)
            row_indexes.append(inserted_idx)
            next_day_row += 1

        for idx, entry in zip(row_indexes, entries):
            self._fill_schedule_row(idx, entry)

        for idx in row_indexes[len(entries) :]:
            row = self.table.rows[idx]
            for cell_index in range(1, min(5, len(row.cells))):
                set_cell_text(row.cells[cell_index], "")

    def _fill_schedule_row(self, row_idx: int, entry: dict[str, str]) -> None:
        row = self.table.rows[row_idx]
        if len(row.cells) > 1:
            set_cell_text(row.cells[1], entry["time"])
        if len(row.cells) > 2:
            set_cell_text(row.cells[2], entry["event"])
        if len(row.cells) > 3:
            set_cell_text(row.cells[3], entry["address"])
        if len(row.cells) > 4:
            set_cell_text(row.cells[4], entry["note"])

    def _clone_day_block(self, template_day_row: int, insert_before: int, target_date: date) -> int:
        block_size = self.find_next_day_row(template_day_row) - template_day_row
        if block_size <= 0:
            raise ValueError("找不到可複製的日期區塊內容。")

        template_rows = [deepcopy(self.table.rows[template_day_row + offset]._tr) for offset in range(block_size)]
        inserted_rows: list[int] = []

        if insert_before >= len(self.table.rows):
            for new_tr in template_rows:
                self.table._tbl.append(new_tr)
                inserted_rows.append(self._locate_row(new_tr))
        else:
            anchor = self.table.rows[insert_before]._tr
            previous = anchor
            for index, new_tr in enumerate(template_rows):
                if index == 0:
                    previous.addprevious(new_tr)
                else:
                    previous.addnext(new_tr)
                previous = new_tr
                inserted_rows.append(self._locate_row(new_tr))

        header_row_idx = inserted_rows[0]
        self._set_day_header_text(header_row_idx, target_date)
        for row_idx in inserted_rows[1:]:
            row = self.table.rows[row_idx]
            for cell_index in range(1, min(5, len(row.cells))):
                set_cell_text(row.cells[cell_index], "")
        return header_row_idx

    def _insert_row_before(self, row_idx: int, template_row_idx: int) -> int:
        new_tr = deepcopy(self.table.rows[template_row_idx]._tr)
        if row_idx >= len(self.table.rows):
            self.table._tbl.append(new_tr)
        else:
            self.table.rows[row_idx]._tr.addprevious(new_tr)
        return self._locate_row(new_tr)

    def _remove_day_block(self, day_row: int) -> None:
        next_day_row = self.find_next_day_row(day_row)
        trs = [self.table.rows[i]._tr for i in range(day_row, next_day_row)]
        for tr in trs:
            self.table._tbl.remove(tr)

    def _find_neighbor_headers(self, target_date: date) -> tuple[DayHeader | None, DayHeader | None]:
        previous_header: DayHeader | None = None
        next_header: DayHeader | None = None
        for header in self.list_day_headers():
            header_date = self._header_date(header)
            if header_date < target_date:
                previous_header = header
                continue
            if header_date > target_date:
                next_header = header
                break
        return previous_header, next_header

    def _header_date(self, header: DayHeader) -> date:
        return date(header.year, header.month, header.day)

    def _set_day_header_text(self, row_idx: int, target_date: date) -> None:
        row = self.table.rows[row_idx]
        header_text = self._build_header_text(target_date)
        replaced = False
        for cell in row.cells:
            original = cell_text(cell)
            if not original:
                continue
            if DAY_HEADER_RE.search(original):
                updated = DAY_HEADER_RE.sub(header_text, original, count=1)
                set_cell_text(cell, updated)
                replaced = True
                break

        if not replaced:
            target_index = 1 if len(row.cells) > 1 else 0
            set_cell_text(row.cells[target_index], header_text)

    def _correct_header_weekday(self, row_idx: int) -> str:
        current_date = self._extract_header_date(row_idx)
        if current_date is None:
            return ""

        expected = self._build_header_text(current_date)
        row = self.table.rows[row_idx]
        for cell in row.cells:
            original = cell_text(cell)
            if not original or not DAY_HEADER_RE.search(original):
                continue
            updated = DAY_HEADER_RE.sub(expected, original, count=1)
            if updated == original:
                return ""
            set_cell_text(cell, updated)
            return f"已校正星期：{self._format_mmdd(current_date)}"
        return ""

    def _extract_header_date(self, row_idx: int) -> date | None:
        for header in self.list_day_headers():
            if header.row_idx == row_idx:
                return self._header_date(header)
        return None

    def _locate_row(self, target_tr) -> int:
        for idx in range(len(self.table.rows)):
            if self.table.rows[idx]._tr is target_tr:
                return idx
        raise RuntimeError("找不到新插入的列。")

    def _collect_action_dates(self, actions: list[ScheduleAction]) -> set[date]:
        collected: set[date] = set()
        for action in actions:
            normalized = normalize_date(action.date)
            if not normalized or "/" not in normalized:
                continue
            try:
                collected.add(self._date_in_document_year(normalized))
            except ValueError:
                continue
        return collected

    def _detect_document_year(self) -> int:
        texts = [paragraph.text for paragraph in self.doc.paragraphs]
        for row in self.table.rows:
            for cell in row.cells:
                texts.append(cell_text(cell))

        merged = "\n".join(texts)
        ad_match = YEAR_RE.search(merged)
        if ad_match:
            return int(ad_match.group(1))

        roc_match = ROC_YEAR_RE.search(merged)
        if roc_match:
            return int(roc_match.group(1)) + 1911

        return datetime.now().year

    def _date_in_document_year(self, value: str) -> date:
        normalized = normalize_date(value)
        month_text, day_text = normalized.split("/", 1)
        month = int(month_text)
        day = int(day_text)
        today = datetime.now().date()
        candidate = date(today.year, month, day)
        if candidate < today and month < today.month:
            candidate = date(today.year + 1, month, day)
        return candidate

    def _today_in_document_year(self) -> date:
        return datetime.now().date()

    def _add_months(self, current: date, months: int) -> date:
        month_index = current.month - 1 + months
        year_value = current.year + month_index // 12
        month_value = month_index % 12 + 1
        day_value = min(current.day, self._month_last_day(year_value, month_value))
        return date(year_value, month_value, day_value)

    def _build_header_text(self, target_date: date) -> str:
        weekday = WEEKDAY_TEXT[target_date.weekday()]
        return f"{target_date.month}/{target_date.day}（{weekday}）"

    @staticmethod
    def _format_mmdd(value: date) -> str:
        return f"{value.month}/{value.day}"

    @staticmethod
    def _month_last_day(year_value: int, month_value: int) -> int:
        if month_value == 12:
            next_month = date(year_value + 1, 1, 1)
        else:
            next_month = date(year_value, month_value + 1, 1)
        return (next_month - timedelta(days=1)).day

    @staticmethod
    def _dedupe_messages(messages: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for message in messages:
            text = message.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result
