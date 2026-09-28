"""Local speech recognition and selectable text inference."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from settings import AppParameters

PROJECT_DIR = Path(__file__).resolve().parents[1]
MODEL_DIR = PROJECT_DIR / "models"
DEFAULT_SERVER = "http://127.0.0.1:8080"

COMMON_PROMPT = (
    "你是语音问答助手。根据用户当前发言给出有帮助的回应；介绍、比较、解释、解决问题等请求都应回答，普通陈述也可自然回应。\n"
    "输出适合朗读的纯文本，不使用 Markdown、项目符号或表情符号。优先简洁、自然；内容复杂时可以多说，不强制句数。\n"
    "尽量使用用户提问的语言。语音转写可能有错；能从上下文确定意思时自然修正，无法确定时简短澄清。\n"
    "不要编造个人经历、身份事实或未知信息。不要主动强调模型身份；如果被直接问到，诚实回答。\n"
    "当前片段可能只是慢速发言的一部分，也要根据已经发生的对话自然回应；信息不足时可以简短接话或澄清，不要补造尚未说出的内容。\n"
    "本次运行的实际对话只以随后提供的用户发言记录为准。人设背景、预置答案和你此前的回答都不能证明用户曾说过什么；回答‘之前问了什么’等记忆问题时，必须依据历史用户发言，找不到就明确说没有记录。\n"
    "下面的人设资料只约束当前场景，不改变上述通用规则。"
)


def to_simplified(text: str) -> str:
    from opencc import OpenCC

    return OpenCC("t2s.json").convert(text)


def build_system_prompt(settings: AppParameters) -> str:
    """Keep a stable common prefix while allowing each persona to supply scene facts."""
    scene = (
        ("身份", settings.identity),
        ("场景与交谈对象", settings.scenario),
        ("已知背景事实", settings.background),
        ("知识与回答范围", settings.knowledge_scope),
        ("说话方式", settings.speaking_style),
        ("常见问题的回答要点", settings.prepared_answers),
        ("其他场景指令", settings.instructions),
    )
    scene_text = "\n".join(f"{label}：{value}" for label, value in scene if value.strip())
    return f"{COMMON_PROMPT}\n\n【当前场景】\n{scene_text}"


class ClassroomResponder:
    def chat(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        raise NotImplementedError

    def answer(
        self,
        statement: str,
        settings: AppParameters,
        history: tuple[tuple[int, str, str | None], ...] = (),
        record_id: int | None = None,
    ) -> str:
        messages: list[dict[str, str]] = []
        for previous_id, previous_statement, previous_answer in history:
            messages.append({"role": "user", "content": f"【本次运行片段 #{previous_id}】{previous_statement}"})
            if previous_answer:
                messages.append({"role": "assistant", "content": previous_answer})
        current_label = f"【当前片段 #{record_id}】" if record_id is not None else ""
        messages.append({"role": "user", "content": current_label + statement})
        content = self.chat(
            build_system_prompt(settings),
            messages,
            1536,
        )
        if not content:
            raise RuntimeError("模型没有生成可显示的答案")
        return to_simplified(content)


class LocalLlama(ClassroomResponder):
    def __init__(self, base_url: str = DEFAULT_SERVER):
        self.base_url = base_url.rstrip("/")
        self.model_id: str | None = None

    def _json(self, path: str, payload: dict | None = None) -> dict:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("仅支持本机 HTTP 模型服务地址，例如 http://127.0.0.1:8080")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        with urlopen(request, timeout=90 if body else 5) as response:
            return json.load(response)

    def available_model(self) -> str:
        if self.model_id is None:
            models = self._json("/v1/models").get("data", [])
            if not models:
                raise RuntimeError("本地模型服务没有加载模型")
            self.model_id = models[0]["id"]
        return self.model_id

    def chat(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        def request_answer(instructions: str, token_limit: int) -> tuple[str, str]:
            result = self._json("/v1/chat/completions", {
                "model": self.available_model(),
                "messages": [{"role": "system", "content": instructions}, *messages],
                "temperature": 0.1,
                "max_tokens": token_limit,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": False},
            })
            choices = result.get("choices", [])
            if not choices:
                return "", "无结果"
            # Never show reasoning_content in the classroom UI.
            content = choices[0].get("message", {}).get("content") or ""
            return content.strip(), str(choices[0].get("finish_reason") or "未知")

        content, reason = request_answer(system, max_tokens)
        if content:
            return content
        content, retry_reason = request_answer(
            system + "必须输出一句可显示的简体中文回答；信息不足时请提出澄清问题。",
            max(max_tokens, 1024),
        )
        if content:
            return content
        raise RuntimeError(f"本地模型两次未返回正式文本（结束原因：{reason}、{retry_reason}）")

class OpenAIGPT(ClassroomResponder):
    """Send recognized text only to the OpenAI Responses API."""

    def __init__(self, api_key: str, model: str = "gpt-6-sol"):
        self.api_key = api_key.strip()
        self.model = model.strip()
        if not self.api_key or not self.model:
            raise ValueError("GPT 模式需要 API Key 和模型名称")

    def chat(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        body = {
            "model": self.model,
            "instructions": system,
            "input": messages,
            "max_output_tokens": max_tokens,
            "store": False,
        }
        if self.model == "gpt-6-sol":
            body["reasoning"] = {"effort": "low"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            "https://api.openai.com/v1/responses",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"OpenAI 请求失败（HTTP {exc.code}）；请检查 API Key、模型及额度") from None
        except URLError:
            raise RuntimeError("无法连接 OpenAI；请检查网络连接") from None
        if result.get("status") != "completed":
            raise RuntimeError("OpenAI 未完成回答，请重试或更换模型")
        content = "".join(
            item.get("text", "")
            for output in result.get("output", [])
            if output.get("type") == "message"
            for item in output.get("content", [])
            if item.get("type") == "output_text"
        ).strip()
        if not content:
            raise RuntimeError("OpenAI 没有返回可显示的文字")
        return content


class CompatibleChat(ClassroomResponder):
    """Use an OpenAI-compatible Chat Completions endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str):
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()
        self.api_key = api_key.strip()
        parsed = urlparse(self.base_url)
        is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
            raise ValueError("兼容 API 请使用 HTTPS 地址；本机服务可使用 HTTP")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("兼容 API 服务地址格式无效")
        if self.base_url.endswith("/chat/completions"):
            raise ValueError("只需填写 API 基础地址，不要附加 /chat/completions")
        if not self.model:
            raise ValueError("请填写兼容 API 的模型名称")
        if not self.api_key and not is_loopback:
            raise ValueError("远程兼容 API 需要 API Key")

    def chat(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": max_tokens,
            "stream": False,
        }
        if urlparse(self.base_url).hostname == "api.deepseek.com":
            body["thinking"] = {"type": "disabled"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"兼容 API 请求失败（HTTP {exc.code}）；请检查地址、API Key、模型名称和额度") from None
        except URLError:
            raise RuntimeError("无法连接兼容 API；请检查服务地址和网络连接") from None
        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError("兼容 API 没有返回回答选项")
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("兼容 API 没有返回可显示的文字")
        return content.strip()


