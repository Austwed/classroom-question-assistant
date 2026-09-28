"""One-run memory and provider message payloads, without real API calls."""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import app as app_module
import engine
from conversation_memory import ConversationMemory
from settings import AppParameters


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class CapturingResponder(engine.ClassroomResponder):
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def chat(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        self.calls.append((system, messages))
        return f"答复{len(self.calls)}"


class ConversationMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def make_window(self):
        with (
            patch.object(app_module, "output_devices", return_value=[("测试设备", "test-id")]),
            patch.object(app_module, "default_output_id", return_value="test-id"),
            patch.object(app_module, "load_key", return_value=None),
            patch.object(app_module.ClassroomApp, "_begin_asr_preload"),
        ):
            return app_module.ClassroomApp()

    def test_voice_and_manual_questions_share_only_this_run_history(self) -> None:
        window = self.make_window()
        responder = CapturingResponder()
        settings = AppParameters(identity="Unity 求职者", prepared_answers="自我介绍示例")
        questions = (
            ("语音", "简述unity动态批处理dynamic batching原理和限制。"),
            ("手动", "简述srp batcher原理与优势。"),
            ("语音", "我问你的第一个问题是什么"),
        )
        try:
            with patch.object(engine, "to_simplified", side_effect=lambda value: value):
                for number, (source, statement) in enumerate(questions, 1):
                    record_id = window._new_record(source, statement, settings=settings, responder=responder)
                    self.assertEqual(record_id, number)
                    window._submit_answer(record_id, statement, settings, responder)
                    self.assertTrue(wait_until(lambda: len(responder.calls) == number))
                    self.assertTrue(wait_until(lambda: window.conversation_memory.history_before(number + 1)[-1][2] is not None))

            system, messages = responder.calls[2]
            self.assertIn("人设背景、预置答案", system)
            self.assertEqual([message["role"] for message in messages], ["user", "assistant", "user", "assistant", "user"])
            self.assertIn(questions[0][1], messages[0]["content"])
            self.assertIn(questions[1][1], messages[2]["content"])
            self.assertIn(questions[2][1], messages[4]["content"])
            self.assertNotIn("自我介绍示例", " ".join(message["content"] for message in messages))
        finally:
            window.close()

        new_window = self.make_window()
        try:
            self.assertEqual(new_window.conversation_memory.history_before(1), ())
        finally:
            new_window.close()

    def test_corrections_replace_old_question_in_future_memory(self) -> None:
        memory = ConversationMemory()
        memory.record_question(1, "错误转写")
        memory.record_answer(1, "旧回答")
        memory.record_question(2, "正确问题", parent_id=1)
        memory.record_answer(2, "新回答")
        memory.record_question(3, "追问")
        self.assertEqual(memory.history_before(2), ())
        self.assertEqual(memory.history_before(3), ((2, "正确问题", "新回答"),))

    def test_provider_payloads_keep_ordered_history(self) -> None:
        messages = [
            {"role": "user", "content": "【本次运行片段 #1】动态批处理是什么"},
            {"role": "assistant", "content": "之前的回答"},
            {"role": "user", "content": "【当前片段 #2】第一个问题是什么"},
        ]
        local = engine.LocalLlama()
        local.available_model = lambda: "test-model"
        captured = []
        local._json = lambda _path, payload=None: (captured.append(payload), {"choices": [{"message": {"content": "回答"}, "finish_reason": "stop"}]})[1]
        local.chat("固定规则", messages, 100)
        self.assertEqual(captured[0]["messages"][1:], messages)

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        requests = []

        def fake_urlopen(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            requests.append(payload)
            if "messages" in payload:
                return Response({"choices": [{"message": {"content": "回答"}}]})
            return Response({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "回答"}]}]})

        with patch.object(engine, "urlopen", side_effect=fake_urlopen), patch.object(engine.json, "load", side_effect=lambda response: response.payload):
            engine.OpenAIGPT("example-only-key").chat("固定规则", messages, 100)
            self.assertEqual(requests[-1]["input"], messages)
            self.assertFalse(requests[-1]["store"])
            engine.CompatibleChat("https://example.com", "demo", "example-only-key").chat("固定规则", messages, 100)
            self.assertEqual(requests[-1]["messages"][1:], messages)


if __name__ == "__main__":
    unittest.main()
