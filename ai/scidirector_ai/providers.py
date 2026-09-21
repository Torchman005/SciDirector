"""模型服务商的注册表与「目标」解析（纯逻辑，无 IO）。

## 为什么单独成模块

「用哪个服务商」这件事横跨三处：配置读取（`config.py`）、客户端构造（`llm.py`）、
健康报告（`service.py`）。如果各自散着写 `if provider == "..."`，
新增一家服务商就要改三个地方 —— 而漏改的那一处**不会报错**：
它只会让健康报告与实际用的服务商不一致，或者让某个 provider 悄悄走默认地址。

因此把「服务商有哪些、各自的默认值是什么、怎么从配置解析出一次调用的目标」
集中在这里，做成**纯函数**：可以完整单测，也可以被三处共用。

## 为什么 DeepSeek 的视觉模型是空的

`deepseek` 目前没有公开的视觉模型。这一点必须**显式**表达（`vlm_model=""`
且 `supports_vision=False`），而不是留一个看起来能用的默认值：
留默认值的后果是每次审查都调用失败、由 Critic 降级转人工 ——
于是**每一个镜头都进人工队列**，而原因看起来像"VLM 服务坏了"。
真正的意图应当一眼可见：想用 DeepSeek 做文本 + 别家做视觉，就显式配
`SCID_VLM_PROVIDER`。

## 模型名会过时，因此都能被环境变量覆盖

各家模型名迭代很快（本文档写下的默认值只代表"当时可用"）。
因此这里只给**出厂默认**，并且 `SCID_LLM_MODEL` / `SCID_VLM_MODEL` 永远优先。
不要把这些名字当成契约。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    """一家服务商的静态信息。"""

    name: str
    #: 人类可读名，用于日志与健康报告（中文，便于运维一眼认出）。
    label: str
    #: OpenAI 兼容的 base_url。**必须**是各家文档给出的兼容端点，
    #: 而不是根域名 —— 后者会 404 或返回非预期内容。
    base_url: str
    #: 该服务商的密钥读哪个环境变量。
    api_key_env: str
    #: 出厂默认的文本模型。
    text_model: str
    #: 出厂默认的视觉模型；**空串表示该服务商没有视觉能力**。
    vlm_model: str
    note: str

    @property
    def supports_vision(self) -> bool:
        return bool(self.vlm_model)


#: 支持的服务商。
#:
#: 全部走 **OpenAI 兼容协议**，因此只需要一份客户端实现 + 不同的 base_url。
#: 这不是巧合：这三家（以及绝大多数国内厂商）都提供兼容端点，
#: 自建一套 SDK 抽象只会增加需要维护的面。
PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="SCID_OPENAI_API_KEY",
        text_model="gpt-4o",
        vlm_model="gpt-4o",
        note="官方端点。本机网络不通时无法验证（见 docs/ROADMAP.md）。",
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="SCID_DEEPSEEK_API_KEY",
        # deepseek-chat 是通用对话模型；deepseek-reasoner 会输出思维链，
        # 对"要求严格 JSON 输出"的场景反而更容易解析失败，故不作为默认。
        text_model="deepseek-chat",
        # **没有视觉能力**：见模块文档。
        vlm_model="",
        note="无视觉模型，因此不能单独承担 Critic 审查；需另配 SCID_VLM_PROVIDER。",
    ),
    "bailian": ProviderSpec(
        name="bailian",
        label="阿里云百炼（DashScope）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="SCID_DASHSCOPE_API_KEY",
        text_model="qwen-plus",
        # 百炼的视觉模型：既能审查画面，也具备中文能力。
        vlm_model="qwen-vl-max",
        note="文本与视觉都有；兼容端点是 /compatible-mode/v1（不是根域名）。",
    ),
    "mock": ProviderSpec(
        name="mock",
        label="本地占位（mock）",
        base_url="",
        api_key_env="",
        text_model="mock",
        vlm_model="mock",
        note="离线可跑通全流程，返回确定性占位结果；绝不用于生产内容。",
    ),
}

#: 视觉**不支持**时，审查会退化成"每个镜头都转人工"。
#: 这条常量是给健康报告与文案用的，避免各处重复写同一句话。
NO_VISION_HINT = (
    "该服务商没有视觉模型：Critic 审查将无法进行，**每个镜头都会降级转人工**。"
    "请设置 SCID_VLM_PROVIDER 指向支持视觉的服务商（如 bailian）。"
)


@dataclass(frozen=True)
class LLMTarget:
    """一次调用要用到的全部信息（文本或视觉各解析一份）。"""

    kind: str  # "text" / "vision"
    provider: str
    label: str
    model: str
    base_url: str
    api_key: str
    is_mock: bool
    supports_vision: bool
    #: 非空表示"这个目标不可用"，内容是原因（用于健康报告与日志）。
    problem: str

    @property
    def usable(self) -> bool:
        return self.problem == ""


def resolve_target(
    *,
    kind: str,
    provider: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
) -> LLMTarget:
    """把配置解析成一个可用的调用目标。

    `model` 为空时用该服务商的出厂默认；`base_url` 为空时用出厂默认；
    `api_key` 为空时读该服务商对应的密钥变量名（实际取值由 config 传入）。

    解析**不抛异常**：把问题写进 `problem` 字段，由调用方决定是拒绝启动、
    降级为 mock、还是只报健康告警 —— 三种处置各不相同，不该由这一层替它决定。
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        # 理论上到不了这里：provider 在 config 里是 Literal，非法值会在
        # 启动时被 pydantic 拦下。这里兜底是为了让"绕过 config 直接构造"的
        # 调用方（测试、脚本）也得到明确原因，而不是 KeyError。
        known = "/".join(sorted(PROVIDERS))
        return LLMTarget(
            kind=kind, provider=provider, label=provider, model=model,
            base_url=base_url, api_key=api_key, is_mock=False,
            supports_vision=False, problem=f"未知的模型服务商 {provider!r}（可用：{known}）",
        )

    if spec.name == "mock":
        return LLMTarget(
            kind=kind, provider="mock", label=spec.label, model=model or spec.text_model,
            base_url="", api_key="", is_mock=True, supports_vision=True, problem="",
        )

    resolved_model = model or (spec.vlm_model if kind == "vision" else spec.text_model)
    problem = ""
    if kind == "vision" and not spec.supports_vision:
        problem = NO_VISION_HINT
    elif not resolved_model:
        problem = "没有可用的模型名：请在配置里显式指定（该服务商没有出厂默认）"
    elif not api_key:
        # 无密钥时**降级为 mock**（与既有行为一致：保证离线可跑通全流程），
        # 但原因要写清楚 —— 这是"看起来在跑真模型、其实在跑占位内容"的入口。
        problem = (
            f"未配置密钥（环境变量 {spec.api_key_env} 或 SCID_LLM_API_KEY），"
            "将使用 mock 占位结果"
        )

    return LLMTarget(
        kind=kind,
        provider=spec.name,
        label=spec.label,
        model=resolved_model,
        base_url=base_url or spec.base_url,
        api_key=api_key,
        # 无密钥 ⇒ 走 mock 分支，因此这里也要如实标出来。
        is_mock=not api_key,
        supports_vision=spec.supports_vision,
        problem=problem,
    )