class LocalASR:
    def __init__(self):
        self.whisper_model = None
        self.funasr_model = None
        self.load_lock = threading.Lock()

    def prepare(self, backend: str) -> None:
        with self.load_lock:
            if backend == "funasr" and self.funasr_model is None:
                os.environ.setdefault("MODELSCOPE_CACHE", str(MODEL_DIR / "funasr"))
                os.environ.setdefault("MODELSCOPE_HOME", str(MODEL_DIR / "funasr-home"))
                from funasr import AutoModel

                MODEL_DIR.mkdir(exist_ok=True)
                self.funasr_model = AutoModel(
                    model="paraformer-zh",
                    vad_model="fsmn-vad",
                    punc_model="ct-punc",
                    device="cpu",
                    disable_update=True,
                    trust_remote_code=False,
                )
            elif backend == "whisper-base" and self.whisper_model is None:
                from faster_whisper import WhisperModel

                MODEL_DIR.mkdir(exist_ok=True)
                model_name = os.environ.get("CLASSROOM_ASR_MODEL", "base")
                self.whisper_model = WhisperModel(
                    model_name,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(MODEL_DIR),
                )

    def transcribe(self, audio, settings: AppParameters) -> str:
        self.prepare(settings.asr_backend)
        if settings.asr_backend == "funasr":
            hotword = " ".join(settings.hotwords.replace("，", ",").split(","))
            options = {"input": audio, "batch_size_s": 30}
            if hotword.strip():
                options["hotword"] = hotword
            result = self.funasr_model.generate(**options)
            text = "".join(str(item.get("text", "")) for item in result)
        else:
            segments, _ = self.whisper_model.transcribe(
                audio,
                language="zh",
                beam_size=3,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = "".join(segment.text for segment in segments)
        return to_simplified(text.strip())
