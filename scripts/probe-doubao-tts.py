#!/usr/bin/env python
"""豆包（火山引擎）TTS 接口探测：用真实 API 的返回码找出可用的参数组合。

## 为什么需要它

豆包语音有**两代**调用方式，鉴权头、请求体、响应体**三处都不同**：

| | 老版（V1 非流式） | 新版（V3 单向流式） |
|---|---|---|
| 鉴权 | `Authorization: Bearer;<token>` | **`X-Api-Key: <key>`** |
| 产品线 | `Resource-Id` | `X-Api-Resource-Id` |
| 请求体 | `app`/`user`/`audio`/`request` | `user`/`req_params`（`speaker`） |
| 响应体 | 单个 JSON | 分块 / SSE |

拿一套头去打另一代的端点，得到的是一句含糊的「鉴权失败/未开通」—— 它**不会**
告诉你用错了哪代。而官方文档页是 JS 壳、抓不到正文。

所以这里不去猜，而是把组合**逐个真发一遍**，把服务端的原始 `code`/`message`
打出来。跑一次就能确定该填什么。

## 用法

先把凭据填进 `.env`（新版只要 `SCID_DOUBAO_API_KEY`；老版要 appid + token），然后：

    python scripts/probe-doubao-tts.py
    python scripts/probe-doubao-tts.py --voice S_xxxxxxxx   # 试复刻出来的音色

脚本**不会打印密钥**。找到可用组合后，它会直接给出该写进 `.env` 的几行。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ENDPOINT_V1 = "https://openspeech.bytedance.com/api/v1/tts"
ENDPOINT_V3 = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"

#: 逐个尝试的 Resource-Id。空串代表"不带这个头"。
RESOURCE_IDS = [
    "",
    "volc.service_type.10029",  # 大模型语音合成（复刻音色通常走这条）
    "volc.megatts.default",
    "volc.megatts.voiceclone",
]

CLUSTERS = ["volcano_tts", "volcano_mega"]

#: 探测用的一句话（够短省额度，又够长能听出是否正常）。
PROBE_TEXT = "接口探测。"


def load_env_file(path: Path) -> None:
    """把 .env 读进环境变量（不覆盖已显式设置的）。

    刻意不依赖 python-dotenv：这个脚本要能在"依赖没装好"的环境里也跑起来 ——
    它本来就是用来排查问题的，自己再因为缺依赖跑不动就说不过去了。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def build_headers(auth_mode: str, api_key: str, token: str, resource_id: str) -> dict:
    """按代次构造请求头。"""
    headers = {"Content-Type": "application/json"}
    if auth_mode == "api_key":
        headers["X-Api-Key"] = api_key
        if resource_id:
            headers["X-Api-Resource-Id"] = resource_id
        return headers
    # 老版鉴权是 `Bearer;<token>`（分号，不是空格）。
    headers["Authorization"] = f"Bearer;{token}"
    if resource_id:
        headers["Resource-Id"] = resource_id
    return headers


def build_payload(api_style: str, appid: str, token: str, cluster: str, voice: str) -> dict:
    """按代次构造请求体。"""
    if api_style == "v3":
        return {
            "user": {"uid": "scidirector-probe"},
            "req_params": {
                "text": PROBE_TEXT,
                "speaker": voice,
                "audio_params": {"format": "mp3", "sample_rate": 24000},
            },
        }
    return {
        "app": {"appid": appid, "token": token, "cluster": cluster},
        "user": {"uid": "scidirector-probe"},
        "audio": {"voice_type": voice, "encoding": "mp3", "sample_rate": 24000},
        "request": {"reqid": str(uuid.uuid4()), "text": PROBE_TEXT, "operation": "query"},
    }


def parse_audio_bytes(raw: str) -> tuple[object, str, int]:
    """从响应里取出 (code, message, 音频字节数)。

    同时兼容两代：老版是单个 JSON，新版是分块/SSE 的逐行 JSON。
    分片必须**逐片解码** —— 各片通常各自带 `=` 补齐，把 base64 串接起来
    再解码一次会失败。
    """
    code = None
    message = ""
    total = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
            if not line or line == "[DONE]":
                continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("code") is not None:
            code = obj["code"]
        if obj.get("message"):
            message = str(obj["message"])
        data = obj.get("data")
        if isinstance(data, str) and data:
            try:
                total += len(base64.b64decode(data))
            except Exception:  # noqa: BLE001 - 探测脚本：解码失败只是想报告的事实
                pass
    return code, message, total


