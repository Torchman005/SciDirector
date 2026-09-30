#!/usr/bin/env python
"""豆包（火山引擎）TTS 接口探测：用真实 API 的返回码找出可用的参数组合。

## 为什么需要它

官方文档页（`docs.volcengine.com`）返回的是 JS 壳、抓不到正文，而豆包的同一个端点
靠 **`Resource-Id` 请求头**与 `cluster` 区分"开通了哪个产品"（老的非流式合成、
大模型语音合成、声音复刻各是一条线）。猜错的表现是一句含糊的「未开通/无权限」，
它**不会**告诉你少了个请求头。

所以这里不去猜，而是把组合**逐个真发一遍**，把服务端的原始 `code`/`message`
打出来。跑一次就能确定该填什么。

## 用法

先把凭据填进 `.env`（appid / access token），然后：

    python scripts/probe-doubao-tts.py
    python scripts/probe-doubao-tts.py --voice S_xxxxxxxx   # 试复刻出来的音色

脚本**不会打印 token**。找到可用组合后，它会直接给出该写进 `.env` 的几行。
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

#: 逐个尝试的 Resource-Id。空串代表"不带这个头"（既有版本的行为）。
RESOURCE_IDS = [
    "",
    "volc.service_type.10029",  # 大模型语音合成（复刻音色走这条）
    "volc.megatts.default",
    "volc.megatts.voiceclone",
]

#: 逐个尝试的 cluster。
CLUSTERS = ["volcano_tts", "volcano_mega"]

#: 探测用的一句话（够短，省额度；又够长，能听出是否正常）。
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


def build_payload(appid: str, token: str, cluster: str, voice: str) -> dict:
    """按「HTTP 非流式接口 V1」的形状构造请求体。

    字段名若与真实接口不符，服务端会以明确的信息拒绝（这正是我们要读的）。
    """
    return {
        "app": {"appid": appid, "token": token, "cluster": cluster},
        "user": {"uid": "scidirector-probe"},
        "audio": {"voice_type": voice, "encoding": "mp3", "sample_rate": 24000},
        "request": {"reqid": str(uuid.uuid4()), "text": PROBE_TEXT, "operation": "query"},
    }


def probe(endpoint: str, appid: str, token: str, cluster: str, resource_id: str, voice: str) -> dict:
    """发一次请求，返回归一化的结果（永不抛出，失败也是一种结果）。"""
    import httpx  # 延迟导入：缺依赖时报错更清楚

    headers = {
        # 注意是**分号**而不是空格；写成空格会得到鉴权失败，
        # 而错误信息通常不会点明这一点。
        "Authorization": f"Bearer;{token}",
        "Content-Type": "application/json",
    }
    if resource_id:
        headers["Resource-Id"] = resource_id

    try:
        resp = httpx.post(
            endpoint,
            json=build_payload(appid, token, cluster, voice),
            headers=headers,
            timeout=30.0,
        )
    except Exception as err:  # noqa: BLE001 - 探测脚本：任何异常都只是想报告的事实
        return {"http": None, "code": None, "message": f"网络错误: {err}", "bytes": 0}

    out: dict = {"http": resp.status_code, "code": None, "message": "", "bytes": 0}
    try:
        body = resp.json()
    except ValueError:
        out["message"] = f"非 JSON 响应: {resp.text[:200]}"
        return out

    out["code"] = body.get("code")
    out["message"] = str(body.get("message") or "")[:200]
    data = body.get("data") or ""
    if isinstance(data, str) and data:
        try:
            out["bytes"] = len(base64.b64decode(data))
        except Exception:  # noqa: BLE001
            out["bytes"] = -1
    return out


def main() -> int:
    load_env_file(ROOT / ".env")

    ap = argparse.ArgumentParser(description="探测豆包 TTS 的可用参数组合")
    ap.add_argument("--voice", default=os.environ.get("SCID_TTS_VOICE", "")
                    or "zh_female_shuangkuaisisi_moon_bigtts",
                    help="要试的音色（复刻出来的 speaker_id 填这里）")
    ap.add_argument("--endpoint", default=os.environ.get("SCID_DOUBAO_ENDPOINT", "")
                    or "https://openspeech.bytedance.com/api/v1/tts")
    args = ap.parse_args()

    appid = os.environ.get("SCID_DOUBAO_APPID", "").strip()
    token = os.environ.get("SCID_DOUBAO_ACCESS_TOKEN", "").strip()
    if not appid or not token:
        print("✗ 缺少凭据：请先在 .env 里填 SCID_DOUBAO_APPID 与 SCID_DOUBAO_ACCESS_TOKEN。")
        print("  （它们来自火山引擎控制台 → 语音技术 → 应用管理。本脚本不会打印 token。）")
        return 2

    print(f"端点   : {args.endpoint}")
    print(f"音色   : {args.voice}")
    print(f"appid  : {appid[:4]}…（长度 {len(appid)}）")
    print(f"token  : 已配置（长度 {len(token)}，不打印）\n")

    winners: list[tuple[str, str]] = []
    for cluster in CLUSTERS:
        for rid in RESOURCE_IDS:
            r = probe(args.endpoint, appid, token, cluster, rid, args.voice)
            ok = r["bytes"] > 0
            mark = "✓" if ok else "✗"
            label = f"cluster={cluster:<12} resource-id={rid or '(不带)'}"
            print(f"  {mark} {label:<48} http={r['http']} code={r['code']} "
                  f"bytes={r['bytes']} {r['message']}")
            if ok:
                winners.append((cluster, rid))

    print()
    if not winners:
        print("✗ 所有组合都失败了。把上面的 code/message 贴出来即可定位：")
        print("  - 「未开通」类 -> 该产品没在控制台开通，或 appid 用错了应用")
        print("  - 鉴权失败     -> token 不匹配该 appid")
        print("  - 音色不存在   -> --voice 换一个（复刻音色要等训练完成）")
        return 1

    cluster, rid = winners[0]
    print("✓ 可用组合（把它填进 .env 即可）：\n")
    print(f"SCID_DOUBAO_CLUSTER={cluster}")
    if rid:
        print(f"SCID_DOUBAO_RESOURCE_ID={rid}")
    print(f"SCID_TTS_VOICE={args.voice}")
    if len(winners) > 1:
        print(f"\n（另有 {len(winners) - 1} 个组合也可用，上面取的是第一个。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
