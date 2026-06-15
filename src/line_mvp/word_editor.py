from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path

from docx import Document

from .models import ActionType, ApplySummary, ScheduleAction
from .parser import normalize_date, normalize_time, time_sort_key

DAY_HEADER_PATTERN = re.compile(r"(\d{1,2})/(\d{1,2})(?:[\(（]|$)")


def cell_text(cell) -> str:
    return "\n".join(paragraph.text.strip() for paragraph in cell.paragraphs).strip()


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
            raise ValueError("Word 檔沒有表格，這版目前無法處理。")
        self.table = self.doc.tables[0]

    def save_as(self, output_path: str | Path) -> str:
        output = str(output_path)
        self.doc.save(output)
        return output

    def find_day_row(self, date_text: str) -> int | None:
        target = normalize_date(date_text)
        for idx in range(len(self.table.rows)):
            merged = " | ".join(row_texts(self.table, idx))
            if target in merged and DAY_HEADER_PATTERN.search(merged):
                return idx
        return None

    def find_next_day_row(self, start_row: int) -> int:
        for idx in range(start_row + 1, len(self.table.rows)):
            merged = " | ".join(row_texts(self.table, idx))
            if DAY_HEADER_PATTERN.search(merged):
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
        if replaced:
            return f"已更新 {action.date} {normalized_time or action.event}"
        return f"已加入 {action.date} {normalized_time or action.event}"

    def ensure_day_row(self, date_text: str) -> int:
        normalized_target = normalize_date(date_text)
        existing = self.find_day_row(normalized_target)
        if existing is not None:
            return existing

        day_rows = self._day_rows()
        if not day_rows:
            raise ValueError("Word 表格裡找不到任何日期標題列，無法自動補日期。")

        blocks = [
            {
                "row": row_idx,
                "date_text": self._extract_header_date(row_idx),
                "date_obj": self._as_date(self._extract_header_date(row_idx)),
            }
            for row_idx in day_rows
        ]
        blocks = [block for block in blocks if block["date_text"]]
        if not blocks:
            raise ValueError("Word 表格裡找不到可辨識的日期標題列，無法自動補日期。")

        target_date = self._as_date(normalized_target)

        previous_block = None
        next_block = None
        for block in blocks:
            block_date = block["date_obj"]
            if block_date < target_date:
                previous_block = block
                continue
            if block_date > target_date:
                next_block = block
                break

        if previous_block and next_block:
            self._fill_gap_after_block(previous_block["row"], target_date, stop_before=next_block["row"])
        elif previous_block:
            self._fill_gap_after_block(previous_block["row"], target_date)
        elif next_block:
            self._fill_gap_before_block(next_block["row"], target_date)
        else:
            raise ValueError("找不到可複製的日期模板。")

        created = self.find_day_row(normalized_target)
        if created is None:
            raise ValueError(f"已嘗試補上 {normalized_target}，但仍找不到該日期列。")
        return created

    def delete_action(self, action: ScheduleAction) -> bool:
        day_row = self.find_day_row(action.date)
        if day_row is None:
            return False

        if action.action == ActionType.DELETE_DAY:
            next_day_row = self.find_next_day_row(day_row)
            for tr in [self.table.rows[i]._tr for i in range(day_row, next_day_row)]:
                self.table._tbl.remove(tr)
            return True

        target_time = normalize_time(action.time)
        entries = self._read_day_entries(day_row)
        kept = [entry for entry in entries if entry["time"] != target_time]
        if len(kept) == len(entries):
            return False
        self._write_day_entries(day_row, kept)
        return True

    def apply(self, actions: list[ScheduleAction], output_dir: str | Path) -> ApplySummary:
        summary = ApplySummary()
        for action in actions:
            try:
                if action.action == ActionType.ADD:
                    summary.messages.append(self.add_action(action))
                    summary.applied.append(f"{action.action.value}:{action.date}:{action.time}:{action.event}")
                else:
                    ok = self.delete_action(action)
                    if ok:
                        summary.messages.append(f"已刪除 {action.date} {action.time}".strip())
                        summary.applied.append(f"{action.action.value}:{action.date}:{action.time}")
                    else:
                        summary.messages.append(f"找不到 {action.date} {action.time}".strip())
                        summary.skipped.append(f"{action.action.value}:{action.date}:{action.time}")
            except Exception as exc:
                summary.messages.append(str(exc))
                summary.skipped.append(f"{action.action.value}:{action.date}:{action.time}:{action.event}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"{self.path.stem}_line_mvp_{timestamp}{self.path.suffix}"
        summary.output_path = self.save_as(output_path)
        return summary

    def _fill_gap_after_block(self, start_row: int, target_date: date, stop_before: int | None = None) -> None:
        current_text = self._extract_header_date(start_row)
        current_date = self._as_date(current_text)
        anchor_row = self.find_next_day_row(start_row) if stop_before is None else stop_before
        template_row = start_row

        next_date = current_date + timedelta(days=1)
        while next_date <= target_date:
            inserted = self._clone_day_block(template_row, anchor_row, self._format_date(next_date))
            template_row = inserted
            anchor_row = self.find_next_day_row(inserted)
            next_date += timedelta(days=1)

    def _fill_gap_before_block(self, next_row: int, target_date: date) -> None:
        next_text = self._extract_header_date(next_row)
        next_date = self._as_date(next_text)
        missing_dates: list[date] = []

        cursor = target_date
        while cursor < next_date:
            missing_dates.append(cursor)
            cursor += timedelta(days=1)

        if not missing_dates:
            return

        template_row = next_row
        anchor_row = next_row
        for missing_date in reversed(missing_dates):
            inserted = self._clone_day_block(template_row, anchor_row, self._format_date(missing_date))
            template_row = inserted
            anchor_row = inserted

    def _clone_day_block(self, template_day_row: int, insert_before: int, date_text: str) -> int:
        block_size = self.find_next_day_row(template_day_row) - template_day_row
        if block_size <= 0:
            raise ValueError("日期區塊格式異常，無法複製日期模板。")

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
        self._set_day_header_text(header_row_idx, normalize_date(date_text))
        for row_idx in inserted_rows[1:]:
            row = self.table.rows[row_idx]
            for cell_index in range(1, min(5, len(row.cells))):
                set_cell_text(row.cells[cell_index], "")
        return header_row_idx

    def _read_day_entries(self, day_row: int) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for row_idx in range(day_row + 1, self.find_next_day_row(day_row)):
            texts = row_texts(self.table, row_idx)
            if DAY_HEADER_PATTERN.search(" | ".join(texts)):
                break
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
            raise ValueError("日期區塊內沒有可用列。")

        while len(row_indexes) < len(entries):
            template_row_idx = row_indexes[-1]
            inserted_idx = self._insert_row_before(next_day_row, template_row_idx)
            row_indexes.append(inserted_idx)
            next_day_row += 1

        for idx, entry in zip(row_indexes, entries):
            self._fill_schedule_row(idx, entry)

        for idx in row_indexes[len(entries):]:
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

    def _insert_row_before(self, row_idx: int, template_row_idx: int) -> int:
        new_tr = deepcopy(self.table.rows[template_row_idx]._tr)
        if row_idx >= len(self.table.rows):
            self.table._tbl.append(new_tr)
        else:
            self.table.rows[row_idx]._tr.addprevious(new_tr)
        return self._locate_row(new_tr)

    def _day_rows(self) -> list[int]:
        rows: list[int] = []
        for idx in range(len(self.table.rows)):
            merged = " | ".join(row_texts(self.table, idx))
            if DAY_HEADER_PATTERN.search(merged):
                rows.append(idx)
        return rows

    def _extract_header_date(self, row_idx: int) -> str:
        for text in row_texts(self.table, row_idx):
            match = DAY_HEADER_PATTERN.search(text)
            if match:
                return f"{int(match.group(1))}/{int(match.group(2))}"
        return ""

    def _set_day_header_text(self, row_idx: int, date_text: str) -> None:
        row = self.table.rows[row_idx]
        normalized = normalize_date(date_text)
        for cell in row.cells:
            original = cell_text(cell)
            if DAY_HEADER_PATTERN.search(original):
                updated = re.sub(r"\d{1,2}/\d{1,2}", normalized, original, count=1)
                set_cell_text(cell, updated)
                return
        target_index = 1 if len(row.cells) > 1 else 0
        set_cell_text(row.cells[target_index], f"{normalized}( )")

    def _locate_row(self, target_tr) -> int:
        for idx in range(len(self.table.rows)):
            if self.table.rows[idx]._tr is target_tr:
                return idx
        raise RuntimeError("找不到剛插入的列。")

    @staticmethod
    def _date_sort_key(value: str) -> tuple[int, int]:
        normalized = normalize_date(value)
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", normalized)
        if not match:
            return (99, 99)
        return int(match.group(1)), int(match.group(2))

    @staticmethod
    def _as_date(value: str) -> date:
        normalized = normalize_date(value)
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", normalized)
        if not match:
            raise ValueError(f"無法辨識日期格式：{value}")
        month = int(match.group(1))
        day = int(match.group(2))
        return date(2000, month, day)

    @staticmethod
    def _format_date(value: date) -> str:
        return f"{value.month}/{value.day}"
