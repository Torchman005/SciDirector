"""LLM / VLM 客户端封装。

所有对模型的调用都必须经过本模块，目的是把以下横切关注点收敛到一处：

1. **重试与超时**：模型 429/5xx 是常态，必须带指数退避重试；
2. **结构化输出**：审查与规划都要求 JSON，解析失败要有明确的降级路径；
3. **成本计量**：token 数直接等于钱，必须逐次累计并可上报；
4. **可替换**：通过 ``SCID_LLM_PROVIDER=mock`` 可完全离线运行，
   这对本地开发、CI 与单测至关重要（否则每个测试都要烧 token）。

关于同步 / 异步的选择：
统一使用**同步** SDK。理由是 gRPC 的同步 servicer 跑在线程池中，
而渲染等调用本身也是阻塞的；引入 asyncio 只会带来「在错误的地方 await」
与事件循环桥接的复杂度。FastAPI 侧统一用 ``asyncio.to_thread`` 调用。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ValidationError

from .config import Settings
from .logging import get_logger

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """模型调用失败（重试耗尽后抛出）。"""


class LLMParseError(LLMError):
    """模型返回的内容无法解析为要求的结构。"""


@dataclass
class Usage:
    """一次或多次调用的累计用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.calls += 1

    def merge(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls


@dataclass
class Message:
    """一条对话消息。``images`` 为本地图片路径列表（仅 VLM 使用）。"""

    role: str
    text: str
    images: list[str] = field(default_factory=list)


#: 任务标识。**显式传参**而不是让 mock 从提示词里嗅探关键词。
#:
#: 为什么必须显式：早期版本靠 ``"审查" in user_text`` 这类关键词判断意图，
#: 而导演提示词的风格约束里恰好含「审查」二字，导致 mock 返回了审查结果、
#: 被解析成「空分镜表」——一个**静默给出错误答案**的失败形态，极难排查。
#: 显式任务标识让 mock 的行为完全确定，也顺带让日志能按任务类型切分。
class Task:
    PLAN = "plan"          # 导演：脚本 -> 分镜表
    CODE = "code"          # 编码：分镜 -> 渲染源码
    CRITIQUE = "critique"  # 审查：抽帧 -> 评审意见
    FREE = "free"          # 自由文本


# ---------------------------------------------------------------------------
# JSON 提取
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """从模型输出中稳健地提取 JSON。

    模型几乎一定会加解释性前后缀或 markdown 代码块，因此按以下顺序尝试：
    1. 直接 ``json.loads``（最理想的情况）；
    2. 提取 ``` 代码块内容；
    3. 截取第一个 ``{`` 到最后一个 ``}``（或 ``[`` 到 ``]``）。

    三次都失败才抛 ``LLMParseError``，并保留原文供排查与提示词迭代。
    """
    if not text or not text.strip():
        raise LLMParseError("模型返回为空")

    candidates: list[str] = [text.strip()]

    for block in _JSON_BLOCK.findall(text):
        candidates.append(block.strip())

    stripped = text.strip()
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = stripped.find(open_ch)
        end = stripped.rfind(close_ch)
        if start != -1 and end > start:
            candidates.append(stripped[start : end + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:  # 继续尝试下一个候选
            last_error = exc
            continue

    raise LLMParseError(f"无法从模型输出中解析 JSON：{last_error}；原文前 500 字：{text[:500]}")


def encode_image(path: str) -> tuple[str, str]:
    """把本地图片编码为 OpenAI 兼容的 data URL。

    返回 ``(data_url, mime)``。文件不存在时抛出 ``LLMError``，
    由调用方决定是跳过该帧还是失败 —— 抽帧缺失通常不值得让整个审查失败。
    """
    p = Path(path)
    if not p.is_file():
        raise LLMError(f"图片不存在：{path}")
    mime, _ = mimetypes.guess_type(p.name)
    if mime is None:
        mime = "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}", mime


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class LLMClient:
    """文本与视觉模型的统一入口。线程安全（每次调用独立构造请求）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.usage = Usage()
        self._client: Any = None
        # mock 模式：无密钥或显式指定时启用，保证离线可跑通全流程。
        self._mock = settings.llm_provider == "mock" or not settings.openai_api_key

        if self._mock:
            logger.warning(
                "LLM 处于 mock 模式（未配置 OPENAI_API_KEY 或 provider=mock）："
                "将返回确定性的占位结果，仅用于本地联调与测试"
            )
        else:
            self._build_client()

    def _build_client(self) -> None:
        """延迟构造 OpenAI 客户端。

        延迟的原因：没有密钥时不应因为缺少依赖/构造失败而让进程起不来 ——
        mock 模式必须能在「什么都没配」的环境下运行。
        """
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署问题
            raise LLMError("未安装 openai 包，无法使用真实模型；请 pip install openai") from exc

        kwargs: dict[str, Any] = {
            "api_key": self.settings.openai_api_key,
            "timeout": float(self.settings.llm_timeout_sec),
            "max_retries": 0,  # 重试由本模块统一控制，避免双重退避导致等待过久
        }
        if self.settings.openai_base_url:
            kwargs["base_url"] = self.settings.openai_base_url

        self._client = OpenAI(**kwargs)
        logger.info(
            "LLM 客户端已初始化",
            extra={
                "provider": self.settings.llm_provider,
                "base_url": self.settings.openai_base_url or "(官方)",
            },
        )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    @property
    def is_mock(self) -> bool:
        return self._mock

    def chat_text(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        task: str = Task.FREE,
    ) -> str:
        """纯文本对话，返回字符串。"""
        return self._call(
            messages=[Message(role="system", text=system), Message(role="user", text=user)],
            model=model or self.settings.llm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
            task=task,
        )

    def chat_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: Sequence[str] = (),
        task: str = Task.FREE,
    ) -> BaseModel:
        """结构化对话：要求模型返回 JSON 并解析为给定 Pydantic 模型。

        解析或校验失败时**重试一次**（附带错误信息），再失败则抛出
        ``LLMParseError``，由调用方决定降级策略（通常是转人工）。
        """
        model_name = model or self.settings.llm_model
        base_user = user
        attempt = 0
        max_parse_attempts = 2
        last_error: Exception | None = None

        while attempt < max_parse_attempts:
            attempt += 1
            raw = self._call(
                messages=[
                    Message(role="system", text=system),
                    Message(role="user", text=base_user, images=list(images)),
                ],
                model=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
                task=task,
            )
            try:
                payload = extract_json(raw)
                return _validate(schema, payload)
            except (LLMParseError, ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "模型输出解析失败，准备重试",
                    extra={"attempt": attempt, "schema": schema.__name__, "error": str(exc)[:400]},
                )
                # 把失败原因回灌给模型，比单纯重试成功率更高（模型能看到自己错在哪）。
                base_user = (
                    f"{user}\n\n"
                    f"【上一次输出无法解析，错误信息如下，请严格按要求输出 JSON】\n{exc}"
                )

        raise LLMParseError(f"模型输出连续 {max_parse_attempts} 次无法解析：{last_error}")

    def vision_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        images: Sequence[str],
        *,
        model: str | None = None,
        task: str = Task.CRITIQUE,
    ) -> BaseModel:
        """视觉结构化对话（VLM 审查）。图片缺失会被跳过而不是让整体失败。"""
        usable: list[str] = []
        for img in images:
            if Path(img).is_file():
                usable.append(img)
            else:
                logger.warning("抽帧文件不存在，已跳过", extra={"path": img})

        if not usable:
            raise LLMError("没有任何可用的抽帧图片，无法进行视觉审查")

        return self.chat_json(
            system,
            user,
            schema,
            model=model or self.settings.vlm_model,
            temperature=0.0,  # 审查要求可复现，温度必须为 0
            images=usable,
            task=task,
        )

    # ------------------------------------------------------------------
    # 内部：底层调用与重试
    # ------------------------------------------------------------------

    def _call(
        self,
        *,
        messages: list[Message],
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
        task: str,
    ) -> str:
        if self._mock:
            return _mock_response(messages, json_mode=json_mode, task=task)

        payload = [self._to_openai_message(m) for m in messages]

        body: dict[str, Any] = {
            "model": model,
            "messages": payload,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        if json_mode:
            # 让服务端保证输出是合法 JSON —— 这是最省事也最可靠的一层保障。
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(1, self.settings.llm_max_retries + 2):
            start = time.monotonic()
            try:
                resp = self._client.chat.completions.create(**body)
                elapsed = time.monotonic() - start
                content = (resp.choices[0].message.content or "").strip()

                usage = getattr(resp, "usage", None)
                if usage is not None:
                    self.usage.add(
                        int(getattr(usage, "prompt_tokens", 0) or 0),
                        int(getattr(usage, "completion_tokens", 0) or 0),
                    )

                logger.debug(
                    "LLM 调用完成",
                    extra={
                        "model": model,
                        "elapsed_sec": round(elapsed, 3),
                        "total_tokens": self.usage.total_tokens,
                    },
                )
                if not content:
                    raise LLMError("模型返回空内容")
                return content

            except Exception as exc:  # noqa: BLE001 - 需要兜住 SDK 的各种异常类型
                last_error = exc
                if attempt > self.settings.llm_max_retries:
                    break
                # 指数退避 + 抖动：避免大量并发任务在同一时刻重试造成惊群。
                delay = min(2 ** (attempt - 1), 16) * (1 + random.random() * 0.3)
                logger.warning(
                    "LLM 调用失败，准备重试",
                    extra={"attempt": attempt, "delay_sec": round(delay, 2), "error": str(exc)[:300]},
                )
                time.sleep(delay)

        raise LLMError(f"LLM 调用失败（已重试 {self.settings.llm_max_retries} 次）：{last_error}")

    def _to_openai_message(self, msg: Message) -> dict[str, Any]:
        """把内部 Message 转为 OpenAI 的多模态消息格式。"""
        if not msg.images:
            return {"role": msg.role, "content": msg.text}

        parts: list[dict[str, Any]] = [{"type": "text", "text": msg.text}]
        for img in msg.images:
            try:
                data_url, _ = encode_image(img)
            except LLMError as exc:
                logger.warning("图片编码失败，已跳过", extra={"error": str(exc)})
                continue
            parts.append({"type": "image_url", "image_url": {"url": data_url, "detail": "high"}})
        return {"role": msg.role, "content": parts}


def _validate(schema: type[BaseModel], payload: Any) -> BaseModel:
    """把解析结果套进 Pydantic 模型，并在「裸标量」场景下做一次友好适配。

    模型有时会返回 ``{"shots": [...]}`` 而我们要的是 ``list[Shot]``（或反之），
    这里做一层常见的包裹/解包，减少无谓的重试。
    """
    if isinstance(payload, dict):
        # 单字段包裹：{"shots": [...]} / {"result": {...}} 等
        if len(payload) == 1:
            only_value = next(iter(payload.values()))
            try:
                return schema.model_validate(only_value)
            except (ValidationError, ValueError):
                pass
        return schema.model_validate(payload)
    return schema.model_validate(payload)


# ---------------------------------------------------------------------------
# Mock 实现
# ---------------------------------------------------------------------------


def _mock_response(messages: list[Message], *, json_mode: bool, task: str) -> str:
    """确定性占位响应。

    设计目标：让「无密钥」环境也能把整条流水线跑通，
    因此返回的结构必须**严格符合真实 schema**，
    否则 mock 模式会掩盖真实的解析问题。

    意图判定使用显式 ``task``，**不做关键词嗅探** ——
    早期版本靠 ``"审查" in text`` 判断，而导演提示词里恰好含这个词，
    导致返回了错误结构并被静默解析成空结果。这类「看起来成功」的
    失败形态比抛异常危险得多。
    """
    if not json_mode:
        return "（mock 模式）这是一段占位说明文本。"

    if task == Task.CRITIQUE:
        # 默认判为通过。需要演练「打回重做」流程时，
        # 通过环境变量 SCID_MOCK_CRITIC_PASSED=false 切换即可，
        # 无需改代码 —— 这让 HITL 闭环的联调变得非常方便。
        import os

        passed = os.environ.get("SCID_MOCK_CRITIC_PASSED", "true").lower() != "false"
        if passed:
            return json.dumps(
                {
                    "passed": True,
                    "score": 0.82,
                    "issues": [],
                    "suggestions": [],
                    "logic_score": 0.85,
                    "readability_score": 0.80,
                    "pacing_score": 0.80,
                    "aesthetics_score": 0.83,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "passed": False,
                "score": 0.41,
                "issues": ["正文字号过小，手机上无法辨认", "动画推进过快，观众来不及理解"],
                "suggestions": [
                    "把正文字号从 24 提到 48",
                    "把关键动画的 run_time 从 0.5 秒提到 1.5 秒",
                ],
                "logic_score": 0.55,
                "readability_score": 0.30,
                "pacing_score": 0.35,
                "aesthetics_score": 0.60,
            },
            ensure_ascii=False,
        )

    if task == Task.PLAN:
        shots = [
            {
                "index": 0,
                "narration": "（mock）开场：抛出问题，建立悬念。",
                "visual_brief": "深色背景上浮现标题，配以缓慢推进的粒子。",
                "tag": "AMBIENCE",
                "duration_sec": 4.0,
                "keywords": ["开场", "标题"],
            },
            {
                "index": 1,
                "narration": "（mock）核心公式推导：从定义出发逐步变形。",
                "visual_brief": "居中展示公式，逐步高亮等号两侧的变化。",
                "tag": "MATH",
                "duration_sec": 12.0,
                "keywords": ["公式", "推导"],
            },
            {
                "index": 2,
                "narration": "（mock）数据对比：两种条件下的结果差异。",
                "visual_brief": "柱状图从左到右生长，数值标签同步淡入。",
                "tag": "DATA",
                "duration_sec": 10.0,
                "keywords": ["柱状图", "对比"],
            },
            {
                "index": 3,
                "narration": "（mock）收尾：总结要点并给出延伸阅读。",
                "visual_brief": "要点逐条浮现，末尾淡出至标题。",
                "tag": "AMBIENCE",
                "duration_sec": 5.0,
                "keywords": ["总结"],
            },
        ]
        return json.dumps(
            {"outline": "（mock）问题 -> 原理 -> 数据 -> 总结", "shots": shots},
            ensure_ascii=False,
        )

    if task == Task.CODE:
        return json.dumps(
            {
                "code": (
                    "from manim import *\n\n"
                    "class MockScene(Scene):\n"
                    "    def construct(self):\n"
                    "        title = Text('mock scene', font_size=48)\n"
                    "        self.play(Write(title), run_time=2)\n"
                    "        self.wait(1)\n"
                ),
                "language": "python",
                "explanation": "（mock）最小可运行的 Manim 场景。",
            },
            ensure_ascii=False,
        )

    return json.dumps({"ok": True}, ensure_ascii=False)
