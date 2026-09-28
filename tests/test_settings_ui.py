"""Settings validation and interface state checks without real devices or credentials."""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QLineEdit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import app as app_module
from settings import AppParameters, Persona


class SettingsUiTests(unittest.TestCase):
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

    def test_settings_pages_dirty_state_and_dependent_fields(self) -> None:
        window = self.make_window()
        try:
            self.assertEqual(window.settings_tabs.count(), 3)
            window.parameters = window._form_parameters()
            window._update_summary()
            self.assertEqual(window.dirty_label.text(), "设置已保存")
            self.assertFalse(window.save_parameters_button.isEnabled())

            window.identity_edit.setText("测试身份")
            window._update_summary()
            self.assertIn("有未保存修改", window.dirty_label.text())
            self.assertTrue(window.save_parameters_button.isEnabled())

            window.asr_box.setCurrentIndex(window.asr_box.findData("whisper-base"))
            self.assertFalse(window.hotwords_edit.isEnabled())
            window.asr_box.setCurrentIndex(window.asr_box.findData("funasr"))
            self.assertTrue(window.hotwords_edit.isEnabled())

            current = window._form_parameters()
            window.personas = {
                "当前人设": Persona(
                    name="当前人设",
                    identity=current.identity,
                    scenario=current.scenario,
                    background=current.background,
                    knowledge_scope=current.knowledge_scope,
                    speaking_style=current.speaking_style,
                    prepared_answers=current.prepared_answers,
                    instructions=current.instructions,
                ),
                "另一个人设": Persona(name="另一个人设", identity="其他身份"),
            }
            window._refresh_persona_box("另一个人设")
            window._update_summary()
            self.assertIn("当前人设", window.persona_hint.text())
            self.assertIn("另一个人设", window.persona_hint.text())
            self.assertIn("点击", window.persona_hint.text())

            window.provider_box.setCurrentIndex(window.provider_box.findData("openai"))
            window.key_edit.setText("example-only-key")
            window.show_key_button.setChecked(True)
            self.assertEqual(window.key_edit.echoMode(), QLineEdit.EchoMode.Normal)
            window.show_key_button.setChecked(False)
            self.assertEqual(window.key_edit.echoMode(), QLineEdit.EchoMode.Password)
            window._update_summary()
            self.assertIn("API Key 待保存", window.dirty_label.text())
        finally:
            window.close()

    def test_saving_cloud_key_uses_credential_store(self) -> None:
        window = self.make_window()
        try:
            window.provider_box.setCurrentIndex(window.provider_box.findData("openai"))
            window.key_edit.setText("example-only-key")
            with (
                patch.object(AppParameters, "save"),
                patch.object(app_module, "save_key") as save_key,
                patch.object(app_module, "load_key", return_value="example-only-key"),
            ):
                self.assertTrue(window._save_parameters())
            save_key.assert_called_once()
            self.assertEqual(window.key_edit.text(), "")
        finally:
            window.close()

    def test_applying_persona_keeps_other_unsaved_fields(self) -> None:
        window = self.make_window()
        try:
            window.provider_box.setCurrentIndex(window.provider_box.findData("local"))
            window.server_edit.setText("http://127.0.0.1:9090")
            persona = Persona(name="面试身份", identity="Unity 应届生", scenario="模拟面试")
            with patch.object(AppParameters, "save"):
                self.assertTrue(window._activate_persona(persona))
            self.assertEqual(window.parameters.identity, "Unity 应届生")
            self.assertEqual(window.parameters.scenario, "模拟面试")
            self.assertEqual(window.parameters.server_url, "http://127.0.0.1:9090")
        finally:
            window.close()

    def test_save_action_stays_visible_while_scrolling_settings(self) -> None:
        window = self.make_window()
        try:
            window.resize(740, 560)
            window.show()
            window.centralWidget().setCurrentIndex(1)
            self.qt_app.processEvents()
            for index in range(window.settings_tabs.count()):
                window.settings_tabs.setCurrentIndex(index)
                scroll = window.settings_tabs.currentWidget()
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                self.qt_app.processEvents()
                self.assertTrue(window.save_parameters_button.isVisibleTo(window))
                self.assertLess(
                    window.save_parameters_button.mapTo(window, window.save_parameters_button.rect().bottomRight()).y(),
                    window.height(),
                )
        finally:
            window.close()

    def test_collapsible_sections_and_answer_maximize_restore(self) -> None:
        window = self.make_window()
        try:
            window.show()
            self.qt_app.processEvents()
            initial_answer_height = window.answer.height()

            window.progress_toggle.click()
            window.transcript_toggle.click()
            self.qt_app.processEvents()
            self.assertTrue(window.progress_content.isHidden())
            self.assertTrue(window.transcript_content.isHidden())
            self.assertEqual(window.progress_toggle.text(), "展开")
            self.assertEqual(window.transcript_toggle.text(), "展开")
            self.assertGreater(window.answer.height(), initial_answer_height)

            window.answer_maximize_button.click()
            self.qt_app.processEvents()
            self.assertTrue(window.answer_maximized)
            self.assertTrue(window.progress_group.isHidden())
            self.assertTrue(window.transcript_group.isHidden())
            self.assertTrue(window.question_group.isVisibleTo(window))
            self.assertGreater(window.answer.height(), initial_answer_height)

            window.answer_maximize_button.click()
            self.qt_app.processEvents()
            self.assertFalse(window.answer_maximized)
            self.assertFalse(window.progress_group.isHidden())
            self.assertFalse(window.transcript_group.isHidden())
            self.assertTrue(window.progress_content.isHidden())
            self.assertTrue(window.transcript_content.isHidden())
            window.progress_toggle.click()
            window.transcript_toggle.click()
            self.assertFalse(window.progress_content.isHidden())
            self.assertFalse(window.transcript_content.isHidden())
        finally:
            window.close()

    def test_answer_record_does_not_repeat_persona_or_model(self) -> None:
        window = self.make_window()
        try:
            settings = AppParameters(identity="独特测试人设")
            window.events.put(("record_new", (1, datetime.now(), time.monotonic(), "手动", "你好", None, settings, None, None)))
            window.events.put(("answer", (1, "你好", "你好，很高兴见到你。", 0.1)))
            window._poll_events()
            answer_text = window.answer.toPlainText()
            self.assertIn("片段 #1", answer_text)
            self.assertIn("你好，很高兴见到你。", answer_text)
            self.assertNotIn("独特测试人设", answer_text)
            self.assertNotIn(window.records[1]["model_label"], answer_text)
        finally:
            window.close()


class SettingsValidationTests(unittest.TestCase):
    def test_active_provider_address_is_validated_on_save(self) -> None:
        with self.assertRaisesRegex(ValueError, "本地模型地址"):
            AppParameters(provider="local", server_url="https://example.com").validate()
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            AppParameters(provider="compatible", compatible_url="http://example.com", compatible_model="demo").validate()
        with self.assertRaisesRegex(ValueError, "基础地址"):
            AppParameters(
                provider="compatible",
                compatible_url="https://example.com/chat/completions",
                compatible_model="demo",
            ).validate()
        AppParameters(provider="openai", server_url="", gpt_model="demo").validate()


if __name__ == "__main__":
    unittest.main()
