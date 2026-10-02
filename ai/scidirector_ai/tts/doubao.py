"""豆包语音（火山引擎）TTS 适配器。

## 两套调用方式并存，必须显式选

火山引擎的豆包语音有**两代**接口，鉴权头、请求体、响应体**三处都不同**：

| | 老版（V1 非流式） | 新版（V3 单向流式） |
|---|---|---|
| 鉴权 | `Authorization: Bearer;<token>` | **`X-Api-Key: <key>`** |
| 产品线 | `Resource-Id: <id>` | `X-Api-Resource-Id: <id>` |
| 请求体 | `app` / `user` / `audio` / `request` | `user` / `req_params`（`speaker`） |
| 响应体 | 单个 JSON，`data` 是整段 base64 | **分块 / SSE**，逐行 JSON，音频分片 |

拿一套头去打另一套的端点，得到的是一句含糊的「鉴权失败 / 未开通」——
它**不会**告诉你用错了哪一代。因此这里把代次做成**显式配置**
（`auth_mode` 与 `api_style`），而不是靠猜。

## ⚠️ 验证状态

老版那套（`bearer` + `v1`）是按公开接口形状写的，**本机从未跑通**（没有凭据）。
新版那套（`api_key` + `v3`）是**按用户给出的鉴权方式**（`X-Api-Key`）实现的，
字段名同样**未经真机核对** —— 官方文档页是 JS 壳，抓不到正文。

因此做了四件事把风险压到最低：

1. 端点、资源 id、代次**全部可配置**，形状不符时不必改代码；
2. 响应解析**同时兼容**「单个 JSON」与「分块 / SSE 逐行 JSON」两种形态；
3. 失败时把服务端的 `code` / `message` / 响应片段原样带出来 —— 排查全靠它；
4. `scripts/probe-doubao-tts.py` 把矩阵逐个真发一遍，一次定论。

## 时间戳

两代接口能否返回时间信息取决于参数与版本，这里**不臆造字段**：`marks` 恒为空，
字幕回退到「按镜头真实音频时长对齐」（Go 侧已实现并验证）。
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

import httpx

from .base import SynthesisResult, TTSError, write_audio

DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"
#: 新版（V3）单向流式端点。可用 SCID_DOUBAO_ENDPOINT 覆盖。
DEFAULT_V3_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_CLUSTER = "volcano_tts"
DEFAULT_VOICE = "zh_female_shuangkuaisisi_moon_bigtts"

#: 鉴权代次。
AUTH_MODE_BEARER = "bearer"  # 老版：Authorization: Bearer;<token>
AUTH_MODE_API_KEY = "api_key"  # 新版：X-Api-Key

#: 请求体 / 响应体形态。
API_STYLE_V1 = "v1"  # app / audio / request，整段 base64
API_STYLE_V3 = "v3"  # user / req_params，分块或 SSE

#: 两代各自的成功码。
SUCCESS_CODE_V1 = 3000
SUCCESS_CODE_V3 = 0
#: 兼容旧名（等于老版成功码）。
#: 保留它是因为改名曾经**打断了既有调用方**（`test_tts.py` 就此收集失败）——
#: 一个仍在使用的公开名字不该无声消失。
SUCCESS_CODE = SUCCESS_CODE_V1

#: 已知失败码 -> **可操作**的中文提示。
#:
#: 为什么值得维护这么一张小表：服务端只回一个数字（如 45000292），
#: 而它对应的行动（"去控制台确认并发配额"）与这个数字之间没有任何线索。
#: 用户看到的是"返回失败码 45000292"，方向只能靠猜 —— 这个项目已经为
#: 「报错说了等于没说」付过好几次代价（哑成片、静默降级都是同一类问题）。
#:
#: 只放**已经真机见过**的码；猜的宁可不要，错提示比没有提示更坏。
KNOWN_ERROR_HINTS: dict[int, str] = {
    45000292: (
        "并发配额不足。到火山引擎控制台确认「声音复刻 / 语音合成大模型」的**并发数**"
        "是否已开通 —— 并发为 0 或已用满都会报这个；它与「音色数量」是两项不同的额度"
    ),
    45000000: (
        "请求被网关拒绝，通常是**端点与请求体形态不匹配** ——"
        "例如把复刻/训练端点（/api/v3/tts/voice_clone）当合成端点用"
    ),
}


def error_hint(code: object) -> str:
    """把失败码翻成可操作提示；没有登记的码返回空串（不编）。"""
    if code is None:
        return ""
    try:
        return KNOWN_ERROR_HINTS.get(int(code), "")
    except (TypeError, ValueError):
        return ""

MAX_TEXT_CHARS = 1000


class DoubaoTTSProvider:
    """火山引擎（豆包）语音合成。"""

    name = "doubao"

    def __init__(
        self,
        *,
        appid: str = "",
        access_token: str = "",
        cluster: str = DEFAULT_CLUSTER,
        endpoint: str = DEFAULT_ENDPOINT,
        resource_id: str = "",
        api_key: str = "",
        auth_mode: str = AUTH_MODE_BEARER,
        api_style: str = API_STYLE_V1,
        default_voice: str = DEFAULT_VOICE,
        encoding: str = "mp3",
        sample_rate: int = 24000,
        timeout_sec: int = 60,
    ) -> None:
        self.appid = appid
        self.access_token = access_token
        self.cluster = cluster
        self.endpoint = endpoint
        self.resource_id = resource_id
        self.api_key = api_key
        self.auth_mode = (auth_mode or AUTH_MODE_BEARER).strip().lower()
        self.api_style = (api_style or API_STYLE_V1).strip().lower()
        self.default_voice = default_voice
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.timeout_sec = timeout_sec

    def available(self) -> tuple[bool, str]:
        if self.auth_mode == AUTH_MODE_API_KEY:
            # 新版只要 API Key —— 这正是"新版调用方式"最省事的地方：
            # 不必再去凑 appid + access token 两个值。
            if not self.api_key:
                return False, "未配置 SCID_DOUBAO_API_KEY（新版鉴权 X-Api-Key 需要它）"
            return True, ""
        if not self.appid:
            return False, "未配置 SCID_DOUBAO_APPID（老版鉴权需要 appid）"
        if not self.access_token:
            return False, "未配置 SCID_DOUBAO_ACCESS_TOKEN"
        return True, ""

    # ------------------------------------------------------------------
    # 请求构造
    # ------------------------------------------------------------------

    def build_headers(self) -> dict[str, str]:
        """按代次构造鉴权与产品线请求头。"""
        headers = {"Content-Type": "application/json"}
        if self.auth_mode == AUTH_MODE_API_KEY:
            headers["X-Api-Key"] = self.api_key
            # 新版用 `X-Api-Resource-Id` 指明产品线（老版叫 `Resource-Id`）。
            if self.resource_id:
                headers["X-Api-Resource-Id"] = self.resource_id
            return headers

        # 老版的鉴权头格式是 `Bearer;<token>`（**分号**，不是空格）。
        # 写成空格会得到鉴权失败，而错误信息通常不会点明这一点。
        headers["Authorization"] = f"Bearer;{self.access_token}"
        if self.resource_id:
            headers["Resource-Id"] = self.resource_id
        return headers

    def build_payload(self, text: str, voice: str, speed: float | None) -> dict:
        """按代次构造请求体。"""
        if self.api_style == API_STYLE_V3:
            return self._build_payload_v3(text, voice, speed)
        return self._build_payload_v1(text, voice, speed)

    def _build_payload_v1(self, text: str, voice: str, speed: float | None) -> dict:
        audio: dict[str, object] = {
            "voice_type": voice,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
        }
        if speed is not None and speed > 0:
            audio["speed_ratio"] = round(speed, 2)
        return {
            "app": {"appid": self.appid, "token": self.access_token, "cluster": self.cluster},
            "user": {"uid": "scidirector"},
            "audio": audio,
            "request": {"reqid": str(uuid.uuid4()), "text": text, "operation": "query"},
        }

    def _build_payload_v3(self, text: str, voice: str, speed: float | None) -> dict:
        """V3 的请求体：`user` + `req_params`。

        与 V1 的差别不只是改名：音色字段叫 `speaker`、语速是**整百比**的偏移量
        （`speech_rate`，0 表示原速）而不是倍率。这些字段名**未经真机核对**，
        由探测脚本定论。
        """
        audio_params: dict[str, object] = {
            "format": self.encoding,
            "sample_rate": self.sample_rate,
        }
        if speed is not None and speed > 0:
            # 1.0 倍速 -> 0；1.5 -> +50；0.8 -> -20。
            audio_params["speech_rate"] = int(round((speed - 1.0) * 100))
        return {
            "user": {"uid": "scidirector"},
            "req_params": {"text": text, "speaker": voice, "audio_params": audio_params},
        }

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def _parse_response_body(self, raw: str) -> tuple[int | None, str, bytes]:
        """解析响应体，返回 (code, message, 音频字节)。

        **同时兼容两种形态**，因为两代接口的响应差别很大：

          * V1：单个 JSON `{"code":3000,"data":"<整段 base64>"}`
          * V3：分块 / SSE，逐行 JSON（可能带 `data: ` 前缀），音频被切成多片

        分片解码有个必须做对的细节：**逐片解码再拼接字节**，而不是把 base64
        字符串接起来再解码一次 —— 各片通常各自带 `=` 补齐，
        接起来再解码会得到乱码或直接失败。
        """
        code: int | None = None
        message = ""
        chunks: list[bytes] = []
        parsed_any = False

        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            # SSE 的 `data:` 前缀；也容忍 `data: {json}`。
            if line.startswith("data:"):
                line = line[len("data:") :].strip()
                if not line or line == "[DONE]":
                    continue
            try:
                obj = json.loads(line)
            except ValueError:
                # 不是 JSON 的行（心跳、纯文本错误）不该让整次解析失败 ——
                # SSE 里混着心跳是常态。
                continue
            if not isinstance(obj, dict):
                continue
            parsed_any = True
            if obj.get("code") is not None:
                code = obj["code"]
            if obj.get("message"):
                message = str(obj["message"])
            data = obj.get("data")
            if isinstance(data, str) and data:
                try:
                    chunk = base64.b64decode(data)
                except (ValueError, TypeError):
                    continue
                if chunk:
                    chunks.append(chunk)

        # **一行 JSON 都没解析出来**且没有音频时，把"响应根本不是 JSON"这件事说清楚。
        #
        # 这一条必须保留：最常见的现场是反向代理/网关把请求挡下来，
        # 返回一页 HTML 错误。此时如果只说"没有音频数据"，排查会毫无方向 ——
        # 而原文一眼就能看出问题在哪。
        #
        # 空响应（`raw` 为空）也算在内：那同样"没有可解析的 JSON"，
        # 而"服务端什么都没返回"本身就是要报出去的事实。
        if not parsed_any and not chunks:
            message = f"响应不是可解析的 JSON（前 200 字：{raw[:200]!r}）"

        # 整段是一个 JSON（V1）时上面的逐行解析同样成立 —— 单行也是行。
        return code, message, b"".join(chunks)

    def _is_success(self, code: int | None) -> bool:
        if code is None:
            # 没有任何 code 字段：V3 分块可能只在某一片给 code，也可能不给。
            # 此时以"有没有音频"为准（由调用方判断）。
            return True
        if self.api_style == API_STYLE_V3:
            return code == SUCCESS_CODE_V3
        return code == SUCCESS_CODE_V1

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------

    def synthesize(
        self,
        text: str,
        *,
        out_path: Path,
        voice: str | None = None,
        speed: float | None = None,
    ) -> SynthesisResult:
        ok, reason = self.available()
        if not ok:
            raise TTSError(reason, retryable=False, provider=self.name)

        clean = text.strip()
        if not clean:
            raise TTSError("文本为空，无需合成", retryable=False, provider=self.name)
        if len(clean) > MAX_TEXT_CHARS:
            raise TTSError(
                f"文本 {len(clean)} 字超过单次上限 {MAX_TEXT_CHARS} 字，请先在导演侧拆分镜头",
                retryable=False,
                provider=self.name,
            )

        endpoint = self.endpoint
        if self.api_style == API_STYLE_V3 and endpoint == DEFAULT_ENDPOINT:
            # 没显式配端点时，新版用新版端点：拿老端点配新头必然失败。
            endpoint = DEFAULT_V3_ENDPOINT

        payload = self.build_payload(clean, voice or self.default_voice, speed)
        headers = self.build_headers()

        try:
            resp = httpx.post(endpoint, json=payload, headers=headers, timeout=self.timeout_sec)
        except httpx.HTTPError as err:
            raise TTSError(f"豆包 TTS 网络错误: {err}", retryable=True, provider=self.name) from err

        if resp.status_code >= 500:
            raise TTSError(
                f"豆包 TTS 服务端错误 HTTP {resp.status_code}", retryable=True, provider=self.name
            )
        if resp.status_code >= 400:
            # 4xx 里最可能是鉴权 / 参数问题 —— 重试没有意义。
            # 把响应体一起带出来：用错代次时，真正的线索只在这里。
            raise TTSError(
                f"豆包 TTS 请求被拒 HTTP {resp.status_code}: {resp.text[:300]}",
                retryable=False,
                provider=self.name,
            )

        code, message, audio = self._parse_response_body(resp.text)
        hint = error_hint(code)
        if not self._is_success(code):
            raise TTSError(
                f"豆包 TTS 返回失败码 code={code} message={message!r}"
                f"（{self.api_style} 形态的成功码应为 "
                f"{SUCCESS_CODE_V3 if self.api_style == API_STYLE_V3 else SUCCESS_CODE_V1}）"
                # 把数字翻成"该去做什么"：界面上只显示一串错误码，
                # 用户无从判断是配额、鉴权还是音色问题（这已经让排查跑偏过好几轮）。
                + (f"\n可能的原因：{hint}" if hint else ""),
                # 失败码多为参数 / 权限问题，重试同样会失败；限流属少数派，
                # 交由上层按整体策略决定是否重投任务。
                retryable=False,
                provider=self.name,
            )
        if not audio:
            raise TTSError(
                f"豆包 TTS 没有返回音频数据（code={code} message={message!r}，"
                f"响应前 200 字：{resp.text[:200]!r}）"
                + (f"\n可能的原因：{hint}" if hint else ""),
                retryable=True,
                provider=self.name,
            )

        # 统一走 write_audio：它会 resolve 并返回绝对路径，
        # 跨进程交给 Go worker 时相对路径必然失效。
        written = write_audio(out_path, audio)
        return SynthesisResult(
            audio_path=written, duration_sec=0.0, marks=(), provider=self.name
        )
