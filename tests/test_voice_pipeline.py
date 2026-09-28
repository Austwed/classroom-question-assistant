"""Focused checks for stop behavior and the two-stage voice pipeline."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import app as app_module
import audio_capture
from settings import AppParameters


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeASR:
    def __init__(self) -> None:
        self.transcribed: list[str] = []

    def transcribe(self, audio, settings: AppParameters) -> str:
        self.transcribed.append(settings.identity)
        return f"问题{len(self.transcribed)}"


class BlockingResponder:
    def __init__(self) -> None:
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.first_finished = threading.Event()

    def answer(self, statement: str, settings: AppParameters) -> str:
        if statement == "问题1":
            self.first_started.set()
            if not self.release_first.wait(3):
                raise TimeoutError("测试中的第一条回答未被释放")
            self.first_finished.set()
        return f"回答：{statement}"


class ImmediateResponder:
    def answer(self, statement: str, settings: AppParameters) -> str:
        return f"{settings.identity}：{statement}"


class VoicePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_stop_keeps_queued_audio_and_asr_runs_during_answer(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
        ):
            window = app_module.ClassroomApp()
        asr = FakeASR()
        responder = BlockingResponder()
        window.asr = asr
        window.session_device_name = "测试设备"
        window.session_provider_name = "本地模型"
        window.session_settings = AppParameters(identity="原身份")
        window.listener_running = True
        window.stop_event = threading.Event()
        settings = AppParameters(identity="原身份")
        window.preload_target = settings.asr_backend
        window.active_asr_backend = settings.asr_backend
        try:
            window._submit_audio(np.ones(3200, dtype=np.float32), settings, responder, 1)
            self.assertTrue(responder.first_started.wait(3))
            window._submit_audio(np.ones(3200, dtype=np.float32), settings, responder, 1)
            self.assertTrue(wait_until(lambda: len(asr.transcribed) == 2))
            self.assertFalse(responder.first_finished.is_set(), "第二段应能在第一条回答期间完成识别")

            window._stop()
            window.events.put(("listener_stopped", None))
            responder.release_first.set()

            def both_complete() -> bool:
                window._poll_events()
                return len(window.records) == 2 and all(
                    item["status"] == "已完成" for item in window.records.values()
                )

            self.assertTrue(wait_until(both_complete))
            self.assertEqual(len(window.conversation_entries), 2)
            self.assertIn("全部处理完成", window.status_label.text())
            self.assertTrue(all("asr_wait" in item and "answer_wait" in item for item in window.records.values()))
        finally:
            responder.release_first.set()
            window.close()

    def test_capture_uses_settings_from_its_own_session(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
        ):
            window = app_module.ClassroomApp()
        asr = FakeASR()
        window.asr = asr
        old_settings = AppParameters(identity="原身份")
        window.parameters = AppParameters(identity="新身份")
        window.preload_target = window.parameters.asr_backend
        window.active_asr_backend = window.parameters.asr_backend
        stop = threading.Event()

        def fake_listen(_device_id, _threshold, _silence, _stop, _level, on_utterance):
            on_utterance(np.ones(3200, dtype=np.float32))

        try:
            with patch.object(app_module, "listen", side_effect=fake_listen):
                window._capture("test-id", 0.01, 0.8, stop, ImmediateResponder(), old_settings, 4)

            def answer_complete() -> bool:
                window._poll_events()
                return len(window.conversation_entries) == 1

            self.assertTrue(wait_until(answer_complete))
            self.assertEqual(asr.transcribed, ["原身份"])
            self.assertIn("原身份", window.conversation_entries[0][2])
            self.assertEqual(next(iter(window.records.values()))["session_id"], 4)
        finally:
            window.close()

    def test_capture_flushes_partially_recorded_speech_on_stop(self) -> None:
        stop = threading.Event()
        utterances = []

        class Recorder:
            def __init__(self) -> None:
                self.count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def record(self, numframes: int):
                self.count += 1
                if self.count == 3:
                    stop.set()
                return np.full(numframes, 0.1, dtype=np.float32)

        class Microphone:
            def recorder(self, **_kwargs):
                return Recorder()

        with patch.object(audio_capture.sc, "get_microphone", return_value=Microphone()):
            audio_capture.listen("test-id", 0.01, 0.8, stop, lambda _level: None, utterances.append)
        self.assertEqual(len(utterances), 1)
        self.assertEqual(len(utterances[0]), 3 * audio_capture.FRAME_SAMPLES)

    def test_full_queues_leave_visible_unqueued_records(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
            patch.object(app_module.ClassroomApp, "_asr_worker"),
            patch.object(app_module.ClassroomApp, "_answer_worker"),
        ):
            window = app_module.ClassroomApp()
        try:
            for _ in range(6):
                window.asr_tasks.put_nowait(object())
                window.answer_tasks.put_nowait(object())
            settings = AppParameters(identity="测试身份")
            window._submit_audio(np.ones(3200, dtype=np.float32), settings, ImmediateResponder(), 1)
            manual_id = window._new_record("手动", "手动问题")
            window._submit_answer(manual_id, "手动问题", settings, ImmediateResponder())
            window._poll_events()
            self.assertEqual([item["status"] for item in window.records.values()], ["未入队", "未入队"])
            self.assertIn("音频已暂存", window.records[1]["detail"])
            self.assertIn("回答队列已满", window.records[manual_id]["detail"])
        finally:
            window.close()

    def test_retry_is_bounded_and_keeps_original_settings(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
            patch.object(app_module.ClassroomApp, "_asr_worker"),
            patch.object(app_module.ClassroomApp, "_answer_worker"),
        ):
            window = app_module.ClassroomApp()
        original = AppParameters(identity="A 身份")
        responder = ImmediateResponder()
        try:
            for _ in range(6):
                window.asr_tasks.put_nowait(object())
            for _ in range(4):
                window._submit_audio(np.ones(3200, dtype=np.float32), original, responder, 1)
            window._poll_events()
            self.assertEqual(len(window.retry_audio), 3)
            self.assertLessEqual(window.retry_audio_bytes, window.MAX_AUDIO_RETRY_BYTES)
            self.assertIn("音频未保留", window.records[1]["detail"])
            self.assertEqual(list(window.retry_audio), [2, 3, 4])

            window.identity_edit.setText("B 身份")
            window.asr_tasks.get_nowait()
            window.progress.setCurrentItem(window.records[4]["item"])
            window._retry_selected()
            self.assertEqual(window.records[4]["status"], "等待转写")
            queued_count = window.asr_tasks.qsize()
            window._retry_selected()
            self.assertEqual(window.asr_tasks.qsize(), queued_count, "快速连点不能重复入队")
            queued = list(window.asr_tasks.queue)[-1]
            self.assertEqual(queued[0], 4)
            self.assertEqual(queued[2].identity, "A 身份")
            self.assertIs(queued[3], responder)

            for _ in range(6):
                window.answer_tasks.put_nowait(object())
            answer_id = window._new_record("手动", "旧会话问题", settings=original, responder=responder)
            window._submit_answer(answer_id, "旧会话问题", original, responder)
            window._poll_events()
            window.progress.setCurrentItem(window.records[answer_id]["item"])
            window._retry_selected()
            self.assertEqual(window.records[answer_id]["status"], "未入队")
            window.answer_tasks.get_nowait()
            window._retry_selected()
            window._poll_events()
            self.assertEqual(window.records[answer_id]["status"], "等待回答")
            answer_count = window.answer_tasks.qsize()
            window._retry_selected()
            self.assertEqual(window.answer_tasks.qsize(), answer_count)
            self.assertEqual(list(window.answer_tasks.queue)[-1][2].identity, "A 身份")
            self.assertIs(list(window.answer_tasks.queue)[-1][3], responder)
        finally:
            window.close()
            self.assertEqual(len(window.retry_audio), 0)

    def test_correction_links_records_and_export_marks_final_answer(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
            patch.object(app_module.ClassroomApp, "_asr_worker"),
            patch.object(app_module.ClassroomApp, "_answer_worker"),
        ):
            window = app_module.ClassroomApp()
        original = AppParameters(identity="A 身份")
        responder = ImmediateResponder()
        try:
            original_id = window._new_record("语音", session_id=1, settings=original, responder=responder)
            window.events.put(("transcript", (original_id, "原问题", 0.2)))
            window.events.put(("answer", (original_id, "原问题", "旧回答", 0.3)))
            window._poll_events()

            window._load_selected_transcript(window.records[original_id]["item"])
            window.test_edit.setText("修正问题")
            window.identity_edit.setText("B 身份")
            window._test_question()
            window._poll_events()
            correction_id = original_id + 1
            queued = window.answer_tasks.get_nowait()
            self.assertEqual(queued[0], correction_id)
            self.assertEqual(queued[2].identity, "A 身份")
            window.events.put(("answer", (correction_id, "修正问题", "新回答", 0.4)))
            window._poll_events()

            self.assertEqual(window.records[original_id]["status"], "已被修正替代")
            self.assertEqual(window.records[original_id]["superseded_by"], correction_id)
            self.assertEqual(window.records[correction_id]["parent_id"], original_id)
            exported = "\n".join(window._conversation_export_lines())
            self.assertIn("修正自：#1", exported)
            self.assertIn("已由片段 #2 修正替代", exported)
            self.assertIn("回答：旧回答", exported)
            self.assertIn("回答：新回答", exported)
            self.assertIn("A 身份", exported)

            window.events.put(("record_state", (original_id, "正在回答", "")))
            window.events.put(("answer", (original_id, "原问题", "迟到的旧回答", 0.5)))
            window._poll_events()
            self.assertEqual(window.records[original_id]["status"], "已被修正替代")
            self.assertEqual(window.records[original_id]["superseded_by"], correction_id)
        finally:
            window.close()

    def test_rapid_corrections_form_one_revision_chain(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
            patch.object(app_module.ClassroomApp, "_asr_worker"),
            patch.object(app_module.ClassroomApp, "_answer_worker"),
        ):
            window = app_module.ClassroomApp()
        settings = AppParameters(identity="原身份")
        responder = ImmediateResponder()
        try:
            first = window._new_record("语音", session_id=1, settings=settings, responder=responder)
            window.events.put(("transcript", (first, "初稿", 0.1)))
            window._poll_events()
            window._load_selected_transcript(window.records[first]["item"])
            window.test_edit.setText("修正一次")
            window._test_question()
            second = first + 1
            window._load_selected_transcript(window.records[first]["item"])
            self.assertEqual(window.correction_source_id, second)
            window.test_edit.setText("修正两次")
            window._test_question()
            third = second + 1
            self.assertEqual(window.records[third]["parent_id"], second)

            for record_id, statement in ((first, "初稿"), (second, "修正一次"), (third, "修正两次")):
                window.events.put(("answer", (record_id, statement, f"答复{record_id}", 0.2)))
            window._poll_events()
            self.assertEqual(window.records[first]["status"], "已被修正替代")
            self.assertEqual(window.records[second]["status"], "已被修正替代")
            self.assertEqual(window.records[third]["status"], "已完成")
            self.assertEqual(window._latest_revision_id(first), third)
        finally:
            window.close()

    def test_model_preload_and_capture_error_do_not_hide_pending_count(self) -> None:
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
            patch.object(app_module.ClassroomApp, "_asr_worker"),
            patch.object(app_module.ClassroomApp, "_answer_worker"),
        ):
            window = app_module.ClassroomApp()
        try:
            window.stop_event = threading.Event()
            window.stop_event.set()
            window._new_record("手动", "问题", settings=AppParameters(), responder=ImmediateResponder())
            window._poll_events()
            window.events.put(("asr_ready", (window.preload_generation, "funasr", "")))
            window._poll_events()
            self.assertIn("仍有 1 段处理中", window.status_label.text())
            self.assertIn("已就绪", window.asr_status_label.text())
            window.events.put(("capture_failed", "设备断开"))
            window._poll_events()
            self.assertIn("收音失败：设备断开", window.status_label.text())
            self.assertIn("仍有 1 段处理中", window.status_label.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
