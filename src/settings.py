"""Application preferences stored only on this computer."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

PARAMETERS_PATH = Path(__file__).resolve().parents[1] / "config" / "app_parameters.json"
PERSONAS_PATH = Path(__file__).resolve().parents[1] / "config" / "personas.json"
ASR_BACKENDS = {"funasr", "whisper-base"}


@dataclass(frozen=True)
class AppParameters:
    identity: str = "通用问答助手"
    scenario: str = ""
    background: str = ""
    knowledge_scope: str = ""
    speaking_style: str = ""
    prepared_answers: str = ""
    instructions: str = ""
    hotwords: str = ""
    asr_backend: str = "funasr"
    output_id: str = ""
    provider: str = "local"
    server_url: str = "http://127.0.0.1:8080"
    gpt_model: str = "gpt-6-sol"
    compatible_url: str = ""
    compatible_model: str = ""
    threshold: float = 0.008
    silence_seconds: float = 0.8
    transcript_font_size: int = 11
    answer_font_size: int = 12
    answer_color_odd: str = "#1f4e79"
    answer_color_even: str = "#9c3b10"

    @classmethod
    def load(cls) -> "AppParameters":
        if not PARAMETERS_PATH.exists():
            return cls()
        data = json.loads(PARAMETERS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("自定义参数文件格式不正确")
        params = cls(
            identity=str(data.get("identity", cls.identity))[:200].strip() or cls.identity,
            scenario=str(data.get("scenario", ""))[:500].strip(),
            background=str(data.get("background", ""))[:4000].strip(),
            knowledge_scope=str(data.get("knowledge_scope", ""))[:2000].strip(),
            speaking_style=str(data.get("speaking_style", ""))[:1000].strip(),
            prepared_answers=str(data.get("prepared_answers", ""))[:6000].strip(),
            instructions=str(data.get("instructions", ""))[:4000].strip(),
            hotwords=str(data.get("hotwords", ""))[:1000].strip(),
            asr_backend=str(data.get("asr_backend", cls.asr_backend)),
            output_id=str(data.get("output_id", ""))[:500],
            provider=str(data.get("provider", "local")),
            server_url=str(data.get("server_url", cls.server_url))[:500],
            gpt_model=str(data.get("gpt_model", cls.gpt_model))[:100],
            compatible_url=str(data.get("compatible_url", ""))[:500].strip(),
            compatible_model=str(data.get("compatible_model", ""))[:100].strip(),
            threshold=float(data.get("threshold", cls.threshold)),
            silence_seconds=float(data.get("silence_seconds", cls.silence_seconds)),
            transcript_font_size=int(data.get("transcript_font_size", cls.transcript_font_size)),
            answer_font_size=int(data.get("answer_font_size", cls.answer_font_size)),
            answer_color_odd=str(data.get("answer_color_odd", cls.answer_color_odd)),
            answer_color_even=str(data.get("answer_color_even", cls.answer_color_even)),
        )
        params.validate()
        return params

    def validate(self) -> None:
        if not self.identity.strip():
            raise ValueError("身份设定不能为空")
        for label, value, limit in (
            ("场景与对象", self.scenario, 500),
            ("背景事实", self.background, 4000),
            ("知识范围", self.knowledge_scope, 2000),
            ("说话方式", self.speaking_style, 1000),
            ("常见问题要点", self.prepared_answers, 6000),
            ("补充指令", self.instructions, 4000),
        ):
            if len(value) > limit:
                raise ValueError(f"{label}不能超过 {limit} 字")
        if self.asr_backend not in ASR_BACKENDS:
            raise ValueError("语音识别方案无效")
        if self.provider not in {"local", "openai", "compatible"}:
            raise ValueError("回答模型类型无效")
        if self.provider == "local":
            parsed = urlparse(self.server_url.strip())
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("本地模型地址须为本机 HTTP，例如 http://127.0.0.1:8080")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("本地模型地址不能包含账号、查询参数或片段")
        elif self.provider == "openai":
            if not self.gpt_model.strip():
                raise ValueError("请填写 GPT 模型名称")
        else:
            parsed = urlparse(self.compatible_url.strip())
            is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if not self.compatible_model.strip():
                raise ValueError("请填写兼容 API 的模型名称")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
                raise ValueError("兼容 API 请使用 HTTPS 地址；本机服务可使用 HTTP")
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("兼容 API 服务地址格式无效")
            if self.compatible_url.rstrip("/").endswith("/chat/completions"):
                raise ValueError("只需填写 API 基础地址，不要附加 /chat/completions")
        if not 0 < self.threshold < 1:
            raise ValueError("声音阈值必须在 0 到 1 之间")
        if not 0.2 <= self.silence_seconds <= 10:
            raise ValueError("停顿时间必须在 0.2 到 10 秒之间")
        if not 8 <= self.transcript_font_size <= 32 or not 8 <= self.answer_font_size <= 32:
            raise ValueError("字体大小必须在 8 到 32 之间")
        if not all(re.fullmatch(r"#[0-9a-fA-F]{6}", color) for color in (self.answer_color_odd, self.answer_color_even)):
            raise ValueError("回答颜色必须是有效的十六进制颜色")

    def save(self) -> None:
        self.validate()
        PARAMETERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = PARAMETERS_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, PARAMETERS_PATH)


@dataclass(frozen=True)
class Persona:
    name: str
    identity: str
    instructions: str = ""
    scenario: str = ""
    background: str = ""
    knowledge_scope: str = ""
    speaking_style: str = ""
    prepared_answers: str = ""

    def validate(self) -> None:
        if not 1 <= len(self.name.strip()) <= 80:
            raise ValueError("人设名称须在 1 到 80 字之间")
        if not 1 <= len(self.identity.strip()) <= 200:
            raise ValueError("身份须在 1 到 200 字之间")
        for label, value, limit in (
            ("场景与对象", self.scenario, 500),
            ("背景事实", self.background, 4000),
            ("知识范围", self.knowledge_scope, 2000),
            ("说话方式", self.speaking_style, 1000),
            ("常见问题要点", self.prepared_answers, 6000),
            ("补充指令", self.instructions, 4000),
        ):
            if len(value) > limit:
                raise ValueError(f"{label}不能超过 {limit} 字")


def load_personas() -> dict[str, Persona]:
    if not PERSONAS_PATH.exists():
        return {}
    data = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("personas"), list):
        raise ValueError("人设文件格式不正确")
    result = {}
    for item in data["personas"]:
        if not isinstance(item, dict):
            raise ValueError("人设文件包含无效条目")
        persona = Persona(
            name=str(item.get("name", "")).strip(),
            identity=str(item.get("identity", "")).strip(),
            instructions=str(item.get("instructions", "")).strip(),
            scenario=str(item.get("scenario", "")).strip(),
            background=str(item.get("background", "")).strip(),
            knowledge_scope=str(item.get("knowledge_scope", "")).strip(),
            speaking_style=str(item.get("speaking_style", "")).strip(),
            prepared_answers=str(item.get("prepared_answers", "")).strip(),
        )
        persona.validate()
        if persona.name in result:
            raise ValueError("人设文件包含重复名称")
        result[persona.name] = persona
    return result


def save_personas(personas: dict[str, Persona]) -> None:
    for name, persona in personas.items():
        persona.validate()
        if name != persona.name:
            raise ValueError("人设名称不一致")
    PERSONAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = PERSONAS_PATH.with_suffix(".tmp")
    payload = {"personas": [asdict(persona) for persona in personas.values()]}
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, PERSONAS_PATH)
