from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from docx import Document
from PIL import Image

from src.line_mvp.image_text_composer import ImageTextComposer
from src.line_mvp.models import ActionType, InboxMessage, MessageStatus, ScheduleAction
from src.line_mvp.parser import MessageParser
from src.line_mvp.rich_menu import LineRichMenuManager
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
        deadline = time.monotonic() + 5
        while self.service._active_overlay_jobs and time.monotonic() < deadline:
            time.sleep(0.01)
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def wait_for_overlay_job(self, sender_id: str, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while sender_id in self.service._active_overlay_jobs and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn(sender_id, self.service._active_overlay_jobs)

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

    def test_follow_event_opens_function_menu(self) -> None:
        with patch.object(self.service, "_reply_line_function_menu", return_value=False), patch.object(
            self.service,
            "_push_line_function_menu",
            return_value=True,
        ) as push_mock:
            result = self.service.ingest_webhook(
                {
                    "events": [
                        {
                            "type": "follow",
                            "replyToken": "follow-token",
                            "source": {"userId": "U-follow", "type": "user"},
                        }
                    ]
                }
            )
        self.assertEqual("true", result["replies"][0]["ok"])
        push_mock.assert_called_once_with("U-follow")

    def test_menu_command_returns_button_template(self) -> None:
        with patch.object(self.service, "_reply_line_function_menu", return_value=True) as menu_mock:
            result = self.service.ingest_webhook(
                {
                    "events": [
                        {
                            "type": "message",
                            "replyToken": "menu-token",
                            "source": {"userId": "U-menu", "type": "user"},
                            "message": {"type": "text", "text": "選單"},
                        }
                    ]
                }
            )
        self.assertEqual("true", result["replies"][0]["ok"])
        menu_mock.assert_called_once_with("menu-token")
        message = self.service._function_menu_message()
        self.assertEqual("template", message["type"])
        self.assertEqual(4, len(message["template"]["actions"]))

    def test_recover_pending_jobs_starts_worker(self) -> None:
        item = self.make_item()
        item.processing = True
        self.service.store.update_message(item)
        started: list[str] = []
        with patch.object(self.service, "_start_apply_job", side_effect=lambda sender_id, message_id: started.append(message_id)):
            recovered = self.service.recover_pending_jobs()
        self.assertEqual(1, recovered)
        self.assertEqual([item.id], started)

    def test_recover_overlay_jobs_starts_complete_saved_batch(self) -> None:
        self.service._handle_line_text("recover-overlay-user", "user", "加字模式")
        self.service._handle_line_text("recover-overlay-user", "user", "待恢復文字")
        session = self.service.overlay_store.get("recover-overlay-user")
        session.image_queue = [
            {
                "path": str(self.tempdir / "saved-image.jpg"),
                "at": datetime.now().isoformat(),
                "event_timestamp": "1000",
                "message_id": "201",
                "event_id": "saved-image-event",
            }
        ]
        session.batch_requested = True
        self.service.overlay_store.update(session)
        started: list[str] = []

        with patch.object(
            self.service,
            "_start_overlay_batch_job",
            side_effect=lambda sender_id: started.append(sender_id),
        ):
            recovered = self.service.recover_overlay_jobs()

        self.assertEqual(1, recovered)
        self.assertEqual(["recover-overlay-user"], started)

    def test_recover_overlay_jobs_finishes_saved_batch(self) -> None:
        sender_id = "recover-overlay-real-user"
        self.service._handle_line_text(sender_id, "user", "加字模式")
        self.service._handle_line_text(
            sender_id,
            "user",
            "重新啟動後繼續",
            event_timestamp=1_000,
            message_id="101",
        )
        source = self.tempdir / "recover-source.jpg"
        Image.new("RGB", (800, 600), (60, 100, 140)).save(source)
        session = self.service.overlay_store.get(sender_id)
        session.image_queue = [
            {
                "path": str(source),
                "at": datetime.now().isoformat(),
                "event_timestamp": "2000",
                "message_id": "201",
                "event_id": "recover-image-event",
            }
        ]
        session.batch_requested = True
        session.batch_processing = True
        self.service.overlay_store.update(session)

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            self.assertEqual("重新啟動後繼續", text)
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        with patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(
            self.service,
            "_push_image_message",
            return_value=True,
        ), patch.object(
            self.service,
            "_push_line_overlay_choice",
            return_value=True,
        ):
            recovered = self.service.recover_overlay_jobs()
            self.wait_for_overlay_job(sender_id)

        self.assertEqual(1, recovered)
        updated = self.service.overlay_store.get(sender_id)
        self.assertEqual(1, updated.completed_count)
        self.assertEqual([], updated.text_queue)
        self.assertEqual([], updated.image_queue)
        self.assertFalse(updated.batch_requested)
        self.assertFalse(updated.batch_processing)

    def test_overlay_batch_failure_preserves_the_same_text_and_image(self) -> None:
        sender_id = "preserve-failed-pair-user"
        self.service._handle_line_text(sender_id, "user", "加字模式")
        self.service._handle_line_text(
            sender_id,
            "user",
            "紅花",
            event_timestamp=1_000,
            message_id="101",
        )
        source = self.tempdir / "failed-pair.jpg"
        Image.new("RGB", (800, 600), (60, 100, 140)).save(source)
        session = self.service.overlay_store.get(sender_id)
        session.image_queue = [
            {
                "path": str(source),
                "at": datetime.now().isoformat(),
                "event_timestamp": "2000",
                "message_id": "201",
                "event_id": "failed-pair-event",
            }
        ]
        session.batch_requested = True
        self.service.overlay_store.update(session)

        with patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=RuntimeError("temporary compose failure"),
        ), patch.object(
            self.service,
            "_push_line_overlay_choice",
            return_value=True,
        ):
            self.service._start_overlay_batch_job(sender_id)
            self.wait_for_overlay_job(sender_id)

        updated = self.service.overlay_store.get(sender_id)
        self.assertEqual(["紅花"], [part["text"] for part in updated.text_queue])
        self.assertEqual([str(source)], [part["path"] for part in updated.image_queue])
        self.assertEqual(0, updated.completed_count)
        self.assertFalse(updated.batch_requested)
        self.assertFalse(updated.batch_processing)
        self.assertIn("合成失敗", updated.last_error)

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

    def test_overlay_mode_text_first_then_image(self) -> None:
        start_reply = self.service._handle_line_text("user-1", "user", "加字模式")
        self.assertIn("已進入圖片加字模式", start_reply)
        text_reply = self.service._handle_line_text("user-1", "user", "中央大標題")
        self.assertIn("第 1 段待配對", text_reply)
        source = self.tempdir / "source.jpg"
        Image.new("RGB", (900, 600), (40, 90, 160)).save(source, format="JPEG")
        content = source.read_bytes()

        with patch.object(self.service, "_download_line_message_content", return_value=(content, "image/jpeg")), patch.object(
            self.service, "_push_image_message", return_value=True
        ), patch.object(self.service, "_push_line_overlay_choice", return_value=True):
            reply = self.service._handle_line_attachment(
                "user-1",
                "user",
                {"id": "image-1", "type": "image"},
            )
            self.wait_for_overlay_job("user-1")

        self.assertIn("照片已收齊", reply)
        session = self.service.overlay_store.get("user-1")
        self.assertTrue(session.active)
        self.assertTrue(Path(session.output_path).exists())
        self.assertEqual([], session.text_queue)
        self.assertIsNone(self.service.store.find_message("user-1"))

    def test_overlay_completion_replies_with_continue_or_schedule_choices(self) -> None:
        self.service._handle_line_text("choice-user", "user", "加字模式")
        self.service._handle_line_text("choice-user", "user", "红花")
        source = self.tempdir / "choice-source.jpg"
        Image.new("RGB", (900, 600), (40, 90, 160)).save(source, format="JPEG")

        with patch.object(
            self.service,
            "_download_line_message_content",
            return_value=(source.read_bytes(), "image/jpeg"),
        ), patch.object(
            self.service,
            "_push_image_message",
            return_value=True,
        ), patch.object(self.service, "_reply_line_message", return_value=True), patch.object(
            self.service, "_push_line_overlay_choice", return_value=True
        ) as choice_mock:
            result = self.service.ingest_webhook(
                {
                    "events": [
                        {
                            "type": "message",
                            "replyToken": "choice-token",
                            "timestamp": 3_000,
                            "webhookEventId": "image-event",
                            "source": {"userId": "choice-user", "type": "user"},
                            "message": {"id": "image-1", "type": "image"},
                        }
                    ]
                }
            )
            self.wait_for_overlay_job("choice-user")

        self.assertEqual("true", result["replies"][0]["ok"])
        choice_mock.assert_called_once()
        recipient, choice_text = choice_mock.call_args.args
        self.assertEqual("choice-user", recipient)
        self.assertIn("第 1 組完成", choice_text)
        session = self.service.overlay_store.get("choice-user")
        self.assertTrue(session.active)

        message = self.service._overlay_choice_message(choice_text)
        actions = [item["action"] for item in message["quickReply"]["items"]]
        self.assertEqual(["繼續貼字", "回到行程模式"], [action["label"] for action in actions])
        self.assertEqual(["繼續貼字", "回到行程模式"], [action["text"] for action in actions])

    def test_schedule_mode_choice_resets_all_overlay_records(self) -> None:
        self.service._handle_line_text("reset-user", "user", "加字模式")
        self.service._handle_line_text(
            "reset-user",
            "user",
            "尚未配對",
            event_timestamp=1_000,
            message_id="101",
            event_id="event-1",
        )
        session = self.service.overlay_store.get("reset-user")
        session.completed_count = 4
        session.image_queue = [{"path": "queued.jpg", "event_id": "image-event"}]
        session.seen_image_event_ids = ["image-event"]
        session.pending_outputs = ["one.jpg", "two.jpg"]
        session.output_path = "latest.jpg"
        session.last_error = "previous error"
        self.service.overlay_store.update(session)

        reply = self.service._handle_line_text("reset-user", "user", "回到行程模式")

        self.assertIn("本批貼字紀錄已歸零", reply)
        reset = self.service.overlay_store.get("reset-user")
        self.assertFalse(reset.active)
        self.assertEqual([], reset.text_queue)
        self.assertEqual([], reset.seen_event_ids)
        self.assertEqual([], reset.image_queue)
        self.assertEqual([], reset.seen_image_event_ids)
        self.assertEqual([], reset.pending_outputs)
        self.assertEqual(0, reset.completed_count)
        self.assertEqual("", reset.output_path)
        self.assertEqual("", reset.last_error)

    def test_overlay_texts_follow_line_timestamp_instead_of_arrival_order(self) -> None:
        composed: list[tuple[bytes, str]] = []

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            composed.append((Path(image_path).read_bytes(), text))
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        self.service._handle_line_text("ordered-user", "user", "加字模式")
        self.service._handle_line_text(
            "ordered-user",
            "user",
            "第二段",
            event_timestamp=2_000,
            message_id="102",
            event_id="event-2",
        )
        self.service._handle_line_text(
            "ordered-user",
            "user",
            "第一段",
            event_timestamp=1_000,
            message_id="101",
            event_id="event-1",
        )

        session = self.service.overlay_store.get("ordered-user")
        self.assertEqual(["第一段", "第二段"], [part["text"] for part in session.text_queue])

        with patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(self.service, "_push_image_message", return_value=True), patch.object(
            self.service, "_push_line_overlay_choice", return_value=True
        ) as choice_mock:
            with patch.object(
                self.service,
                "_download_line_message_content",
                return_value=(b"later-image", "image/jpeg"),
            ):
                first_reply = self.service._handle_line_attachment(
                    "ordered-user",
                    "user",
                    {"id": "202", "type": "image"},
                    event_timestamp=2_000,
                    event_id="image-event-2",
                )
                self.assertEqual([], composed)
            with patch.object(
                self.service,
                "_download_line_message_content",
                return_value=(b"earlier-image", "image/jpeg"),
            ):
                final_reply = self.service._handle_line_attachment(
                    "ordered-user",
                    "user",
                    {"id": "201", "type": "image"},
                    event_timestamp=1_000,
                    event_id="image-event-1",
                )
            self.wait_for_overlay_job("ordered-user")

        self.assertEqual(
            [(b"earlier-image", "第一段"), (b"later-image", "第二段")],
            composed,
        )
        self.assertIn("安全保存", first_reply)
        self.assertIn("照片已收齊", final_reply)
        choice_mock.assert_called_once()

    def test_partial_overlay_batch_processes_without_waiting_for_all_images(self) -> None:
        composed: list[str] = []

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            composed.append(text)
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        self.service._handle_line_text("partial-user", "user", "加字模式")
        self.service._handle_line_text("partial-user", "user", "第一段")
        self.service._handle_line_text("partial-user", "user", "第二段")

        with patch.object(
            self.service,
            "_download_line_message_content",
            return_value=(b"only-one-image", "image/jpeg"),
        ), patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(self.service, "_push_image_message", return_value=True), patch.object(
            self.service, "_push_text_message", return_value=True
        ) as text_mock, patch.object(
            self.service, "_push_line_overlay_choice", return_value=True
        ) as choice_mock:
            reply = self.service._handle_line_attachment(
                "partial-user",
                "user",
                {"id": "image-1", "type": "image"},
                event_timestamp=1_000,
                event_id="partial-image-event",
            )
            start_reply = self.service._handle_line_text(
                "partial-user",
                "user",
                "開始合成",
            )
            self.wait_for_overlay_job("partial-user")

        self.assertIn("安全保存", reply)
        self.assertIn("可處理 1 組", start_reply)
        self.assertEqual(["第一段"], composed)
        session = self.service.overlay_store.get("partial-user")
        self.assertEqual(["第二段"], [part["text"] for part in session.text_queue])
        self.assertEqual([], session.image_queue)
        text_mock.assert_not_called()
        choice_mock.assert_called_once()

    def test_overlay_images_are_sorted_in_a_bounded_background_batch(self) -> None:
        self.service._handle_line_text("parallel-user", "user", "加字模式")
        self.service._handle_line_text("parallel-user", "user", "第一段")
        self.service._handle_line_text("parallel-user", "user", "第二段")

        def fake_download(message_id: str) -> tuple[bytes, str]:
            return f"image-{message_id}".encode("ascii"), "image/jpeg"

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        with patch.object(
            self.service,
            "_download_line_message_content",
            side_effect=fake_download,
        ), patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(self.service, "_push_image_message", return_value=True):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        self.service._handle_line_attachment,
                        "parallel-user",
                        "user",
                        {"id": "202", "type": "image"},
                        2_000,
                        "parallel-image-2",
                    ),
                    executor.submit(
                        self.service._handle_line_attachment,
                        "parallel-user",
                        "user",
                        {"id": "201", "type": "image"},
                        1_000,
                        "parallel-image-1",
                    ),
                ]
                replies = [future.result(timeout=2) for future in futures]
            self.wait_for_overlay_job("parallel-user")

        self.assertTrue(all("照片" in reply for reply in replies))
        session = self.service.overlay_store.get("parallel-user")
        self.assertEqual(2, session.completed_count)
        self.assertEqual([], session.text_queue)
        self.assertEqual([], session.image_queue)

    def test_current_overlay_image_is_pushed_before_old_pending_output(self) -> None:
        self.service._handle_line_text("priority-user", "user", "加字模式")
        self.service._handle_line_text("priority-user", "user", "新圖片")
        session = self.service.overlay_store.get("priority-user")
        session.pending_outputs = ["old-1.jpg", "old-2.jpg"]
        self.service.overlay_store.update(session)
        pushed: list[str] = []

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        def fake_push(sender_id: str, output_path: str) -> bool:
            pushed.append(output_path)
            return True

        with patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(
            self.service, "_push_image_message", side_effect=fake_push
        ):
            self.service._create_overlay_from_image(
                "priority-user",
                session,
                self.tempdir / "source.jpg",
            )

        self.assertEqual(1, len(pushed))
        self.assertIn("line_overlay_", Path(pushed[0]).name)
        updated = self.service.overlay_store.get("priority-user")
        self.assertEqual(["old-1.jpg", "old-2.jpg"], updated.pending_outputs)

    def test_failed_overlay_push_is_recorded_without_background_retry(self) -> None:
        self.service._handle_line_text("retry-user", "user", "加字模式")
        self.service._handle_line_text("retry-user", "user", "自動補送")
        session = self.service.overlay_store.get("retry-user")
        attempts = [False, True]

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        with patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(
            self.service,
            "_push_image_message",
            side_effect=lambda *_args: attempts.pop(0),
        ):
            self.service._create_overlay_from_image(
                "retry-user",
                session,
                self.tempdir / "source.jpg",
            )

        updated = self.service.overlay_store.get("retry-user")
        self.assertEqual(1, len(updated.pending_outputs))
        self.assertIn("等待手動補送", updated.last_error)

    def test_line_image_push_uses_a_distinct_preview_under_one_megabyte(self) -> None:
        original = self.service.output_dir / "high-resolution.jpg"
        channels = [Image.effect_noise((2400, 1800), 120) for _ in range(3)]
        Image.merge("RGB", channels).save(original, quality=98)
        original_size = original.stat().st_size

        with patch.dict(
            os.environ,
            {
                "LINE_CHANNEL_ACCESS_TOKEN": "test-token",
                "PUBLIC_BASE_URL": "https://word.example.com",
            },
        ), patch.object(
            self.service,
            "_send_line_request",
            return_value=True,
        ) as send_mock:
            pushed = self.service._push_image_message("user-preview", str(original))

        self.assertTrue(pushed)
        request_obj = send_mock.call_args.args[0]
        payload = json.loads(request_obj.data.decode("utf-8"))
        message = payload["messages"][0]
        self.assertNotEqual(message["originalContentUrl"], message["previewImageUrl"])
        self.assertTrue(message["originalContentUrl"].endswith("/high-resolution.jpg"))
        self.assertTrue(message["previewImageUrl"].endswith("/high-resolution_line_preview.jpg"))

        preview = self.service.output_dir / "high-resolution_line_preview.jpg"
        self.assertTrue(preview.is_file())
        self.assertLessEqual(preview.stat().st_size, 900 * 1024)
        self.assertEqual(original_size, original.stat().st_size)
        with Image.open(original) as original_image, Image.open(preview) as preview_image:
            self.assertEqual(
                original_image.width / original_image.height,
                preview_image.width / preview_image.height,
            )

    def test_manual_overlay_retry_sends_one_saved_image(self) -> None:
        self.service._handle_line_text("manual-retry-user", "user", "加字模式")
        first = self.tempdir / "outputs" / "first.jpg"
        second = self.tempdir / "outputs" / "second.jpg"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        session = self.service.overlay_store.get("manual-retry-user")
        session.pending_outputs = [str(first), str(second)]
        self.service.overlay_store.update(session)

        with patch.object(self.service, "_push_image_message", return_value=True) as push_mock:
            reply = self.service._handle_line_text(
                "manual-retry-user",
                "user",
                "補送圖片",
            )

        push_mock.assert_called_once_with("manual-retry-user", str(first))
        self.assertIn("補送成功", reply)
        self.assertIn("剩餘等待補送：1 張", reply)
        updated = self.service.overlay_store.get("manual-retry-user")
        self.assertEqual([str(second)], updated.pending_outputs)

    def test_overlay_retry_accepts_photo_word_and_removes_misqueued_command(self) -> None:
        sender_id = "photo-word-retry-user"
        self.service._handle_line_text(sender_id, "user", "加字模式")
        session = self.service.overlay_store.get(sender_id)
        session.text_queue = [
            {
                "text": "補送照片",
                "at": datetime.now().isoformat(),
                "event_timestamp": "1000",
                "message_id": "101",
                "event_id": "misqueued-command",
            }
        ]
        output = self.tempdir / "outputs" / "saved-result.jpg"
        output.write_bytes(b"saved-result")
        session.pending_outputs = [str(output)]
        self.service.overlay_store.update(session)

        with patch.object(self.service, "_push_image_message", return_value=True) as push_mock:
            reply = self.service._handle_line_text(sender_id, "user", "補送照片")

        push_mock.assert_called_once_with(sender_id, str(output))
        self.assertIn("已移除先前誤加入", reply)
        self.assertIn("圖片補送成功", reply)
        updated = self.service.overlay_store.get(sender_id)
        self.assertEqual([], updated.text_queue)
        self.assertEqual([], updated.pending_outputs)

    def test_manual_overlay_retry_failure_keeps_saved_image(self) -> None:
        self.service._handle_line_text("manual-fail-user", "user", "加字模式")
        output = self.tempdir / "outputs" / "failed.jpg"
        output.write_bytes(b"failed")
        session = self.service.overlay_store.get("manual-fail-user")
        session.pending_outputs = [str(output)]
        self.service.overlay_store.update(session)

        with patch.object(self.service, "_push_image_message", return_value=False):
            reply = self.service._handle_line_text(
                "manual-fail-user",
                "user",
                "重試回傳",
            )

        self.assertIn("補送失敗", reply)
        updated = self.service.overlay_store.get("manual-fail-user")
        self.assertEqual([str(output)], updated.pending_outputs)
        self.assertIn("成品仍安全保留", updated.last_error)

    def test_overlay_start_cleans_legacy_queue_and_missing_outputs(self) -> None:
        session = self.service.overlay_store.get_or_create("legacy-user")
        session.queue_version = 0
        valid_output = self.tempdir / "outputs" / "valid.jpg"
        valid_output.write_bytes(b"valid")
        session.image_queue = [{"path": "old-image.jpg"}]
        session.pending_outputs = [
            str(self.tempdir / "outputs" / "missing.jpg"),
            str(valid_output),
        ]
        self.service.overlay_store.update(session)

        reply = self.service._handle_line_text("legacy-user", "user", "加字模式")

        self.assertIn("舊版待處理照片 1 張", reply)
        self.assertIn("失效成品 1 張", reply)
        updated = self.service.overlay_store.get("legacy-user")
        self.assertEqual([], updated.image_queue)
        self.assertEqual([str(valid_output)], updated.pending_outputs)

    def test_failed_overlay_choice_offers_manual_retry(self) -> None:
        message = self.service._overlay_choice_message("圖片回傳失敗，等待補送。")
        labels = [item["action"]["label"] for item in message["quickReply"]["items"]]
        self.assertEqual(["補送圖片", "繼續貼字", "回到行程模式"], labels)

    def test_complete_overlay_batch_skips_settle_delay(self) -> None:
        self.service._handle_line_text("fast-user", "user", "加字模式")
        self.service._handle_line_text("fast-user", "user", "立即處理")

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        started_at = time.monotonic()
        with patch.object(
            self.service,
            "_download_line_message_content",
            return_value=(b"complete-image", "image/jpeg"),
        ), patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(self.service, "_push_image_message", return_value=True), patch.object(
            self.service, "_push_line_overlay_choice", return_value=True
        ):
            reply = self.service._handle_line_attachment(
                "fast-user",
                "user",
                {"id": "complete-image", "type": "image"},
                event_timestamp=1_000,
                event_id="complete-image-event",
            )
            self.wait_for_overlay_job("fast-user", timeout=1)

        self.assertLess(time.monotonic() - started_at, 1)
        self.assertIn("照片已收齊", reply)
        self.assertNotIn("背景", reply)

    def test_overlay_image_redelivery_is_not_queued_twice(self) -> None:
        self.service._handle_line_text("image-duplicate-user", "user", "加字模式")
        self.service._handle_line_text("image-duplicate-user", "user", "第一段")
        self.service._handle_line_text("image-duplicate-user", "user", "第二段")

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        with patch.object(
            self.service,
            "_download_line_message_content",
            return_value=(b"image", "image/jpeg"),
        ) as download_mock, patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(self.service, "_push_image_message", return_value=True):
            first = self.service._handle_line_attachment(
                "image-duplicate-user",
                "user",
                {"id": "201", "type": "image"},
                event_timestamp=1_000,
                event_id="same-image-event",
            )
            duplicate = self.service._handle_line_attachment(
                "image-duplicate-user",
                "user",
                {"id": "201", "type": "image"},
                event_timestamp=1_000,
                event_id="same-image-event",
            )

        self.assertIn("安全保存", first)
        self.assertIn("不會重複配對", duplicate)
        self.assertEqual(1, download_mock.call_count)
        session = self.service.overlay_store.get("image-duplicate-user")
        self.assertEqual(1, len(session.image_queue))

    def test_overlay_redelivery_does_not_duplicate_text(self) -> None:
        self.service._handle_line_text("duplicate-user", "user", "加字模式")
        first = self.service._handle_line_text(
            "duplicate-user",
            "user",
            "红花",
            event_timestamp=1_000,
            message_id="101",
            event_id="same-event",
        )
        duplicate = self.service._handle_line_text(
            "duplicate-user",
            "user",
            "红花",
            event_timestamp=1_000,
            message_id="101",
            event_id="same-event",
        )

        self.assertIn("目前共 1 段", first)
        self.assertIn("不會重複加入", duplicate)
        session = self.service.overlay_store.get("duplicate-user")
        self.assertEqual(1, len(session.text_queue))

    def test_overlay_mode_rejects_image_before_text(self) -> None:
        self.service._handle_line_text("user-2", "user", "加字模式")
        source = self.tempdir / "source-first.jpg"
        Image.new("RGB", (800, 800), (90, 130, 70)).save(source, format="JPEG")
        content = source.read_bytes()

        with patch.object(
            self.service,
            "_download_line_message_content",
            return_value=(content, "image/jpeg"),
        ) as download_mock:
            waiting_reply = self.service._handle_line_attachment(
                "user-2",
                "user",
                {"id": "image-2", "type": "image"},
            )
        self.assertIn("請先傳送一段或多段文字", waiting_reply)
        download_mock.assert_not_called()

        text_reply = self.service._handle_line_text("user-2", "user", "照片正中央")
        self.assertIn("第 1 段待配對", text_reply)
        with patch.object(
            self.service,
            "_download_line_message_content",
            return_value=(content, "image/jpeg"),
        ), patch.object(self.service, "_push_image_message", return_value=True), patch.object(
            self.service, "_push_line_overlay_choice", return_value=True
        ) as choice_mock:
            reply = self.service._handle_line_attachment(
                "user-2",
                "user",
                {"id": "image-2-retry", "type": "image"},
            )
            self.wait_for_overlay_job("user-2")
        self.assertIn("照片已收齊", reply)

    def test_twenty_batched_texts_then_images_stay_one_to_one_in_order(self) -> None:
        composed: list[tuple[bytes, str]] = []

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            composed.append((Path(image_path).read_bytes(), text))
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        self.service._handle_line_text("bulk-user", "user", "加字模式")
        for index in range(20):
            self.service._handle_line_text(
                "bulk-user",
                "user",
                f"第 {index + 1} 張照片的文字",
            )

        waiting = self.service.overlay_store.get("bulk-user")
        self.assertEqual(20, len(waiting.text_queue))

        with patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(self.service, "_push_image_message", return_value=True), patch.object(
            self.service, "_push_line_overlay_choice", return_value=True
        ) as choice_mock:
            for index in range(20):
                image_bytes = f"unique-image-{index}".encode("ascii")
                with patch.object(
                    self.service,
                    "_download_line_message_content",
                    return_value=(image_bytes, "image/jpeg"),
                ):
                    self.service._handle_line_attachment(
                        "bulk-user",
                        "user",
                        {"id": f"image-{index}", "type": "image"},
                    )
            self.wait_for_overlay_job("bulk-user", timeout=10)

        expected = [
            (f"unique-image-{index}".encode("ascii"), f"第 {index + 1} 張照片的文字")
            for index in range(20)
        ]
        self.assertEqual(expected, composed)
        session = self.service.overlay_store.get("bulk-user")
        self.assertEqual(20, session.completed_count)
        self.assertEqual([], session.text_queue)
        self.assertEqual([], session.image_queue)
        choice_mock.assert_called_once()

    def test_more_than_one_worker_batch_shows_choices_only_after_all_images(self) -> None:
        sender_id = "large-bounded-batch-user"
        self.service._handle_line_text(sender_id, "user", "加字模式")
        session = self.service.overlay_store.get(sender_id)
        session.text_queue = [
            {
                "text": f"文字 {index}",
                "at": datetime.now().isoformat(),
                "event_timestamp": str(1_000 + index),
                "message_id": str(10_000 + index),
                "event_id": f"text-{index}",
            }
            for index in range(101)
        ]
        session.image_queue = [
            {
                "path": str(self.tempdir / f"image-{index}.jpg"),
                "at": datetime.now().isoformat(),
                "event_timestamp": str(2_000 + index),
                "message_id": str(20_000 + index),
                "event_id": f"image-{index}",
            }
            for index in range(101)
        ]
        session.batch_requested = True
        self.service.overlay_store.update(session)

        def fake_compose(image_path: str, text: str, output_path: Path) -> str:
            Path(output_path).write_bytes(b"generated-jpeg")
            return str(output_path)

        with patch.object(
            self.service.image_text_composer,
            "compose",
            side_effect=fake_compose,
        ), patch.object(
            self.service,
            "_push_image_message",
            return_value=True,
        ), patch.object(
            self.service,
            "_push_text_message",
            return_value=True,
        ) as progress_mock, patch.object(
            self.service,
            "_push_line_overlay_choice",
            return_value=True,
        ) as choice_mock:
            self.service._start_overlay_batch_job(sender_id)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                updated = self.service.overlay_store.get(sender_id)
                if updated.completed_count == 101 and not updated.batch_processing:
                    break
                time.sleep(0.01)
            self.wait_for_overlay_job(sender_id, timeout=10)

        updated = self.service.overlay_store.get(sender_id)
        self.assertEqual(101, updated.completed_count)
        self.assertEqual([], updated.text_queue)
        self.assertEqual([], updated.image_queue)
        progress_mock.assert_called_once()
        choice_mock.assert_called_once()

    def test_overlay_mode_is_separate_from_schedule_messages(self) -> None:
        schedule_reply = self.service._handle_line_text(
            "separate-user",
            "user",
            "7/20 10:00 原本行程",
        )
        self.assertIn("案件代碼", schedule_reply)
        item = self.service.store.find_message("separate-user")
        original_raw_text = item.raw_text

        self.service._handle_line_text("separate-user", "user", "加字模式")
        self.service._handle_line_text("separate-user", "user", "確認")
        unchanged = self.service.store.find_message("separate-user")
        self.assertEqual(original_raw_text, unchanged.raw_text)
        session = self.service.overlay_store.get("separate-user")
        self.assertEqual("確認", session.text_queue[0]["text"])

        end_reply = self.service._handle_line_text("separate-user", "user", "結束加字")
        self.assertIn("切換至行程模式", end_reply)
        self.service._handle_line_text("separate-user", "user", "7/21 11:00 新行程")
        updated = self.service.store.find_message("separate-user")
        self.assertIn("7/21 11:00 新行程", updated.raw_text)

    def test_overlay_status_and_cancel_commands(self) -> None:
        self.service._handle_line_text("control-user", "user", "加字模式")
        self.service._handle_line_text("control-user", "user", "第一段文字")
        self.service._handle_line_text("control-user", "user", "第二段文字")

        status = self.service._handle_line_text("control-user", "user", "加字狀態")
        self.assertIn("目前模式：加字模式", status)
        self.assertIn("等待照片的文字：2 段", status)

        cancelled = self.service._handle_line_text("control-user", "user", "取消加字")
        self.assertIn("清空 2 段", cancelled)
        session = self.service.overlay_store.get("control-user")
        self.assertFalse(session.active)
        self.assertEqual([], session.text_queue)

    def test_overlay_texts_do_not_expire_by_default(self) -> None:
        self.service.overlay_pair_seconds = 0
        session = self.service.overlay_store.get_or_create("no-expiry-user")
        session.active = True
        session.text_queue = [
            {
                "text": "保留到我主動結束",
                "at": "2020-01-01T00:00:00",
                "event_timestamp": "1000",
                "message_id": "101",
                "event_id": "old-text-event",
            }
        ]

        expired = self.service._prune_overlay_texts(session)

        self.assertEqual(0, expired)
        self.assertEqual("保留到我主動結束", session.text_queue[0]["text"])


class ImageTextComposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="line-overlay-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def test_text_is_drawn_in_upper_center_without_dark_panel(self) -> None:
        source = self.tempdir / "source.jpg"
        output = self.tempdir / "result.jpg"
        background = (24, 80, 145)
        Image.new("RGB", (1200, 800), background).save(source, format="JPEG", quality=100)

        result_path = ImageTextComposer().compose(source, "中央大字測試", output)
        self.assertEqual(str(output), result_path)
        self.assertTrue(output.exists())

        with Image.open(output) as result:
            upper_center = result.crop((350, 120, 850, 450))
            corner = result.crop((0, 0, 120, 120))
            upper_colors = upper_center.convert("RGB").getcolors(maxcolors=1_000_000)
            corner_colors = corner.convert("RGB").getcolors(maxcolors=1_000_000)
            self.assertGreater(len(upper_colors), len(corner_colors))

            colors = upper_center.convert("RGB").getcolors(maxcolors=1_000_000) or []
            dark_pixels = sum(count for count, color in colors if max(color) < 45)
            pixel_count = upper_center.width * upper_center.height
            self.assertLess(dark_pixels / pixel_count, 0.16)

    def test_default_line_image_preserves_safe_original_dimensions(self) -> None:
        source = self.tempdir / "large-source.jpg"
        output = self.tempdir / "large-result.jpg"
        Image.new("RGB", (3200, 2400), (70, 110, 150)).save(source, quality=95)

        ImageTextComposer().compose(source, "快速回傳", output)

        with Image.open(output) as result:
            self.assertEqual((3200, 2400), result.size)

    def test_optional_max_dimension_still_allows_manual_size_limit(self) -> None:
        source = self.tempdir / "manual-limit-source.jpg"
        output = self.tempdir / "manual-limit-result.jpg"
        Image.new("RGB", (2400, 1800), (70, 110, 150)).save(source, quality=95)

        ImageTextComposer(max_dimension=1200).compose(source, "手動限制", output)

        with Image.open(output) as result:
            self.assertEqual((1200, 900), result.size)

    def test_default_output_is_a_valid_jpeg(self) -> None:
        source = self.tempdir / "detailed-source.jpg"
        output = self.tempdir / "detailed-result.jpg"
        channels = [Image.effect_noise((1600, 1200), 100) for _ in range(3)]
        Image.merge("RGB", channels).save(source, quality=95)

        ImageTextComposer().compose(source, "容量測試", output)

        with Image.open(output) as result:
            self.assertEqual((1600, 1200), result.size)
        self.assertGreater(output.stat().st_size, 0)

    def test_oversized_output_is_bounded_only_when_needed(self) -> None:
        source = self.tempdir / "oversized-source.jpg"
        output = self.tempdir / "oversized-result.jpg"
        channels = [Image.effect_noise((1800, 1400), 120) for _ in range(3)]
        Image.merge("RGB", channels).save(source, quality=98)

        ImageTextComposer(max_output_bytes=220_000).compose(source, "紅花", output)

        self.assertLessEqual(output.stat().st_size, 220_000)
        with Image.open(output) as result:
            self.assertLessEqual(result.width, 1800)
            self.assertLessEqual(result.height, 1400)
            self.assertGreaterEqual(result.width, 120)
            self.assertGreaterEqual(result.height, 120)

    def test_bundled_font_contains_distinct_traditional_chinese_glyphs(self) -> None:
        font = ImageTextComposer._load_font(72)
        self.assertTrue(ImageTextComposer._font_supports_cjk(font))
        signatures = []
        for character in "勝光山":
            mask = font.getmask(character)
            signatures.append((mask.size, bytes(mask)))
        self.assertEqual(3, len(set(signatures)))

    def test_simplified_chinese_uses_a_font_without_missing_glyph_boxes(self) -> None:
        font = ImageTextComposer._load_font(72, "红花")
        self.assertTrue(ImageTextComposer._font_supports_text(font, "红花"))
        self.assertEqual("NotoSansSC.ttf", Path(font.path).name)
        self.assertTrue(ImageTextComposer._font_supports_text(font, "紅红花"))

        source = self.tempdir / "simplified-source.jpg"
        output = self.tempdir / "simplified-result.jpg"
        Image.new("RGB", (900, 1200), (80, 120, 160)).save(source)
        ImageTextComposer().compose(source, "红花", output)
        self.assertTrue(output.exists())

    def test_long_text_wraps_without_exceeding_canvas(self) -> None:
        source = self.tempdir / "portrait.png"
        output = self.tempdir / "portrait-result.jpg"
        Image.new("RGBA", (700, 1100), (180, 100, 80, 255)).save(source)
        text = "這是一段比較長的文字，系統需要自動換行，而且仍然保持在照片上方中央。"
        ImageTextComposer().compose(source, text, output)
        with Image.open(output) as result:
            self.assertEqual((700, 1100), result.size)

    def test_ten_layout_variants(self) -> None:
        variants = [
            ((1200, 630), "活動開始"),
            ((630, 1200), "今晚七點集合"),
            ((900, 900), "重要通知"),
            ((1600, 900), "這是一行橫式大標題"),
            ((900, 1600), "直式照片\n第二行文字"),
            ((640, 480), "短字"),
            ((480, 640), "請記得攜帶證件與相關資料"),
            ((1024, 512), "第一行\n第二行\n第三行"),
            ((512, 1024), "比較長的文字會自動換行並保持置中"),
            ((1400, 1000), "LINE 照片自動加字測試 10"),
        ]
        composer = ImageTextComposer()
        for index, (size, text) in enumerate(variants, start=1):
            with self.subTest(index=index, size=size):
                source = self.tempdir / f"variant-{index}.png"
                output = self.tempdir / f"variant-{index}.jpg"
                Image.new("RGB", size, (35 + index * 5, 80, 120)).save(source)
                composer.compose(source, text, output)
                self.assertTrue(output.exists())
                with Image.open(output) as result:
                    self.assertLessEqual(max(result.size), 2048)


class RichMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="line-rich-menu-"))
        assets = self.tempdir / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        source_font = Path(__file__).resolve().parents[1] / "assets" / "NotoSansTC.ttf"
        shutil.copy2(source_font, assets / "NotoSansTC.ttf")

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def test_rich_menu_has_four_ordered_message_actions(self) -> None:
        manager = LineRichMenuManager(self.tempdir)
        payload = manager._menu_payload()
        self.assertTrue(payload["selected"])
        self.assertEqual({"width": 2500, "height": 843}, payload["size"])
        self.assertEqual(
            ["行程模式", "加字模式", "加字狀態", "幫助"],
            [area["action"]["text"] for area in payload["areas"]],
        )

    def test_rich_menu_image_renders_with_chinese_font(self) -> None:
        manager = LineRichMenuManager(self.tempdir)
        image_path = manager._render_image()
        self.assertTrue(image_path.exists())
        with Image.open(image_path) as image:
            self.assertEqual((2500, 843), image.size)


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
