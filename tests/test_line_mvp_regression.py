from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from docx import Document

from src.line_mvp.models import ActionType, InboxMessage, MessageStatus, ScheduleAction
from src.line_mvp.parser import MessageParser
from src.line_mvp.service import LineMVPService
from src.line_mvp.word_editor import ScheduleWordEditor


def build_template(path: Path, year: int, day_specs: list[tuple[int, int]]) -> None:
    doc = Document()
    doc.add_paragraph(f"{year}年")
    table = doc.add_table(rows=0, cols=5)
    for month, day in day_specs:
        header = table.add_row()
        header.cells[1].text = f"{month}/{day}（一）"
        blank = table.add_row()
        blank.cells[1].text = ""
        blank.cells[2].text = ""
        blank.cells[3].text = ""
        blank.cells[4].text = ""
    doc.save(path)


class WordEditorRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="line-mvp-word-"))
        self.today = datetime.now().date()
        self.doc_year = self.today.year

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def make_editor(self, day_specs: list[tuple[int, int]]) -> ScheduleWordEditor:
        path = self.tempdir / "template.docx"
        build_template(path, self.doc_year, day_specs)
        return ScheduleWordEditor(path)

    def test_autofix_prunes_past_days(self) -> None:
        yesterday = self.today.replace(day=max(1, self.today.day - 1))
        editor = self.make_editor(
            [
                (yesterday.month, yesterday.day),
                (self.today.month, self.today.day),
            ]
        )
        messages = editor.autofix_template([])
        headers = [h.date_text for h in editor.list_day_headers()]
        self.assertNotIn(f"{yesterday.month}/{yesterday.day}", headers)
        self.assertIn(f"{self.today.month}/{self.today.day}", headers)
        self.assertTrue(any("刪除過期日期區塊" in message for message in messages))

    def test_autofix_fills_two_month_range(self) -> None:
        editor = self.make_editor([(self.today.month, self.today.day)])
        editor.autofix_template([])
        headers = {h.date_text for h in editor.list_day_headers()}
        self.assertIn(f"{self.today.month}/{self.today.day}", headers)
        future = editor._add_months(editor._today_in_document_year(), 2)
        self.assertIn(f"{future.month}/{future.day}", headers)

    def test_autofix_adds_extra_future_action_dates_only(self) -> None:
        editor = self.make_editor([(self.today.month, self.today.day)])
        far_future = editor._add_months(editor._today_in_document_year(), 4)
        actions = [
            ScheduleAction(
                action=ActionType.ADD,
                date=f"{far_future.month}/{far_future.day}",
                event="遠期行程",
            )
        ]
        editor.autofix_template(actions)
        headers = {h.date_text for h in editor.list_day_headers()}
        self.assertIn(f"{far_future.month}/{far_future.day}", headers)
        missing_gap = far_future - editor._add_months(editor._today_in_document_year(), 2)
        if missing_gap.days > 1:
            gap_day = far_future.replace(day=max(1, far_future.day - 1))
            self.assertNotIn(f"{gap_day.month}/{gap_day.day}", headers)

    def test_add_action_sorts_by_time(self) -> None:
        editor = self.make_editor([(self.today.month, self.today.day)])
        editor.add_action(
            ScheduleAction(action=ActionType.ADD, date=f"{self.today.month}/{self.today.day}", time="17:00", event="晚場")
        )
        editor.add_action(
            ScheduleAction(action=ActionType.ADD, date=f"{self.today.month}/{self.today.day}", time="09:00", event="早場")
        )
        day_row = editor.find_day_row(f"{self.today.month}/{self.today.day}")
        self.assertIsNotNone(day_row)
        entries = editor._read_day_entries(day_row)
        self.assertEqual(["09:00", "17:00"], [entry["time"] for entry in entries])

    def test_delete_single_action(self) -> None:
        editor = self.make_editor([(self.today.month, self.today.day)])
        date_text = f"{self.today.month}/{self.today.day}"
        editor.add_action(ScheduleAction(action=ActionType.ADD, date=date_text, time="10:00", event="A"))
        editor.add_action(ScheduleAction(action=ActionType.ADD, date=date_text, time="11:00", event="B"))
        ok = editor.delete_action(ScheduleAction(action=ActionType.DELETE, date=date_text, time="10:00"))
        self.assertTrue(ok)
        day_row = editor.find_day_row(date_text)
        entries = editor._read_day_entries(day_row)
        self.assertEqual(["11:00"], [entry["time"] for entry in entries])

    def test_delete_day_action(self) -> None:
        tomorrow = self.today
        editor = self.make_editor([(tomorrow.month, tomorrow.day)])
        ok = editor.delete_action(ScheduleAction(action=ActionType.DELETE_DAY, date=f"{tomorrow.month}/{tomorrow.day}"))
        self.assertTrue(ok)
        self.assertEqual([], editor.list_day_headers())

    def test_apply_runs_multiple_add_and_delete_rounds(self) -> None:
        editor = self.make_editor([(self.today.month, self.today.day)])
        date_text = f"{self.today.month}/{self.today.day}"
        actions = [
            ScheduleAction(action=ActionType.ADD, date=date_text, time="16:00", event="A"),
            ScheduleAction(action=ActionType.ADD, date=date_text, time="08:00", event="B"),
            ScheduleAction(action=ActionType.DELETE, date=date_text, time="16:00"),
        ]
        summary = editor.apply(actions, self.tempdir / "outputs")
        self.assertTrue(summary.output_path)
        day_row = editor.find_day_row(date_text)
        entries = editor._read_day_entries(day_row)
        self.assertEqual(["08:00"], [entry["time"] for entry in entries])


class ServiceRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="line-mvp-service-"))
        (self.tempdir / "data" / "line_mvp").mkdir(parents=True, exist_ok=True)
        (self.tempdir / "outputs").mkdir(parents=True, exist_ok=True)
        self.service = LineMVPService(self.tempdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def make_item(self) -> InboxMessage:
        item = self.service.store.add_message("user-1", "manual", "7/20 10:00 測試行程")
        item.template_docx_path = str(self.tempdir / "fake.docx")
        Path(item.template_docx_path).write_bytes(b"fake")
        self.service.store.update_message(item)
        return item

    def test_reply_falls_back_to_push(self) -> None:
        with patch.object(self.service, "_reply_line_message", return_value=False), patch.object(
            self.service, "_push_text_message", return_value=True
        ) as push_mock:
            result = self.service.ingest_webhook(
                {
                    "events": [
                        {
                            "type": "message",
                            "replyToken": "token",
                            "source": {"userId": "U1", "type": "user"},
                            "message": {"type": "text", "text": "hello"},
                        }
                    ]
                }
            )
        self.assertEqual("true", result["replies"][0]["ok"])
        push_mock.assert_called()

    def test_recover_pending_jobs_starts_worker(self) -> None:
        item = self.make_item()
        item.processing = True
        self.service.store.update_message(item)
        started: list[str] = []
        with patch.object(self.service, "_start_apply_job", side_effect=lambda sender_id, message_id: started.append(message_id)):
            recovered = self.service.recover_pending_jobs()
        self.assertEqual(1, recovered)
        self.assertEqual([item.id], started)

    def test_run_apply_job_retries_then_succeeds(self) -> None:
        item = self.make_item()
        item.processing = True
        self.service.store.update_message(item)
        calls = {"count": 0}

        def fake_apply(message_id: str, docx_path=None, uploaded_filename=None, uploaded_content=None):
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("boom")
            latest = self.service._require(message_id)
            latest.status = MessageStatus.APPLIED
            latest.processing = False
            latest.output_path = "C:/tmp/out.docx"
            latest.preview_paths = []
            return latest

        pushed: list[str] = []
        with patch.object(self.service, "apply_to_word", side_effect=fake_apply), patch.object(
            self.service, "_push_apply_assets", return_value=True
        ), patch.object(self.service, "_push_text_message", side_effect=lambda to, text: pushed.append(text) or True), patch(
            "src.line_mvp.service.time.sleep", return_value=None
        ):
            self.service._run_apply_job("user-1", item.id)

        latest = self.service._require(item.id)
        self.assertEqual(3, calls["count"])
        self.assertFalse(latest.processing)
        self.assertTrue(any("自動重試" in text for text in pushed))


class ParserRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = MessageParser(openai_enabled=False)

    def test_multi_day_same_event(self) -> None:
        actions = self.parser.parse("6/18、19宿沙里仙")
        self.assertEqual(2, len(actions))
        self.assertEqual(["6/18", "6/19"], [action.date for action in actions])
        self.assertTrue(all("宿沙里仙" in action.event for action in actions))

    def test_afternoon_time_normalization(self) -> None:
        actions = self.parser.parse("4/20 下午3點開會，地點市政府，請安排")
        self.assertEqual(1, len(actions))
        self.assertEqual("15:00", actions[0].time)
        self.assertEqual("4/20", actions[0].date)

    def test_delete_day_parse(self) -> None:
        actions = self.parser.parse("4/9刪除")
        self.assertEqual(1, len(actions))
        self.assertEqual(ActionType.DELETE_DAY, actions[0].action)
        self.assertEqual("4/9", actions[0].date)

    def test_time_range_uses_start_time(self) -> None:
        actions = self.parser.parse("6/18下午5:30-6:00報到，地點水里鄉")
        self.assertEqual(1, len(actions))
        self.assertEqual("17:30", actions[0].time)

    def test_english_event_kept(self) -> None:
        actions = self.parser.parse("8/29 19:00 is full新書發表會")
        self.assertEqual(1, len(actions))
        self.assertEqual("19:00", actions[0].time)
        self.assertIn("is full新書發表會", actions[0].event)


if __name__ == "__main__":
    unittest.main()