def probe(
    endpoint: str,
    auth_mode: str,
    api_style: str,
    appid: str,
    token: str,
    api_key: str,
    cluster: str,
    resource_id: str,
    voice: str,
) -> dict:
    """发一次请求；永不抛出，失败也是一种结果。"""
    import httpx  # 延迟导入：缺依赖时报错更清楚

    try:
        resp = httpx.post(
            endpoint,
            json=build_payload(api_style, appid, token, cluster, voice),
            headers=build_headers(auth_mode, api_key, token, resource_id),
            timeout=30.0,
        )
    except Exception as err:  # noqa: BLE001
        return {"http": None, "code": None, "message": f"网络错误: {err}", "bytes": 0}

    code, message, nbytes = parse_audio_bytes(resp.text)
    return {
        "http": resp.status_code,
        "code": code,
        "message": (message or resp.text[:120]).replace("\n", " ")[:160],
        "bytes": nbytes,
    }


def main() -> int:
    load_env_file(ROOT / ".env")

    ap = argparse.ArgumentParser(description="探测豆包 TTS 的可用参数组合")
    ap.add_argument("--voice", default=os.environ.get("SCID_TTS_VOICE", "")
                    or "zh_female_shuangkuaisisi_moon_bigtts",
                    help="要试的音色（复刻出来的 speaker_id 填这里）")
    ap.add_argument("--endpoint", default=os.environ.get("SCID_DOUBAO_ENDPOINT", ""),
                    help="留空则按代次自动选 v1/v3 端点")
    args = ap.parse_args()

    appid = os.environ.get("SCID_DOUBAO_APPID", "").strip()
    token = os.environ.get("SCID_DOUBAO_ACCESS_TOKEN", "").strip()
    api_key = os.environ.get("SCID_DOUBAO_API_KEY", "").strip()

    if not api_key and not (appid and token):
        print("✗ 缺少凭据。二选一：")
        print("  新版：在 .env 填 SCID_DOUBAO_API_KEY")
        print("  老版：在 .env 填 SCID_DOUBAO_APPID 与 SCID_DOUBAO_ACCESS_TOKEN")
        print("  （本脚本不会打印任何密钥。）")
        return 2

    print(f"音色   : {args.voice}")
    print(f"appid  : {appid[:4] + '…' if appid else '(未配置)'}")
    print(f"api_key: {'已配置（长度 %d）' % len(api_key) if api_key else '(未配置)'}")
    print(f"token  : {'已配置（长度 %d）' % len(token) if token else '(未配置）'}")
    print()

    winners: list[tuple[str, str, str, str]] = []
    for auth_mode in ("api_key", "bearer"):
        if auth_mode == "api_key" and not api_key:
            continue
        if auth_mode == "bearer" and not (appid and token):
            continue
        for api_style in ("v3", "v1"):
            endpoint = args.endpoint or (ENDPOINT_V3 if api_style == "v3" else ENDPOINT_V1)
            for cluster in (CLUSTERS if api_style == "v1" else ["-"]):
                for rid in RESOURCE_IDS:
                    r = probe(endpoint, auth_mode, api_style, appid, token, api_key,
                              cluster, rid, args.voice)
                    ok = r["bytes"] > 0
                    label = (f"auth={auth_mode:<8} style={api_style} "
                             f"cluster={cluster:<12} rid={rid or '(不带)'}")
                    print(f"  {'✓' if ok else '✗'} {label:<62} http={r['http']} "
                          f"code={r['code']} bytes={r['bytes']} {r['message']}")
                    if ok:
                        winners.append((auth_mode, api_style, cluster, rid))

    print()
    if not winners:
        print("✗ 所有组合都失败了。把上面的 code/message 贴出来即可定位：")
        print("  - 「未开通」类 -> 该产品没在控制台开通，或密钥用错了应用")
        print("  - 鉴权失败     -> 密钥与代次不匹配（新版要 X-Api-Key，老版要 Bearer;）")
        print("  - 音色不存在   -> --voice 换一个（复刻音色要等训练完成）")
        return 1

    auth_mode, api_style, cluster, rid = winners[0]
    print("✓ 可用组合（把它填进 .env 即可）：\n")
    print(f"SCID_DOUBAO_AUTH_MODE={auth_mode}")
    print(f"SCID_DOUBAO_API_STYLE={api_style}")
    if auth_mode == "bearer":
        print(f"SCID_DOUBAO_CLUSTER={cluster}")
    if rid:
        print(f"SCID_DOUBAO_RESOURCE_ID={rid}")
    print(f"SCID_TTS_VOICE={args.voice}")
    if len(winners) > 1:
        print(f"\n（另有 {len(winners) - 1} 个组合也可用，上面取的是第一个。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
