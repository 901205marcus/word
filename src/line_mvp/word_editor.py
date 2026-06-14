from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from docx import Document

from .models import ActionType, ApplySummary, ScheduleAction
from .parser import normalize_date, normalize_time, time_sort_key

DAY_HEADER_PATTERN = re.compile(r"(\d{1,2})/(\d{1,2})[\(（]")


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
        day_row = self.find_day_row(action.date)
        if day_row is None:
            raise ValueError(f"找不到日期 {action.date}，目前 MVP 還不會自動新增整天區塊。")

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
        for idx in range(len(self.table.rows)):
            if self.table.rows[idx]._tr is new_tr:
                return idx
        raise RuntimeError("插入新列失敗。")
