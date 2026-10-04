"""测试工具：合成图片、伪造 Gemini 响应。不联网、不需要 API Key。"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw


def make_image(seed: int, size=(640, 480), fmt="PNG") -> bytes:
    """按 seed 生成结构不同的图：不同 seed 的 dHash 差异大。"""
    img = Image.new("RGB", size, (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    d = ImageDraw.Draw(img)
    w, h = size
    for k in range(6):
        x = (seed * 97 + k * 131) % w
        y = (seed * 61 + k * 89) % h
        d.rectangle([x, y, x + w // 4, y + h // 5], fill=((seed + k) * 71 % 255, (k * 40) % 255, 255 - (seed * 13 % 255)))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def fake_usage(prompt=1000, out=100, thoughts=0, image_tokens=0):
    details = [SimpleNamespace(modality=SimpleNamespace(value="IMAGE"), token_count=image_tokens)] if image_tokens else []
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=out,
        thoughts_token_count=thoughts,
        tool_use_prompt_token_count=0,
        prompt_tokens_details=details,
    )


def fake_response(text="", parsed=None, usage=None, grounding=None):
    cand = SimpleNamespace(grounding_metadata=grounding)
    return SimpleNamespace(text=text, parsed=parsed, usage_metadata=usage or fake_usage(), candidates=[cand])


class FakeModels:
    """记录调用；按顺序返回预设响应或抛出异常。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(contents) if callable(r) else r


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


@pytest.fixture
def tmp_settings(tmp_path):
    from dreamview.config import Settings

    return Settings(gemini_api_key="test", runs_dir=tmp_path / "runs", search_interval_s=0, download_workers=2)
