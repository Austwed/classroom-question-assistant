"""System audio and text input to a selected answer model."""

from __future__ import annotations

import queue
import os
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import qrcode

# soundcard initializes COM on import. Qt's Windows clipboard needs the main
# thread to enter the OLE apartment first.
if sys.platform == "win32":
    import ctypes

    ctypes.windll.ole32.OleInitialize(None)

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPixmap, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QColorDialog,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from audio_capture import default_output_id, listen, output_devices
from conversation_memory import ConversationMemory
from credential_store import delete_key, load_key, save_key, target_for
from engine import ClassroomResponder, CompatibleChat, LocalASR, LocalLlama, OpenAIGPT
from mobile_share import MobileShare, lan_ipv4_addresses
from settings import AppParameters, Persona, load_personas, save_personas


class CopyableText:
    def copy_visible(self) -> None:
        selected = self.textCursor().selectedText().replace("\u2029", "\n")
        QApplication.clipboard().setText(selected or self.toPlainText())

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_visible()
            event.accept()
        else:
            super().keyPressEvent(event)


class CopyableLog(CopyableText, QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)


class ColoredAnswerLog(CopyableText, QTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)

    def append_answer(self, number: int, statement: str, answer: str, color: str, record_id: int | None = None, context: str = "") -> None:
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.document().isEmpty():
            cursor.insertBlock()
            cursor.insertBlock()
        normal = QTextCharFormat()
        normal.setForeground(self.palette().text().color())
        source = f" · 片段 #{record_id}" if record_id is not None else ""
        context_text = f" · {context}" if context else ""
        cursor.insertText(f"[{datetime.now():%H:%M:%S}] 第 {number} 条{source}{context_text} · 发言：{statement}", normal)
        cursor.insertBlock()
        colored = QTextCharFormat()
        colored.setForeground(QColor(color))
        cursor.insertText(f"回答：{answer}", colored)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()


