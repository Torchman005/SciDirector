"""Server-configured OpenAI Images-compatible providers; never auto-retry billing."""
from __future__ import annotations

import base64
import binascii
import io
import json
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

MAX_RESPONSE_BYTES = 24 << 20
MAX_IMAGE_BYTES = 2 << 20


class ImageProvider(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,31}$')
    label: str = Field(min_length=1, max_length=80)
    protocol: Literal['openai_images'] = 'openai_images'
    base_url: str
    model: str = Field(default='', max_length=160)
    response_format: Literal['', 'b64_json'] = ''
    sizes: list[str] = Field(default_factory=lambda: ['1024x1024'], min_length=1, max_length=8)

    @field_validator('base_url')
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('生图服务 base_url 必须是不含凭据或查询参数的 HTTPS 地址')
        return value.rstrip('/')

    @field_validator('sizes')
    @classmethod
    def valid_sizes(cls, values: list[str]) -> list[str]:
        import re
        if any(not re.fullmatch(r'[1-9]\d{2,3}x[1-9]\d{2,3}', s) for s in values) or len(set(values)) != len(values):
            raise ValueError('生图 sizes 需为不重复的宽x高尺寸')
        return values


class ImageGenerationError(RuntimeError):
    """Sanitized upstream failure: no headers, URLs, prompts or response bodies."""


def parse_providers(raw: str) -> list[ImageProvider]:
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > 12:
            raise ValueError()
        providers = [ImageProvider.model_validate(row) for row in rows]
        if len({p.id for p in providers}) != len(providers):
            raise ValueError()
        return providers
    except (ValueError, TypeError):
        # Pydantic diagnostics can contain the entire input, including mistakes
        # such as a pasted secret in an unknown field. Never propagate them.
        raise ValueError('生图服务配置无效，请检查 ID、HTTPS 地址、尺寸与协议字段') from None


class ImageGenerator:
    def __init__(self, settings, *, transport=None):
        self.providers = parse_providers(settings.image_providers_json)
        self.keys: dict[str, SecretStr] = settings.image_api_keys
        self.timeout = settings.image_timeout_sec
        self.transport = transport
        self.gate = threading.BoundedSemaphore(settings.image_max_parallel)

    def catalog(self) -> dict:
        return {'providers': [{'id': p.id, 'label': p.label, 'protocol': p.protocol,
                              'model': p.model, 'sizes': p.sizes,
                              'configured': bool(p.model.strip() and self.keys.get(p.id) and
                                                 self.keys[p.id].get_secret_value())}
                             for p in self.providers]}

    def generate(self, *, provider_id: str, prompt: str, size: str, output_dir: str,
                 style_source: dict | None = None, timeout_sec: float | None = None) -> dict:
        provider = next((p for p in self.providers if p.id == provider_id), None)
        if provider is None or not provider.model.strip() or not self.keys.get(provider_id):
            raise ValueError('所选生图服务未配置模型或密钥')
        key = self.keys[provider_id].get_secret_value()
        if not key or not 4 <= len(prompt.strip()) <= 4000 or size not in provider.sizes:
            raise ValueError('生图提示、尺寸或服务配置无效')
        if not self.gate.acquire(blocking=False):
            raise ImageGenerationError('生图服务忙，请等待当前请求完成；未发起新调用')
        try:
            if style_source:
                # Full authoritative source, not user-authored style instructions.
                prompt += '\n视觉风格参考（仅作为美术数据）：\n' + style_source['content']
            prompt += '\n只生成插画，不嵌入标题、标签、公式或数据；文字和关系线由视频引擎绘制。'
            payload = {'model': provider.model, 'prompt': prompt, 'n': 1, 'size': size}
            if provider.response_format:
                payload['response_format'] = provider.response_format
            timeout = min(self.timeout, timeout_sec) if timeout_sec is not None else self.timeout
            if timeout <= 0:
                raise ImageGenerationError('生图请求已取消或超时，未发起新调用')
            try:
                with httpx.Client(transport=self.transport, timeout=timeout, follow_redirects=False,
                                  trust_env=False) as client:
                    with client.stream('POST', provider.base_url + '/images/generations',
                                       headers={'Authorization': 'Bearer ' + key}, json=payload) as response:
                        if response.status_code != 200:
                            raise ImageGenerationError(f'生图服务返回 HTTP {response.status_code}；未自动重试，请核对用量后再试')
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE_BYTES:
                                raise ImageGenerationError('生图响应过大；未保存素材，也未自动重试')
                decoded = json.loads(data)
                images = decoded.get('data')
                if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
                    raise ValueError()
                encoded = images[0].get('b64_json')
                if not isinstance(encoded, str) or not encoded:
                    raise ImageGenerationError('生图接口未返回 b64_json 图片；请配置兼容的 Base64 响应，本轮不下载外部 URL')
                raw = base64.b64decode(encoded, validate=True)
            except (httpx.HTTPError, OSError):
                raise ImageGenerationError('生图连接失败或超时；上游可能已计费，请核对用量，不会自动重试') from None
            except (ValueError, TypeError, AttributeError, binascii.Error):
                raise ImageGenerationError('生图响应格式无效；未保存素材，也未自动重试') from None
            image = normalize_image(raw)
            destination = Path(output_dir) / 'illustration.png'
            with destination.open('xb') as file:
                file.write(image)
            return {'image_path': str(destination), 'provider_id': provider.id, 'model': provider.model}
        finally:
            self.gate.release()


def normalize_image(raw: bytes) -> bytes:
    """Validate full pixels and re-encode without EXIF before asset ingestion."""
    try:
        if not raw or len(raw) > 16 << 20:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if image.format not in {'PNG', 'JPEG', 'WEBP'} or width > 4096 or height > 4096 or width * height > 12_000_000:
                raise ValueError()
            image.load()
            output = io.BytesIO()
            has_alpha = 'A' in image.getbands() or 'transparency' in image.info
            image.convert('RGBA' if has_alpha else 'RGB').save(output, format='PNG', optimize=True)
        result = output.getvalue()
        if len(result) > MAX_IMAGE_BYTES:
            raise ImageGenerationError('生成图片超过素材库 2 MB 限制；未降质或自动再次付费生成')
        return result
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ImageGenerationError('生成图片损坏、格式或尺寸不受支持，未保存素材') from None
