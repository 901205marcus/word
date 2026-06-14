from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from docx import Document

from .models import ActionType, ApplySummary, ScheduleAction
from .parser import normalize_date, normalize_time

DAY_HEADER_PATTERN = re.compile(r"(\d{1,2})/(\d{1,2})[\(（]")


def cell_text(cell) -> str:
    return "\n".join(p.text.strip() for p in cell.paragraphs).strip()


def row_texts(table, row_idx: int) -> list[str]:
    return [cell_text(c) for c in table.rows[row_idx].cells]


def clear_cell(cell) -> None:
    for p in cell.paragraphs:
        for run in p.runs:
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
            raise ValueError("這個 Word 檔沒有表格")
        self.table = self.doc.tables[0]

    def save_as(self, output_path: str | Path) -> str:
        output_path = str(output_path)
        self.doc.save(output_path)
        return output_path

    def find_day_row(self, date_text: str) -> int | None:
        date_text = normalize_date(date_text)
        for idx in range(len(self.table.rows)):
            merged = " | ".join(row_texts(self.table, idx))
            if date_text in merged and DAY_HEADER_PATTERN.search(merged):
                return idx
        return None

    def find_next_day_row(self, start_row: int) -> int:
        for idx in range(start_row + 1, len(self.table.rows)):
            merged = " | ".join(row_texts(self.table, idx))
            if DAY_HEADER_PATTERN.search(merged):
                return idx
        return len(self.table.rows)

    def find_time_row(self, day_row: int, time_text: str) -> int | None:
        wanted = normalize_time(time_text)
        for idx in range(day_row + 1, self.find_next_day_row(day_row)):
            for text in row_texts(self.table, idx):
                if normalize_time(text) == wanted:
                    return idx
        return None

    def find_blank_row(self, day_row: int) -> int | None:
        for idx in range(day_row + 1, self.find_next_day_row(day_row)):
            texts = row_texts(self.table, idx)
            if DAY_HEADER_PATTERN.search(" | ".join(texts)):
                return None
            if "".join(texts).strip() == "":
                return idx
            if sum(1 for text in texts if text.strip()) <= 1:
                return idx
        return None

    def insert_row_before(self, row_idx: int, template_row_idx: int) -> int:
        new_tr = deepcopy(self.table.rows[template_row_idx]._tr)
        if row_idx >= len(self.table.rows):
            self.table._tbl.append(new_tr)
        else:
            self.table.rows[row_idx]._tr.addprevious(new_tr)
        for idx in range(len(self.table.rows)):
            if self.table.rows[idx]._tr is new_tr:
                return idx
        raise RuntimeError("插入列失敗")

    def add_action(self, action: ScheduleAction) -> str:
        day_row = self.find_day_row(action.date)
        if day_row is None:
            raise ValueError(f"找不到日期 {action.date}，這版 MVP 先不自動建立新日期")

        if action.time:
            time_row = self.find_time_row(day_row, action.time)
            if time_row is not None:
                self._fill_schedule_row(time_row, action)
                return f"已覆蓋 {action.date} {action.time}"

        blank_row = self.find_blank_row(day_row)
        if blank_row is not None:
            self._fill_schedule_row(blank_row, action)
            return f"已加入 {action.date}"

        next_day_row = self.find_next_day_row(day_row)
        template_row_idx = max(day_row + 1, next_day_row - 1)
        inserted = self.insert_row_before(next_day_row, template_row_idx)
        for cell in self.table.rows[inserted].cells:
            clear_cell(cell)
        self._fill_schedule_row(inserted, action)
        return f"已插入 {action.date}"

    def _fill_schedule_row(self, row_idx: int, action: ScheduleAction) -> None:
        row = self.table.rows[row_idx]
        if len(row.cells) > 1:
            set_cell_text(row.cells[1], action.time)
        if len(row.cells) > 2:
            set_cell_text(row.cells[2], action.event)
        if len(row.cells) > 3:
            set_cell_text(row.cells[3], action.address)
        if len(row.cells) > 4:
            set_cell_text(row.cells[4], action.note)

    def delete_action(self, action: ScheduleAction) -> bool:
        day_row = self.find_day_row(action.date)
        if day_row is None:
            return False
        if action.action == ActionType.DELETE_DAY:
            next_day_row = self.find_next_day_row(day_row)
            for tr in [self.table.rows[i]._tr for i in range(day_row, next_day_row)]:
                self.table._tbl.remove(tr)
            return True
        time_row = self.find_time_row(day_row, action.time)
        if time_row is None:
            return False
        for index in range(1, min(5, len(self.table.rows[time_row].cells))):
            set_cell_text(self.table.rows[time_row].cells[index], "")
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
                summary.skipped.append(f"{action.action.value}:{action.date}:{action.time}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"{self.path.stem}_line_mvp_{timestamp}{self.path.suffix}"
        summary.output_path = self.save_as(output_path)
        return summary