class ClassroomApp(QMainWindow):
    MAX_AUDIO_RETRY_SEGMENTS = 3
    MAX_AUDIO_RETRY_BYTES = 12_000_000

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("通用语音问答助手 · 测试版")
        self.resize(980, 760)
        self.setMinimumSize(740, 560)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.asr_tasks: queue.Queue[tuple] = queue.Queue(maxsize=6)
        self.answer_tasks: queue.Queue[tuple] = queue.Queue(maxsize=6)
        self.record_lock = threading.Lock()
        self.next_record_id = 1
        self.next_session_id = 1
        self.current_session_id: int | None = None
        self.records: dict[int, dict] = {}
        self.retry_audio: OrderedDict[int, tuple] = OrderedDict()
        self.retry_audio_bytes = 0
        self.retry_audio_lock = threading.Lock()
        self.stop_event: threading.Event | None = None
        self.listener_running = False
        self.last_transcript = ""
        self.last_transcript_record_id: int | None = None
        self.correction_source_id: int | None = None
        self.capture_error = ""
        self.conversation_entries: list[tuple[datetime, str, str]] = []
        self.mobile_answer_entries: list[dict] = []
        self.mobile_share = MobileShare()
        self.share_dialog: QDialog | None = None
        self.conversation_memory = ConversationMemory()
        self.asr = LocalASR()
        self.active_asr_backend: str | None = None
        self.preload_target: str | None = None
        self.preload_generation = 0
        self.session_responder: ClassroomResponder | None = None
        self.session_settings: AppParameters | None = None
        try:
            self.parameters = AppParameters.load()
            self.parameters_error = ""
        except Exception as exc:
            self.parameters = AppParameters()
            self.parameters_error = f"自定义参数读取失败，已使用默认值：{exc}"
        try:
            self.personas = load_personas()
            self.personas_error = ""
        except Exception as exc:
            self.personas = {}
            self.personas_error = f"人设读取失败：{exc}"
        self._build_ui()
        self._refresh_devices()
        if self.parameters_error:
            self._append(self.transcript, self.parameters_error)
        if self.personas_error:
            self._append(self.transcript, self.personas_error)

        threading.Thread(target=self._asr_worker, daemon=True).start()
        threading.Thread(target=self._answer_worker, daemon=True).start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_events)
        self.timer.start(100)
        self._begin_asr_preload(self.parameters.asr_backend)

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        classroom = QWidget()
        tabs.addTab(classroom, "问答")
        layout = QVBoxLayout(classroom)

        controls = QHBoxLayout()
        layout.addLayout(controls)
        self.start_button = QPushButton("开始监听")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("停止监听")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        controls.addWidget(self.stop_button)
        self.status_label = QLabel("准备就绪")
        controls.addWidget(self.status_label, 1)
        self.level_label = QLabel("音量：0.000")
        controls.addWidget(self.level_label)
        export_button = QPushButton("导出对话记录")
        export_button.clicked.connect(self._export_conversation)
        controls.addWidget(export_button)

        self.asr_status_label = QLabel("语音识别：准备加载")
        layout.addWidget(self.asr_status_label)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        share_controls = QHBoxLayout()
        layout.addLayout(share_controls)
        self.share_button = QPushButton("开启手机共享")
        self.share_button.clicked.connect(self._toggle_mobile_share)
        share_controls.addWidget(self.share_button)
        self.share_qr_button = QPushButton("查看手机扫码地址")
        self.share_qr_button.setEnabled(False)
        self.share_qr_button.clicked.connect(self._show_mobile_share_dialog)
        share_controls.addWidget(self.share_qr_button)
        self.share_status_label = QLabel("手机共享未开启")
        share_controls.addWidget(self.share_status_label, 1)

        self.question_group = QGroupBox("手动提问或纠正转写")
        test_layout = QHBoxLayout(self.question_group)
        layout.addWidget(self.question_group)
        self.test_edit = QLineEdit()
        self.test_edit.setPlaceholderText("输入问题后按 Enter，或点击生成回答")
        self.test_edit.returnPressed.connect(self._test_question)
        test_layout.addWidget(self.test_edit, 1)
        test_button = QPushButton("生成回答")
        test_button.clicked.connect(self._test_question)
        test_layout.addWidget(test_button)
        reuse_button = QPushButton("载入最近转写")
        reuse_button.clicked.connect(self._load_last_transcript)
        test_layout.addWidget(reuse_button)
        copy_last_button = QPushButton("复制最近转写")
        copy_last_button.clicked.connect(self._copy_last_transcript)
        test_layout.addWidget(copy_last_button)
        self.cancel_correction_button = QPushButton("取消纠正")
        self.cancel_correction_button.setEnabled(False)
        self.cancel_correction_button.clicked.connect(self._cancel_correction)
        test_layout.addWidget(self.cancel_correction_button)

        self.progress_group = QGroupBox("逐段处理进度（双击语音条目可载入转写）")
        progress_layout = QVBoxLayout(self.progress_group)
        self.progress_toggle = QPushButton("收起")
        self.progress_toggle.clicked.connect(self._toggle_progress_section)
        progress_layout.addWidget(self.progress_toggle, alignment=Qt.AlignmentFlag.AlignRight)
        self.progress_content = QWidget()
        progress_content_layout = QVBoxLayout(self.progress_content)
        progress_content_layout.setContentsMargins(0, 0, 0, 0)
        self.progress = QListWidget()
        self.progress.setWordWrap(True)
        self.progress.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.progress.setMinimumHeight(100)
        self.progress.setMaximumHeight(150)
        self.progress.itemDoubleClicked.connect(self._load_selected_transcript)
        progress_content_layout.addWidget(self.progress)
        retry_button = QPushButton("重试选中片段（沿用原人设与模型）")
        retry_button.clicked.connect(self._retry_selected)
        progress_content_layout.addWidget(retry_button)
        progress_layout.addWidget(self.progress_content)
        self.progress_expanded_max_height = self.progress_group.maximumHeight()
        layout.addWidget(self.progress_group)

        self.transcript_group = QGroupBox("识别记录")
        transcript_layout = QVBoxLayout(self.transcript_group)
        self.transcript_toggle = QPushButton("收起")
        self.transcript_toggle.clicked.connect(self._toggle_transcript_section)
        transcript_layout.addWidget(self.transcript_toggle, alignment=Qt.AlignmentFlag.AlignRight)
        self.transcript_content = QWidget()
        transcript_content_layout = QVBoxLayout(self.transcript_content)
        transcript_content_layout.setContentsMargins(0, 0, 0, 0)
        self.transcript = CopyableLog()
        transcript_content_layout.addWidget(self.transcript)
        copy_transcript = QPushButton("复制识别记录（选中内容或全部）")
        copy_transcript.clicked.connect(self.transcript.copy_visible)
        transcript_content_layout.addWidget(copy_transcript)
        transcript_layout.addWidget(self.transcript_content)
        self.transcript_expanded_max_height = self.transcript_group.maximumHeight()

        answer_group = QGroupBox("回答")
        answer_layout = QVBoxLayout(answer_group)
        self.answer_maximize_button = QPushButton("最大化回答")
        self.answer_maximize_button.clicked.connect(self._toggle_answer_maximized)
        answer_layout.addWidget(self.answer_maximize_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.answer = ColoredAnswerLog()
        answer_layout.addWidget(self.answer)
        copy_answer = QPushButton("复制回答（选中内容或全部）")
        copy_answer.clicked.connect(self.answer.copy_visible)
        answer_layout.addWidget(copy_answer)
        self.answer_splitter = QSplitter(Qt.Orientation.Vertical)
        self.answer_splitter.addWidget(self.transcript_group)
        self.answer_splitter.addWidget(answer_group)
        self.answer_splitter.setChildrenCollapsible(False)
        self.answer_splitter.setSizes([210, 280])
        self.answer_maximized = False
        self.answer_splitter_sizes = [210, 280]
        layout.addWidget(self.answer_splitter, 1)

        settings_root = QWidget()
        tabs.addTab(settings_root, "设置")
        settings_layout = QVBoxLayout(settings_root)
        settings_tabs = QTabWidget()
        self.settings_tabs = settings_tabs
        settings_layout.addWidget(settings_tabs, 1)

        def settings_page(title: str) -> QVBoxLayout:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            page = QWidget()
            scroll.setWidget(page)
            settings_tabs.addTab(scroll, title)
            return QVBoxLayout(page)

        persona_layout = settings_page("人设")
        model_layout = settings_page("回答模型与密钥")
        audio_layout = settings_page("语音与显示")

        profile = QGroupBox("人设场景（通用回答规则固定，切换人设只替换本区内容）")
        profile_form = QGridLayout(profile)
        profile_form.addWidget(QLabel("身份："), 0, 0)
        self.identity_edit = QLineEdit(self.parameters.identity)
        self.identity_edit.setPlaceholderText("例如：编程导师、旅行顾问、通用问答助手")
        profile_form.addWidget(self.identity_edit, 0, 1)
        profile_form.addWidget(QLabel("场景与对象："), 1, 0)
        self.scenario_edit = QLineEdit(self.parameters.scenario)
        self.scenario_edit.setPlaceholderText("例如：在网课中对学生解释概念")
        profile_form.addWidget(self.scenario_edit, 1, 1)
        profile_form.addWidget(QLabel("背景事实："), 2, 0)
        self.background_edit = QTextEdit(self.parameters.background)
        self.background_edit.setPlaceholderText("写这个身份已确定的经历、技能或业务事实；不确定的不要编造。")
        self.background_edit.setMaximumHeight(85)
        profile_form.addWidget(self.background_edit, 2, 1)
        profile_form.addWidget(QLabel("知识与回答范围："), 3, 0)
        self.knowledge_scope_edit = QTextEdit(self.parameters.knowledge_scope)
        self.knowledge_scope_edit.setPlaceholderText("擅长哪些内容；超出范围时如何回应。")
        self.knowledge_scope_edit.setMaximumHeight(75)
        profile_form.addWidget(self.knowledge_scope_edit, 3, 1)
        profile_form.addWidget(QLabel("说话方式："), 4, 0)
        self.speaking_style_edit = QTextEdit(self.parameters.speaking_style)
        self.speaking_style_edit.setPlaceholderText("例如：称呼对方为同学；用第一人称；回答约 3 句。")
        self.speaking_style_edit.setMaximumHeight(75)
        profile_form.addWidget(self.speaking_style_edit, 4, 1)
        profile_form.addWidget(QLabel("常见问题要点："), 5, 0)
        self.prepared_answers_edit = QTextEdit(self.parameters.prepared_answers)
        self.prepared_answers_edit.setPlaceholderText("可选。写高频问题及真实、可核对的回答要点。")
        self.prepared_answers_edit.setMaximumHeight(100)
        profile_form.addWidget(self.prepared_answers_edit, 5, 1)
        profile_form.addWidget(QLabel("其他场景指令："), 6, 0)
        self.instructions_edit = QTextEdit(self.parameters.instructions)
        self.instructions_edit.setPlaceholderText("旧人设的补充指令会保留在这里；只写此场景特有的要求。")
        self.instructions_edit.setMaximumHeight(90)
        profile_form.addWidget(self.instructions_edit, 6, 1)
        profile_form.addWidget(QLabel("已保存的人设："), 7, 0)
        self.persona_box = QComboBox()
        profile_form.addWidget(self.persona_box, 7, 1)
        persona_actions = QHBoxLayout()
        apply_persona = QPushButton("应用选中人设")
        apply_persona.clicked.connect(self._apply_persona)
        persona_actions.addWidget(apply_persona)
        save_persona = QPushButton("保存当前为人设")
        save_persona.clicked.connect(self._save_persona)
        persona_actions.addWidget(save_persona)
        profile_form.addLayout(persona_actions, 8, 1)
        self._refresh_persona_box()
        self.persona_hint = QLabel("从列表选中人设后，点击“应用选中人设”才会切换；编辑字段不会覆盖已保存的人设。")
        self.persona_hint.setWordWrap(True)
        persona_layout.addWidget(self.persona_hint)
        persona_layout.addWidget(profile)
        persona_layout.addStretch()

        model_group = QGroupBox("回答服务")
        form = QGridLayout(model_group)
        model_layout.addWidget(model_group)
        model_layout.addStretch()

        voice_group = QGroupBox("收音与语音识别")
        voice_form = QGridLayout(voice_group)
        audio_layout.addWidget(voice_group)

        voice_form.addWidget(QLabel("播放设备："), 0, 0)
        self.device_box = QComboBox()
        voice_form.addWidget(self.device_box, 0, 1)
        refresh = QPushButton("刷新设备")
        refresh.clicked.connect(self._refresh_devices)
        voice_form.addWidget(refresh, 0, 2)

        form.addWidget(QLabel("回答模型："), 0, 0)
        self.provider_box = QComboBox()
        self.provider_box.addItem("本地 llama.cpp", "local")
        self.provider_box.addItem("OpenAI GPT（云端）", "openai")
        self.provider_box.addItem("兼容 API（DeepSeek 等）", "compatible")
        self.provider_box.setCurrentIndex(self.provider_box.findData(self.parameters.provider))
        self.provider_box.currentIndexChanged.connect(self._provider_changed)
        form.addWidget(self.provider_box, 0, 1, 1, 2)

        self.server_label = QLabel("本地模型地址：")
        form.addWidget(self.server_label, 1, 0)
        self.server_edit = QLineEdit(self.parameters.server_url)
        form.addWidget(self.server_edit, 1, 1, 1, 2)
        self.model_label = QLabel("GPT 模型：")
        form.addWidget(self.model_label, 2, 0)
        self.model_edit = QLineEdit(self.parameters.gpt_model)
        form.addWidget(self.model_edit, 2, 1, 1, 2)
        self.key_label = QLabel("API Key：")
        form.addWidget(self.key_label, 5, 0)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("输入后自动保存到 Windows 凭据库")
        key_controls = QHBoxLayout()
        key_controls.addWidget(self.key_edit, 1)
        self.show_key_button = QPushButton("显示")
        self.show_key_button.setCheckable(True)
        self.show_key_button.toggled.connect(self._toggle_key_visibility)
        key_controls.addWidget(self.show_key_button)
        self.key_state_label = QLabel()
        key_controls.addWidget(self.key_state_label)
        self.clear_key_button = QPushButton("清除已保存 Key")
        self.clear_key_button.clicked.connect(self._clear_saved_key)
        key_controls.addWidget(self.clear_key_button)
        form.addLayout(key_controls, 5, 1, 1, 2)
        self.privacy_label = QLabel()
        self.privacy_label.setWordWrap(True)
        form.addWidget(self.privacy_label, 6, 0, 1, 3)

        voice_form.addWidget(QLabel("声音阈值："), 1, 0)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setDecimals(4)
        self.threshold_spin.setRange(0.0001, 0.9999)
        self.threshold_spin.setSingleStep(0.001)
        self.threshold_spin.setValue(self.parameters.threshold)
        self.threshold_spin.setToolTip("数值越低越容易触发；只有音量变化、没有转写时可适当调低。")
        voice_form.addWidget(self.threshold_spin, 1, 1)
        voice_form.addWidget(QLabel("越低越敏感；背景声误触发时调高"), 1, 2)
        voice_form.addWidget(QLabel("发言结束停顿："), 2, 0)
        self.silence_spin = QDoubleSpinBox()
        self.silence_spin.setDecimals(1)
        self.silence_spin.setRange(0.2, 10.0)
        self.silence_spin.setSingleStep(0.2)
        self.silence_spin.setSuffix(" 秒")
        self.silence_spin.setValue(self.parameters.silence_seconds)
        voice_form.addWidget(self.silence_spin, 2, 1)
        voice_form.addWidget(QLabel("较长停顿可减少句中切断，但回答开始更晚"), 2, 2)

        display_group = QGroupBox("文字显示")
        display_form = QGridLayout(display_group)
        audio_layout.addWidget(display_group)
        audio_layout.addStretch()

        display_form.addWidget(QLabel("识别记录字体："), 0, 0)
        self.transcript_font_spin = QSpinBox()
        self.transcript_font_spin.setRange(8, 32)
        self.transcript_font_spin.setSuffix(" 磅")
        self.transcript_font_spin.setValue(self.parameters.transcript_font_size)
        self.transcript_font_spin.valueChanged.connect(self._apply_fonts)
        display_form.addWidget(self.transcript_font_spin, 0, 1)
        display_form.addWidget(QLabel("回答字体："), 1, 0)
        self.answer_font_spin = QSpinBox()
        self.answer_font_spin.setRange(8, 32)
        self.answer_font_spin.setSuffix(" 磅")
        self.answer_font_spin.setValue(self.parameters.answer_font_size)
        self.answer_font_spin.valueChanged.connect(self._apply_fonts)
        display_form.addWidget(self.answer_font_spin, 1, 1)

        self.answer_color_odd = self.parameters.answer_color_odd
        self.answer_color_even = self.parameters.answer_color_even
        display_form.addWidget(QLabel("奇数条回答颜色："), 2, 0)
        self.odd_color_button = QPushButton()
        self.odd_color_button.clicked.connect(lambda: self._choose_answer_color("odd"))
        display_form.addWidget(self.odd_color_button, 2, 1)
        display_form.addWidget(QLabel("用于第 1、3、5 条回答"), 2, 2)
        display_form.addWidget(QLabel("偶数条回答颜色："), 3, 0)
        self.even_color_button = QPushButton()
        self.even_color_button.clicked.connect(lambda: self._choose_answer_color("even"))
        display_form.addWidget(self.even_color_button, 3, 1)
        display_form.addWidget(QLabel("用于第 2、4、6 条回答"), 3, 2)
        self._update_color_buttons()

        voice_form.addWidget(QLabel("语音识别方案："), 3, 0)
        self.asr_box = QComboBox()
        self.asr_box.addItem("中文增强 · FunASR Paraformer", "funasr")
        self.asr_box.addItem("兼容模式 · Whisper base", "whisper-base")
        self.asr_box.setCurrentIndex(self.asr_box.findData(self.parameters.asr_backend))
        self.asr_box.currentIndexChanged.connect(self._asr_selection_changed)
        voice_form.addWidget(self.asr_box, 3, 1)
        reload_asr = QPushButton("重新加载")
        reload_asr.clicked.connect(self._reload_asr)
        voice_form.addWidget(reload_asr, 3, 2)
        voice_form.addWidget(QLabel("专有词："), 4, 0)
        self.hotwords_edit = QLineEdit(self.parameters.hotwords)
        self.hotwords_edit.setPlaceholderText("用逗号隔开；仅中文增强识别方案使用")
        voice_form.addWidget(self.hotwords_edit, 4, 1, 1, 2)
        self._asr_selection_changed()

        self.compatible_url_label = QLabel("兼容 API 地址：")
        form.addWidget(self.compatible_url_label, 3, 0)
        self.compatible_url_edit = QLineEdit(self.parameters.compatible_url)
        self.compatible_url_edit.setPlaceholderText("例如 https://api.deepseek.com")
        self.compatible_url_edit.textChanged.connect(self._compatible_url_changed)
        form.addWidget(self.compatible_url_edit, 3, 1)
        self.deepseek_preset_button = QPushButton("填入 DeepSeek 预设")
        self.deepseek_preset_button.clicked.connect(self._set_deepseek_preset)
        form.addWidget(self.deepseek_preset_button, 3, 2)
        self.compatible_model_label = QLabel("兼容模型名：")
        form.addWidget(self.compatible_model_label, 4, 0)
        self.compatible_model_edit = QLineEdit(self.parameters.compatible_model)
        self.compatible_model_edit.setPlaceholderText("例如 deepseek-flash")
        form.addWidget(self.compatible_model_edit, 4, 1, 1, 2)

        settings_actions = QHBoxLayout()
        self.dirty_label = QLabel("设置已保存")
        settings_actions.addWidget(self.dirty_label, 1)
        self.save_parameters_button = QPushButton("保存设置")
        self.save_parameters_button.clicked.connect(self._save_parameters)
        settings_actions.addWidget(self.save_parameters_button)
        settings_layout.addLayout(settings_actions)
        self.parameters_status = QLabel("手动提问使用当前界面设置；正在监听的语音从下次监听起使用新设置。API Key 默认保存到 Windows 凭据库。")
        self.parameters_status.setWordWrap(True)
        settings_layout.addWidget(self.parameters_status)
        self._provider_changed()
        self._apply_fonts()
        self._update_config_state()

    def _toggle_progress_section(self) -> None:
        expanded = not self.progress_content.isVisibleTo(self.progress_group)
        if expanded:
            self.progress_group.setMaximumHeight(self.progress_expanded_max_height)
        self.progress_content.setVisible(expanded)
        if not expanded:
            self.progress_group.setMaximumHeight(self.progress_group.minimumSizeHint().height())
        self.progress_toggle.setText("收起" if expanded else "展开")

    def _toggle_transcript_section(self) -> None:
        expanded = not self.transcript_content.isVisibleTo(self.transcript_group)
        if expanded:
            self.transcript_group.setMaximumHeight(self.transcript_expanded_max_height)
        self.transcript_content.setVisible(expanded)
        if not expanded:
            self.transcript_group.setMaximumHeight(self.transcript_group.minimumSizeHint().height())
        self.transcript_toggle.setText("收起" if expanded else "展开")

    def _toggle_answer_maximized(self) -> None:
        self.answer_maximized = not self.answer_maximized
        if self.answer_maximized:
            self.answer_splitter_sizes = self.answer_splitter.sizes()
            self.progress_group.hide()
            self.transcript_group.hide()
            self.answer_maximize_button.setText("还原回答")
        else:
            self.progress_group.show()
            self.transcript_group.show()
            self.answer_splitter.setSizes(self.answer_splitter_sizes)
            self.answer_maximize_button.setText("最大化回答")

    def _refresh_devices(self) -> None:
        old_id = self.device_box.currentData()
        try:
            devices = output_devices()
            preferred = old_id or self.parameters.output_id or default_output_id()
            self.device_box.clear()
            for name, identifier in devices:
                self.device_box.addItem(name, identifier)
            index = self.device_box.findData(preferred)
            if index >= 0:
                self.device_box.setCurrentIndex(index)
            if not devices:
                self.status_label.setText("没有发现播放设备")
        except Exception as exc:
            self.status_label.setText(f"列出设备失败：{exc}")

    def _provider_changed(self) -> None:
        provider = self.provider_box.currentData()
        previous = getattr(self, "_previous_provider", None)
        if previous is not None and previous != provider:
            self.key_edit.clear()
            self.show_key_button.setChecked(False)
        self._previous_provider = provider
        for widget in (self.server_label, self.server_edit):
            widget.setVisible(provider == "local")
        for widget in (self.model_label, self.model_edit):
            widget.setVisible(provider == "openai")
        for widget in (self.compatible_url_label, self.compatible_url_edit, self.deepseek_preset_button,
                       self.compatible_model_label, self.compatible_model_edit):
            widget.setVisible(provider == "compatible")
        for widget in (self.key_label, self.key_edit, self.show_key_button, self.key_state_label,
                       self.clear_key_button, self.privacy_label):
            widget.setVisible(provider in {"openai", "compatible"})
        if provider == "openai":
            self.key_edit.setPlaceholderText("输入后自动保存；留空使用已保存 Key 或 OPENAI_API_KEY")
            self.privacy_label.setText("转写文字和当前人设的全部场景内容发送到 OpenAI；原始音频留在本机。")
        elif provider == "compatible":
            self.key_edit.setPlaceholderText("输入后自动保存；留空使用已保存 Key 或环境变量")
            self.privacy_label.setText("转写文字和当前人设的全部场景内容发送到所填 API 服务；原始音频留在本机。")
        self._refresh_key_state()

    def _toggle_key_visibility(self, visible: bool) -> None:
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.show_key_button.setText("隐藏" if visible else "显示")

    def _asr_selection_changed(self) -> None:
        funasr = self.asr_box.currentData() == "funasr"
        self.hotwords_edit.setEnabled(funasr)
        self.hotwords_edit.setToolTip("仅 FunASR 中文增强识别使用专有词" if funasr else "Whisper base 不使用专有词；已填写内容会保留")

    def _update_config_state(self, edited: bool | None = None) -> None:
        current = self._form_parameters()
        if edited is None:
            edited = current != self.parameters
        key_entered = bool(self.key_edit.text().strip())
        parts = ["有未保存修改" if edited else "设置已保存"]
        if key_entered:
            parts.append("API Key 待保存")
        self.dirty_label.setText(" · ".join(parts))
        self.save_parameters_button.setEnabled(edited or key_entered)
        matched = self._matching_persona_name(current)
        selected = self.persona_box.currentData()
        hint = f"当前编辑内容匹配已保存人设“{matched}”。" if matched else "当前编辑内容为自定义人设。"
        if selected and selected != matched:
            hint += f" 列表已选中“{selected}”，点击“应用选中人设”才会切换。"
        else:
            hint += " 编辑字段不会自动覆盖已保存的人设。"
        self.persona_hint.setText(hint)

    def _matching_persona_name(self, parameters: AppParameters) -> str | None:
        fields = ("identity", "scenario", "background", "knowledge_scope", "speaking_style", "prepared_answers", "instructions")
        for name, persona in self.personas.items():
            if all(getattr(persona, field) == getattr(parameters, field) for field in fields):
                return name
        return None

    def _credential_target(self) -> str:
        return target_for(self.provider_box.currentData(), self.compatible_url_edit.text())

    def _compatible_url_changed(self) -> None:
        if self.provider_box.currentData() == "compatible":
            self.key_edit.clear()
            self._refresh_key_state()

    def _refresh_key_state(self) -> None:
        if self.provider_box.currentData() not in {"openai", "compatible"}:
            self.key_state_label.setText("")
            return
        try:
            target = self._credential_target()
            saved = load_key(target) is not None
            if saved:
                self.key_state_label.setText("已保存")
            else:
                provider = self.provider_box.currentData()
                url = self.compatible_url_edit.text().strip()
                env_name = "OPENAI_API_KEY" if provider == "openai" else (
                    "DEEPSEEK_API_KEY" if urlparse(url).hostname == "api.deepseek.com" else "COMPATIBLE_API_KEY"
                )
                self.key_state_label.setText("环境变量可用" if os.environ.get(env_name, "").strip() else "未保存")
        except ValueError:
            self.key_state_label.setText("先填地址")
        except OSError:
            self.key_state_label.setText("凭据库不可用")

    def _saved_key(self) -> str | None:
        try:
            return load_key(self._credential_target())
        except OSError:
            self.key_state_label.setText("凭据库不可用")
            return None

    def _persist_entered_key(self) -> None:
        entered = self.key_edit.text().strip()
        if not entered or self.provider_box.currentData() not in {"openai", "compatible"}:
            return
        if self.provider_box.currentData() == "compatible":
            CompatibleChat(self.compatible_url_edit.text(), self.compatible_model_edit.text(), entered)
        save_key(self._credential_target(), entered)
        self.key_edit.clear()
        self._refresh_key_state()

    def _clear_saved_key(self) -> None:
        try:
            removed = delete_key(self._credential_target())
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "清除 API Key 失败", str(exc))
            return
        self.key_edit.clear()
        self._refresh_key_state()
        self.parameters_status.setText(
            "已清除当前服务的已保存 Key；环境变量仍可能提供 Key。"
            if removed else "当前服务没有已保存的 Key；环境变量仍可能提供 Key。"
        )

    def _set_deepseek_preset(self) -> None:
        self.provider_box.setCurrentIndex(self.provider_box.findData("compatible"))
        self.compatible_url_edit.setText("https://api.deepseek.com")
        self.compatible_model_edit.setText("deepseek-flash")
        self.parameters_status.setText("已填入 DeepSeek 地址和模型；请填写 API Key 并保存自定义参数。")

    def _apply_fonts(self) -> None:
        for widget, size in (
            (self.transcript, self.transcript_font_spin.value()),
            (self.answer, self.answer_font_spin.value()),
        ):
            font = widget.font()
            font.setPointSize(size)
            widget.setFont(font)

    def _update_color_buttons(self) -> None:
        for button, color in (
            (self.odd_color_button, self.answer_color_odd),
            (self.even_color_button, self.answer_color_even),
        ):
            foreground = "#ffffff" if QColor(color).lightness() < 150 else "#202020"
            button.setText(color.upper())
            button.setStyleSheet(f"background-color: {color}; color: {foreground};")

    def _choose_answer_color(self, parity: str) -> None:
        current = self.answer_color_odd if parity == "odd" else self.answer_color_even
        selected = QColorDialog.getColor(QColor(current), self, "选择回答文字颜色")
        if not selected.isValid():
            return
        if parity == "odd":
            self.answer_color_odd = selected.name()
        else:
            self.answer_color_even = selected.name()
        self._update_color_buttons()

    def _refresh_persona_box(self, selected_name: str | None = None) -> None:
        self.persona_box.clear()
        self.persona_box.addItem("选择已保存的人设", None)
        for name in self.personas:
            self.persona_box.addItem(name, name)
        index = self.persona_box.findData(selected_name)
        if index > 0:
            self.persona_box.setCurrentIndex(index)

    def _activate_persona(self, persona: Persona) -> bool:
        self.identity_edit.setText(persona.identity)
        self.scenario_edit.setText(persona.scenario)
        self.background_edit.setPlainText(persona.background)
        self.knowledge_scope_edit.setPlainText(persona.knowledge_scope)
        self.speaking_style_edit.setPlainText(persona.speaking_style)
        self.prepared_answers_edit.setPlainText(persona.prepared_answers)
        self.instructions_edit.setPlainText(persona.instructions)
        if not self._save_parameters():
            return False
        self.parameters_status.setText(f"已应用人设“{persona.name}”；手动提问立即使用，正在监听的语音从下次监听起使用。")
        self._update_config_state()
        return True

    def _apply_persona(self) -> None:
        name = self.persona_box.currentData()
        if name is None:
            self.parameters_status.setText("请先选择一个已保存的人设。")
            return
        self._activate_persona(self.personas[name])

    def _save_persona(self) -> None:
        if self.personas_error:
            QMessageBox.critical(self, "无法保存人设", "原有人设文件读取失败。为保留原文件，请先检查识别记录中的错误。")
            return
        name, accepted = QInputDialog.getText(self, "保存人设", "人设名称：")
        if not accepted:
            return
        name = name.strip()
        persona = Persona(
            name=name,
            identity=self.identity_edit.text().strip(),
            instructions=self.instructions_edit.toPlainText().strip(),
            scenario=self.scenario_edit.text().strip(),
            background=self.background_edit.toPlainText().strip(),
            knowledge_scope=self.knowledge_scope_edit.toPlainText().strip(),
            speaking_style=self.speaking_style_edit.toPlainText().strip(),
            prepared_answers=self.prepared_answers_edit.toPlainText().strip(),
        )
        try:
            persona.validate()
        except ValueError as exc:
            QMessageBox.critical(self, "人设无效", str(exc))
            return
        if name in self.personas:
            choice = QMessageBox.question(self, "覆盖人设", f"“{name}”已存在，是否用当前内容覆盖？")
            if choice != QMessageBox.StandardButton.Yes:
                return
        updated = dict(self.personas)
        updated[name] = persona
        try:
            save_personas(updated)
        except Exception as exc:
            QMessageBox.critical(self, "保存人设失败", str(exc))
            return
        self.personas = updated
        self._refresh_persona_box(name)
        self._activate_persona(persona)

    def _form_parameters(self) -> AppParameters:
        return AppParameters(
            identity=self.identity_edit.text().strip(),
            scenario=self.scenario_edit.text().strip(),
            background=self.background_edit.toPlainText().strip(),
            knowledge_scope=self.knowledge_scope_edit.toPlainText().strip(),
            speaking_style=self.speaking_style_edit.toPlainText().strip(),
            prepared_answers=self.prepared_answers_edit.toPlainText().strip(),
            instructions=self.instructions_edit.toPlainText().strip(),
            hotwords=self.hotwords_edit.text().strip(),
            asr_backend=self.asr_box.currentData(),
            output_id=self.device_box.currentData() or "",
            provider=self.provider_box.currentData(),
            server_url=self.server_edit.text().strip(),
            gpt_model=self.model_edit.text().strip(),
            compatible_url=self.compatible_url_edit.text().strip(),
            compatible_model=self.compatible_model_edit.text().strip(),
            threshold=self.threshold_spin.value(),
            silence_seconds=self.silence_spin.value(),
            transcript_font_size=self.transcript_font_spin.value(),
            answer_font_size=self.answer_font_spin.value(),
            answer_color_odd=self.answer_color_odd,
            answer_color_even=self.answer_color_even,
        )

    def _save_parameters(self) -> bool:
        parameters = self._form_parameters()
        try:
            parameters.validate()
            parameters.save()
        except Exception as exc:
            QMessageBox.critical(self, "参数保存失败", str(exc))
            return False
        key_error = ""
        try:
            self._persist_entered_key()
        except (OSError, ValueError) as exc:
            key_error = f"API Key 未能自动保存：{exc}；本次运行仍可使用已输入的 Key。"
        old_backend = self.parameters.asr_backend
        self.parameters = parameters
        if parameters.asr_backend != old_backend:
            if not self.listener_running:
                self._begin_asr_preload(parameters.asr_backend)
                self.parameters_status.setText("参数已保存；正在加载新语音识别模型。")
            else:
                self.parameters_status.setText("参数已保存；停止监听后加载新语音识别模型。")
        else:
            self.parameters_status.setText("参数已保存；手动提问立即使用，正在监听的语音从下次监听起使用。")
        if key_error:
            self.parameters_status.setText(self.parameters_status.text() + " " + key_error)
        self._update_config_state()
        return True

    def _begin_asr_preload(self, backend: str) -> None:
        self.preload_generation += 1
        generation = self.preload_generation
        self.preload_target = backend
        self.active_asr_backend = None
        self.start_button.setEnabled(False)
        self.asr_status_label.setText("语音识别：正在加载模型…")
        threading.Thread(target=self._preload_asr, args=(backend, generation), daemon=True).start()

    def _reload_asr(self) -> None:
        if self.listener_running:
            self.parameters_status.setText("请先停止监听，再重新加载语音模型。")
            return
        self._begin_asr_preload(self.asr_box.currentData())

    def _preload_asr(self, backend: str, generation: int) -> None:
        try:
            self.asr.prepare(backend)
            self.events.put(("asr_ready", (generation, backend, "")))
        except Exception as exc:
            if backend != "funasr":
                self.events.put(("asr_failed", (generation, str(exc))))
                return
            self.events.put(("log", f"中文增强识别加载失败：{exc}；尝试兼容模式"))
            try:
                self.asr.prepare("whisper-base")
                self.events.put(("asr_ready", (generation, "whisper-base", "中文增强不可用，已加载兼容模式")))
            except Exception as fallback_exc:
                self.events.put(("asr_failed", (generation, str(fallback_exc))))

    def _selected_responder(self) -> ClassroomResponder:
        provider = self.provider_box.currentData()
        if provider == "openai":
            entered = self.key_edit.text().strip()
            key = entered or self._saved_key() or os.environ.get("OPENAI_API_KEY", "").strip()
            responder = OpenAIGPT(key, self.model_edit.text())
            try:
                self._persist_entered_key()
            except OSError as exc:
                self.parameters_status.setText(f"API Key 未能自动保存：{exc}；本次运行仍可使用。")
            return responder
        if provider == "compatible":
            url = self.compatible_url_edit.text().strip()
            env_name = "DEEPSEEK_API_KEY" if urlparse(url).hostname == "api.deepseek.com" else "COMPATIBLE_API_KEY"
            entered = self.key_edit.text().strip()
            key = entered or self._saved_key() or os.environ.get(env_name, "").strip()
            responder = CompatibleChat(url, self.compatible_model_edit.text(), key)
            try:
                self._persist_entered_key()
            except OSError as exc:
                self.parameters_status.setText(f"API Key 未能自动保存：{exc}；本次运行仍可使用。")
            return responder
        return LocalLlama(self.server_edit.text().strip())

    def _start(self) -> None:
        if not self._save_parameters():
            return
        if self.active_asr_backend is None:
            self.status_label.setText("语音识别模型尚未就绪")
            return
        device_id = self.device_box.currentData()
        if not device_id:
            QMessageBox.critical(self, "无法监听", "请先选择播放设备")
            return
        try:
            self.session_responder = self._selected_responder()
        except ValueError as exc:
            QMessageBox.critical(self, "模型设定无效", str(exc))
            return
        self.stop_event = threading.Event()
        self.capture_error = ""
        session_id = self.next_session_id
        self.next_session_id += 1
        self.current_session_id = session_id
        self.listener_running = True
        self.session_provider_name = self.provider_box.currentText()
        self.session_device_name = self.device_box.currentText()
        self.session_settings = replace(self.parameters, asr_backend=self.active_asr_backend)
        threading.Thread(
            target=self._capture,
            args=(device_id, self.threshold_spin.value(), self.silence_spin.value(), self.stop_event, self.session_responder, self.session_settings, session_id),
            daemon=True,
        ).start()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status_label.setText(f"监听中：{self.device_box.currentText()}")
        self._append(self.transcript, f"开始监听：{self.device_box.currentText()}")

    def _capture(self, device_id: str, threshold: float, silence_seconds: float, stop: threading.Event, responder: ClassroomResponder, settings: AppParameters, session_id: int) -> None:
        try:
            listen(
                device_id,
                threshold,
                silence_seconds,
                stop,
                lambda level: self.events.put(("level", level)),
                lambda audio: self._submit_audio(audio, settings, responder, session_id),
            )
        except Exception as exc:
            self.events.put(("capture_failed", str(exc)))
        finally:
            stop.set()
            self.events.put(("listener_stopped", None))

    def _stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()
            self.status_label.setText("正在停止收音，已录入内容继续处理…")
            self.stop_button.setEnabled(False)

    @staticmethod
    def _model_label(settings: AppParameters | None) -> str:
        if settings is None:
            return "未记录模型"
        if settings.provider == "openai":
            return f"OpenAI {settings.gpt_model}"
        if settings.provider == "compatible":
            hostname = urlparse(settings.compatible_url).hostname or "兼容 API"
            return f"{hostname} · {settings.compatible_model}"
        address = urlparse(settings.server_url).netloc or settings.server_url
        return f"本地 llama.cpp {address}"

    def _new_record(
        self,
        source: str,
        statement: str = "",
        session_id: int | None = None,
        settings: AppParameters | None = None,
        responder: ClassroomResponder | None = None,
        parent_id: int | None = None,
    ) -> int:
        with self.record_lock:
            record_id = self.next_record_id
            self.next_record_id += 1
        self.events.put(("record_new", (record_id, datetime.now(), time.monotonic(), source, statement, session_id, settings, responder, parent_id)))
        return record_id

    def _submit_audio(self, audio, settings: AppParameters, responder: ClassroomResponder, session_id: int) -> None:
        record_id = self._new_record("语音", session_id=session_id, settings=settings, responder=responder)
        try:
            self.asr_tasks.put_nowait((record_id, audio, settings, responder, time.monotonic()))
        except queue.Full:
            retained = self._cache_audio_for_retry(record_id, audio, settings, responder)
            detail = "转写队列已满，音频已暂存，可选中重试" if retained else "转写队列已满，音频未保留"
            self.events.put(("record_state", (record_id, "未入队", detail)))
            self.events.put(("log", f"第 {record_id} 段：{detail}"))

    def _cache_audio_for_retry(self, record_id: int, audio, settings: AppParameters, responder: ClassroomResponder) -> bool:
        size = int(getattr(audio, "nbytes", 0))
        if size <= 0 or size > self.MAX_AUDIO_RETRY_BYTES:
            return False
        with self.retry_audio_lock:
            while self.retry_audio and (
                len(self.retry_audio) >= self.MAX_AUDIO_RETRY_SEGMENTS
                or self.retry_audio_bytes + size > self.MAX_AUDIO_RETRY_BYTES
            ):
                evicted_id, (_, _, _, evicted_size) = self.retry_audio.popitem(last=False)
                self.retry_audio_bytes -= evicted_size
                self.events.put(("retry_evicted", evicted_id))
            self.retry_audio[record_id] = (audio, settings, responder, size)
            self.retry_audio_bytes += size
        return True

    def _submit_answer(
        self,
        record_id: int,
        statement: str,
        settings: AppParameters,
        responder: ClassroomResponder,
        notify_full: bool = True,
        parent_id: int | None = None,
    ) -> bool:
        self.conversation_memory.record_question(record_id, statement, parent_id)
        try:
            self.answer_tasks.put_nowait((record_id, statement, settings, responder, time.monotonic()))
        except queue.Full:
            if notify_full:
                self.events.put(("record_state", (record_id, "未入队", "回答队列已满，可选中重试")))
                self.events.put(("log", f"第 {record_id} 段：回答队列已满，未生成回答"))
            return False
        return True

    def _test_question(self) -> None:
        statement = self.test_edit.text().strip()
        if statement:
            parent_id = self.correction_source_id
            if parent_id is not None:
                parent = self.records.get(parent_id)
                if parent is None or parent.get("settings") is None or parent.get("responder") is None:
                    self.status_label.setText("原片段的回答设置已不可用")
                    return
                settings = parent["settings"]
                responder = parent["responder"]
                session_id = parent["session_id"]
                source = "修正"
            else:
                try:
                    settings = self._form_parameters()
                    settings.validate()
                    responder = self._selected_responder()
                except ValueError as exc:
                    QMessageBox.critical(self, "模型设定无效", str(exc))
                    return
                session_id = None
                source = "手动"
            record_id = self._new_record(source, statement, session_id, settings, responder, parent_id)
            self._submit_answer(record_id, statement, settings, responder, parent_id=parent_id)
            self.test_edit.clear()
            self._cancel_correction()
            self._poll_events()

    def _cancel_correction(self) -> None:
        self.correction_source_id = None
        self.question_group.setTitle("手动提问或纠正转写")
        self.cancel_correction_button.setEnabled(False)

    def _latest_revision_id(self, record_id: int) -> int:
        visited = set()
        while record_id in self.records and record_id not in visited:
            visited.add(record_id)
            successor = self.records[record_id].get("pending_revision_id") or self.records[record_id].get("superseded_by")
            if successor is None:
                break
            record_id = successor
        return record_id

    def _load_selected_transcript(self, item: QListWidgetItem) -> None:
        record_id = self._latest_revision_id(item.data(Qt.ItemDataRole.UserRole))
        statement = self.records.get(record_id, {}).get("statement", "")
        if statement:
            self.test_edit.setText(statement)
            self.test_edit.setFocus()
            self.correction_source_id = record_id
            self.question_group.setTitle(f"正在纠正片段 #{record_id}（沿用原人设与模型）")
            self.cancel_correction_button.setEnabled(True)
        else:
            self.status_label.setText("该段尚无可载入的转写")

    def _load_last_transcript(self) -> None:
        if self.last_transcript_record_id in self.records:
            self._load_selected_transcript(self.records[self.last_transcript_record_id]["item"])
        else:
            self.status_label.setText("尚无语音转写")

    def _retry_selected(self) -> None:
        item = self.progress.currentItem()
        if item is None:
            self.status_label.setText("请先选中要重试的片段")
            return
        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = self.records.get(record_id)
        if record is None or record["status"] not in {"未入队", "失败"}:
            self.status_label.setText("这条片段当前不需要重试")
            return
        with self.retry_audio_lock:
            cached = self.retry_audio.get(record_id)
            if cached is not None:
                audio, settings, responder, size = cached
                try:
                    self.asr_tasks.put_nowait((record_id, audio, settings, responder, time.monotonic()))
                except queue.Full:
                    self.status_label.setText("转写队列仍满，请稍后重试")
                    return
                self.retry_audio.pop(record_id)
                self.retry_audio_bytes -= size
        if cached is not None:
            record["status"] = "等待转写"
            record["detail"] = "已重新入队"
        elif record["statement"] and record.get("settings") is not None and record.get("responder") is not None:
            if not self._submit_answer(record_id, record["statement"], record["settings"], record["responder"], notify_full=False, parent_id=record.get("parent_id")):
                self.status_label.setText("回答队列仍满，请稍后重试")
                return
            record["status"] = "等待回答"
            record["detail"] = "已重新入队"
        else:
            self.status_label.setText("该段没有保留可重试的音频或转写")
            return
        self._update_record_row(record_id)
        self._update_overall_status()

    def _copy_last_transcript(self) -> None:
        if self.last_transcript:
            QApplication.clipboard().setText(self.last_transcript)
            self.status_label.setText("已复制最近转写")
        else:
            self.status_label.setText("尚无语音转写")

    def _toggle_mobile_share(self) -> None:
        if self.mobile_share.active:
            self.mobile_share.stop()
            if self.share_dialog is not None:
                self.share_dialog.close()
                self.share_dialog = None
            self.share_button.setText("开启手机共享")
            self.share_qr_button.setEnabled(False)
            self.share_status_label.setText("手机共享未开启")
            return
        addresses = lan_ipv4_addresses()
        if not addresses:
            QMessageBox.warning(self, "无法开启手机共享", "未找到可供手机访问的局域网 IPv4 地址。请连接 Wi-Fi 后重试。")
            return
        address = addresses[0]
        if len(addresses) > 1:
            address, accepted = QInputDialog.getItem(
                self, "选择手机所在网络", "请选择手机能访问的电脑地址：", addresses, 0, False
            )
            if not accepted:
                return
        try:
            self.mobile_share.start(self.mobile_answer_entries, address)
        except OSError as exc:
            QMessageBox.critical(self, "无法开启手机共享", f"本机网页服务启动失败：{exc}")
            return
        self.share_address = address
        self.share_button.setText("停止手机共享")
        self.share_qr_button.setEnabled(True)
        self.share_status_label.setText("已开启 · 手机与电脑连接同一 Wi-Fi 后扫码")
        self._show_mobile_share_dialog()

    def _show_mobile_share_dialog(self) -> None:
        if not self.mobile_share.active:
            return
        if self.share_dialog is not None:
            self.share_dialog.show()
            self.share_dialog.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("手机查看回答")
        dialog.resize(360, 390)
        layout = QVBoxLayout(dialog)
        note = QLabel("手机和电脑连接同一 Wi-Fi。扫描二维码后，可以实时查看本次运行的回答。")
        note.setWordWrap(True)
        layout.addWidget(note)
        address_label = QLabel(f"共享地址：{self.share_address}")
        layout.addWidget(address_label)
        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(qr_label, 1)
        link_edit = QLineEdit()
        link_edit.setReadOnly(True)
        layout.addWidget(link_edit)
        copy_button = QPushButton("复制手机地址")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(link_edit.text()))
        layout.addWidget(copy_button)
        footer = QLabel("仅当前运行有效；停止共享或退出软件后，手机地址失效。")
        footer.setWordWrap(True)
        layout.addWidget(footer)

        url = self.mobile_share.pair_url(self.share_address)
        link_edit.setText(url)
        code = qrcode.QRCode(border=2)
        code.add_data(url)
        code.make(fit=True)
        matrix = code.get_matrix()
        scale = 4
        image = QImage(len(matrix) * scale, len(matrix) * scale, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.black)
        for y, row in enumerate(matrix):
            for x, filled in enumerate(row):
                if filled:
                    painter.drawRect(x * scale, y * scale, scale, scale)
        painter.end()
        qr_label.setPixmap(QPixmap.fromImage(image))
        self.share_dialog = dialog
        dialog.show()

    def _export_conversation(self) -> None:
        if not self.records:
            self.status_label.setText("本次运行尚无可导出的片段")
            return
        suggested = Path.home() / f"语音问答记录-{datetime.now():%Y%m%d-%H%M%S}.txt"
        filename, _ = QFileDialog.getSaveFileName(self, "导出对话记录", str(suggested), "文本文件 (*.txt)")
        if not filename:
            return
        try:
            Path(filename).write_text("\n".join(self._conversation_export_lines()), encoding="utf-8-sig")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.status_label.setText(f"已导出 {len(self.records)} 条片段")

    def _conversation_export_lines(self) -> list[str]:
        lines = ["语音问答记录", f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}", ""]
        for record_id in sorted(self.records):
            record = self.records[record_id]
            session = str(record["session_id"]) if record["session_id"] is not None else "手动"
            lines.append(f"#{record_id} [{record['created']:%Y-%m-%d %H:%M:%S}] 来源：{record['source']} · 会话：{session}")
            lines.append(f"身份：{record['identity']} · 模型：{record['model_label']}")
            if record.get("asr_backend"):
                lines.append(f"识别方案：{record['asr_backend']}")
            lines.append(f"状态：{record['status']}")
            if record.get("parent_id") is not None:
                lines.append(f"修正自：#{record['parent_id']}")
            if record.get("superseded_by") is not None:
                lines.append(f"已由片段 #{record['superseded_by']} 修正替代")
            if record["statement"]:
                lines.append(f"发言：{record['statement']}")
            if record.get("answer"):
                lines.append(f"回答：{record['answer']}")
            if record.get("detail"):
                lines.append(f"说明：{record['detail']}")
            lines.append("")
        return lines

    def _asr_worker(self) -> None:
        while True:
            record_id, audio, settings, responder, queued_at = self.asr_tasks.get()
            try:
                self.events.put(("record_timing", (record_id, "asr_wait", time.monotonic() - queued_at)))
                self.events.put(("record_state", (record_id, "正在转写", "")))
                started = time.monotonic()
                try:
                    statement = self.asr.transcribe(audio, settings)
                except Exception as exc:
                    if settings.asr_backend != "funasr":
                        raise
                    self.events.put(("log", f"第 {record_id} 段：中文增强识别不可用：{exc}；改用兼容模式"))
                    self.events.put(("asr_backend_used", (record_id, "whisper-base（回退）")))
                    statement = self.asr.transcribe(audio, replace(settings, asr_backend="whisper-base"))
                elapsed = time.monotonic() - started
                if not statement:
                    self.events.put(("record_state", (record_id, "未识别", "没有识别出清晰语音")))
                    self.events.put(("log", f"第 {record_id} 段：未识别出清晰语音"))
                    continue
                self.events.put(("transcript", (record_id, statement, elapsed)))
                self.events.put(("log", f"第 {record_id} 段听到：{statement}"))
                self.events.put(("record_state", (record_id, "等待回答", "")))
                self._submit_answer(record_id, statement, settings, responder)
            except Exception as exc:
                self.events.put(("record_state", (record_id, "失败", f"转写失败：{exc}")))
                self.events.put(("log", f"第 {record_id} 段转写失败：{exc}"))
            finally:
                self.asr_tasks.task_done()

    def _answer_worker(self) -> None:
        while True:
            record_id, statement, settings, responder, queued_at = self.answer_tasks.get()
            try:
                self.events.put(("record_timing", (record_id, "answer_wait", time.monotonic() - queued_at)))
                self.events.put(("record_state", (record_id, "正在回答", "")))
                started = time.monotonic()
                history = self.conversation_memory.history_before(record_id)
                if isinstance(responder, ClassroomResponder):
                    answer = responder.answer(statement, settings, history, record_id)
                else:
                    answer = responder.answer(statement, settings)
                self.conversation_memory.record_answer(record_id, answer)
                self.events.put(("answer", (record_id, statement, answer, time.monotonic() - started)))
            except Exception as exc:
                self.events.put(("record_state", (record_id, "失败", f"回答失败：{exc}")))
                self.events.put(("log", f"第 {record_id} 段回答失败：{exc}"))
            finally:
                self.answer_tasks.task_done()

    def _append(self, widget: CopyableLog, text: str) -> None:
        widget.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def _update_record_row(self, record_id: int) -> None:
        record = self.records[record_id]
        session = f"会话 {record['session_id']} · " if record['session_id'] is not None else ""
        correction = f"（修正 #{record['parent_id']}）" if record.get("parent_id") is not None else ""
        title = f"#{record_id}  {record['created']:%H:%M:%S}  {session}{record['source']}{correction}  ·  {record['status']}"
        identity = record["identity"][:18] + ("…" if len(record["identity"]) > 18 else "")
        model = record["model_label"][:32] + ("…" if len(record["model_label"]) > 32 else "")
        detail = f"{identity} / {model}"
        if record.get("asr_backend"):
            detail += f" · 识别 {record['asr_backend']}"
        if record["statement"]:
            preview = record["statement"].replace("\n", " ")[:38]
            detail += f" · {preview}"
        timing = []
        if "asr_wait" in record:
            timing.append(f"待识别 {record['asr_wait']:.1f}s")
        if "asr_elapsed" in record:
            timing.append(f"识别 {record['asr_elapsed']:.1f}s")
        if "answer_wait" in record:
            timing.append(f"待回答 {record['answer_wait']:.1f}s")
        if "answer_elapsed" in record:
            timing.append(f"回答 {record['answer_elapsed']:.1f}s")
        if timing:
            detail += " · " + " / ".join(timing)
        if record.get("detail"):
            detail += f" · {record['detail']}"
        record["item"].setText(f"{title}\n{detail}")
        record["item"].setSizeHint(QSize(0, 48))
        record["item"].setToolTip(
            f"身份：{record['identity']}\n模型：{record['model_label']}\n状态：{record['status']}\n"
            f"转写：{record['statement'] or '无'}\n{detail}"
        )

    def _update_overall_status(self) -> None:
        terminal = {"已完成", "已被修正替代", "失败", "未识别", "未入队"}
        pending_records = [record for record in self.records.values() if record["status"] not in terminal]
        pending = len(pending_records)
        if self.listener_running and self.stop_event is not None and not self.stop_event.is_set():
            current = sum(record["session_id"] == self.current_session_id for record in pending_records)
            other = pending - current
            other_text = f" · 其他 {other} 段" if other else ""
            self.status_label.setText(f"监听中：{self.session_device_name} · 本次 {current} 段{other_text}处理中")
        elif self.stop_event is not None and self.stop_event.is_set():
            prefix = f"收音失败：{self.capture_error[:70]}" if self.capture_error else "收音已停止"
            self.status_label.setText(f"{prefix}，仍有 {pending} 段处理中" if pending else f"{prefix}，全部处理完成")
        else:
            self.status_label.setText(
                f"仍有 {pending} 段处理中" if pending else ("准备就绪" if self.active_asr_backend else "语音识别模型尚未就绪")
            )

    def _update_summary(self) -> None:
        edited = self._form_parameters() != self.parameters
        self._update_config_state(edited)
        if self.listener_running:
            persona = self._matching_persona_name(self.session_settings) or "自定义"
            text = f"本次监听：{self.session_provider_name} · {self.session_device_name} · 人设：{persona}（{self.session_settings.identity}）"
            if edited:
                text += " · 界面有未保存修改"
        else:
            current = self._form_parameters()
            persona = self._matching_persona_name(current) or "自定义"
            text = f"当前界面：{self.provider_box.currentText()} · {self.device_box.currentText() or '未选设备'} · 人设：{persona}（{current.identity or '未设置'}）"
            if edited:
                text += " · 未保存，手动提问使用当前界面内容"
        self.summary_label.setText(text)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "level":
                    self.level_label.setText(f"音量：{value:.3f}")
                elif kind == "log":
                    self._append(self.transcript, str(value))
                elif kind == "record_new":
                    record_id, created, created_mono, source, statement, session_id, settings, responder, parent_id = value
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, record_id)
                    self.progress.insertItem(0, item)
                    self.records[record_id] = {
                        "created": created,
                        "created_mono": created_mono,
                        "source": source,
                        "session_id": session_id,
                        "settings": settings,
                        "responder": responder,
                        "identity": settings.identity if settings is not None else "未记录身份",
                        "model_label": self._model_label(settings),
                        "parent_id": parent_id,
                        "asr_backend": settings.asr_backend if source == "语音" and settings is not None else "",
                        "status": "等待转写" if source == "语音" else "等待回答",
                        "statement": statement,
                        "item": item,
                    }
                    if parent_id in self.records:
                        self.records[parent_id]["pending_revision_id"] = record_id
                        self._update_record_row(parent_id)
                    self._update_record_row(record_id)
                    self._update_overall_status()
                elif kind == "record_state":
                    record_id, state, detail = value
                    if record_id in self.records:
                        record = self.records[record_id]
                        if record.get("superseded_by") is not None:
                            continue
                        record["status"] = state
                        record["detail"] = detail
                        self._update_record_row(record_id)
                        self._update_overall_status()
                elif kind == "retry_evicted":
                    record_id = value
                    if record_id in self.records and self.records[record_id]["status"] == "未入队":
                        self.records[record_id]["detail"] = "重试缓存已满，音频未保留"
                        self._update_record_row(record_id)
                elif kind == "record_timing":
                    record_id, field, elapsed = value
                    if record_id in self.records:
                        self.records[record_id][field] = elapsed
                        self._update_record_row(record_id)
                elif kind == "asr_backend_used":
                    record_id, backend = value
                    if record_id in self.records:
                        self.records[record_id]["asr_backend"] = backend
                        self._update_record_row(record_id)
                elif kind == "transcript":
                    record_id, statement, elapsed = value
                    self.last_transcript = statement
                    self.last_transcript_record_id = record_id
                    if record_id in self.records:
                        self.records[record_id]["statement"] = statement
                        self.records[record_id]["asr_elapsed"] = elapsed
                        self._update_record_row(record_id)
                elif kind == "answer":
                    record_id, statement, answer, elapsed = value
                    answered_at = datetime.now()
                    self.conversation_entries.append((answered_at, statement, answer))
                    number = len(self.conversation_entries)
                    color = self.answer_color_odd if number % 2 else self.answer_color_even
                    if record_id in self.records:
                        record = self.records[record_id]
                        parent_id = record.get("parent_id")
                        context_parts = []
                        if parent_id is not None:
                            context_parts.append(f"修正 #{parent_id}")
                        if record.get("superseded_by") is not None:
                            context_parts.append("已被修正替代")
                        self.answer.append_answer(number, statement, answer, color, record_id, " · ".join(context_parts))
                        record["answer"] = answer
                        record["answer_elapsed"] = elapsed
                        if record.get("superseded_by") is None:
                            record["status"] = "已完成"
                            record.pop("detail", None)
                        self._update_record_row(record_id)
                        descendant_id = record_id
                        replaced_record_ids = []
                        while parent_id in self.records:
                            parent = self.records[parent_id]
                            replaced_record_ids.append(parent_id)
                            parent.pop("pending_revision_id", None)
                            parent["superseded_by"] = descendant_id
                            parent["status"] = "已被修正替代"
                            parent["detail"] = f"以片段 #{record_id} 的回答为准"
                            self._update_record_row(parent_id)
                            descendant_id = parent_id
                            parent_id = parent.get("parent_id")
                        mobile_entry = {
                            "id": number,
                            "record_id": record_id,
                            "time": answered_at.strftime("%H:%M:%S"),
                            "question": statement,
                            "answer": answer,
                            "superseded": record.get("superseded_by") is not None,
                            "replaced_record_ids": replaced_record_ids,
                        }
                        replaced_set = set(replaced_record_ids)
                        for old in self.mobile_answer_entries:
                            if old["record_id"] in replaced_set:
                                old["superseded"] = True
                        self.mobile_answer_entries.append(mobile_entry)
                        self.mobile_share.publish(mobile_entry)
                        self._update_overall_status()
                elif kind == "status":
                    self.status_label.setText(str(value))
                elif kind == "asr_ready":
                    generation, backend, notice = value
                    if generation == self.preload_generation:
                        self.active_asr_backend = backend
                        self.start_button.setEnabled(not self.listener_running)
                        self.asr_status_label.setText(f"语音识别：{notice or backend + ' 已就绪'}")
                        self._append(self.transcript, notice or "语音识别模型已加载")
                        self._update_overall_status()
                elif kind == "asr_failed":
                    generation, error = value
                    if generation == self.preload_generation:
                        self.active_asr_backend = None
                        self.start_button.setEnabled(False)
                        self.asr_status_label.setText("语音识别：模型加载失败")
                        self._append(self.transcript, f"语音识别模型加载失败：{error}")
                        self._update_overall_status()
                elif kind == "capture_failed":
                    self.capture_error = str(value)
                    self._append(self.transcript, f"收音失败：{value}")
                    self._update_overall_status()
                elif kind == "listener_stopped":
                    self.listener_running = False
                    self.stop_button.setEnabled(False)
                    if self.parameters.asr_backend != self.preload_target:
                        self._begin_asr_preload(self.parameters.asr_backend)
                    else:
                        self.start_button.setEnabled(self.active_asr_backend is not None)
                    self._update_overall_status()
        except queue.Empty:
            pass
        self._update_summary()

    def closeEvent(self, event) -> None:
        self.mobile_share.stop()
        if self.share_dialog is not None:
            self.share_dialog.close()
            self.share_dialog = None
        if self.stop_event:
            self.stop_event.set()
        with self.retry_audio_lock:
            self.retry_audio.clear()
            self.retry_audio_bytes = 0
        for record in self.records.values():
            record["responder"] = None
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ClassroomApp()
    window.show()
    sys.exit(app.exec())
