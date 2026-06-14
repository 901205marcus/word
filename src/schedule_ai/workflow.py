from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from .config import AssistantConfig
from .docx_editor import ScheduleDocEditor
from .models import ApplyResult, ScheduleAction
from .parser import MessageParser

ReviewCallback = Callable[[list[ScheduleAction]], set[int]]


class ScheduleAssistant:
    def __init__(self, config: Optional[AssistantConfig] = None):
        self.config = config or AssistantConfig()
        self.parser = MessageParser(self.config)

    def parse_messages(self, messages: list[str]) -> list[ScheduleAction]:
        actions: list[ScheduleAction] = []
        for message in messages:
            actions.extend(self.parser.parse(message))
        return actions

    def run(
        self,
        input_docx_path: str,
        messages: list[str],
        output_docx_path: Optional[str] = None,
        parse_review_callback: Optional[ReviewCallback] = None,
        apply_review_callback: Optional[ReviewCallback] = None,
    ) -> tuple[str, list[ScheduleAction], ApplyResult]:
        editor = ScheduleDocEditor.from_path(input_docx_path, self.config)
        actions = self.parse_messages(messages)

        if self.config.manual_parse_review and parse_review_callback:
            approved_indexes = parse_review_callback(actions)
            actions = [action for idx, action in enumerate(actions) if idx in approved_indexes]

        approved_for_apply = None
        if self.config.manual_apply_review and apply_review_callback:
            approved_for_apply = apply_review_callback(actions)

        result = editor.apply_actions(actions, approved_for_apply)

        if self.config.prune_past_days:
            removed = editor.prune_past_days(self.config.today)
            for item in removed:
                result.messages.append(f"已清除過去日期 {item}")

        output_path = output_docx_path or self._default_output_path(input_docx_path)
        editor.save(output_path)
        return output_path, actions, result

    def complete_month(
        self,
        input_docx_path: str,
        month: int,
        year: Optional[int] = None,
        output_docx_path: Optional[str] = None,
    ) -> tuple[str, list[str]]:
        editor = ScheduleDocEditor.from_path(input_docx_path, self.config)
        inserted = editor.complete_month(month=month, year=year)
        output_path = output_docx_path or self._default_output_path(input_docx_path)
        editor.save(output_path)
        return output_path, inserted

    @staticmethod
    def _default_output_path(input_docx_path: str) -> str:
        path = Path(input_docx_path)
        return str(path.with_name(f"{path.stem}_updated{path.suffix}"))

    @staticmethod
    def preview(actions: list[ScheduleAction]) -> list[dict]:
        return [asdict(action) for action in actions]
