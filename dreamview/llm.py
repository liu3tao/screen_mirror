"""模型后端的公共部分：接口、调用计数与用量、错误、提示词、JSON 解析。

后端：gemini（AI Studio，默认）/ vertex（同一 SDK，走 Cloud 结算）/ ollama（本机）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import IMAGE_TOKENS, PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M, TOKEN_DEVIATION_WARN, Settings, load_prompt
from .schemas import Element, ModelOutputError, RegionsState, Scene, SceneAnalysis, ScoreBatchOut, Usage

M = TypeVar("M", bound=BaseModel)

BACKENDS = ("gemini", "vertex", "ollama")


class CallLimitExceeded(RuntimeError):
    pass


class ModelError(RuntimeError):
    """后端报错：原文 + 提示，UI 原样展示。"""

    def __init__(self, message: str, hint: str = "", code: int | None = None):
        super().__init__(message)
        self.hint = hint
        self.code = code


class ModelBackend(Protocol):
    name: str
    grounded: bool  # 找地区是否经 Google 搜索核实

    def analyze_scene(self, meter: "Meter", image: bytes, mime: str = "image/jpeg") -> SceneAnalysis: ...

    def find_regions(self, meter: "Meter", scene: Scene, scopes: Sequence[str]) -> RegionsState: ...

    def score_batch(self, meter: "Meter", elements: Sequence[Element], thumbs: Sequence[bytes]) -> ScoreBatchOut: ...


def make_backend(settings: Settings) -> ModelBackend:
    if settings.model_backend == "ollama":
        from .ollama import Ollama

        return Ollama(settings)
    if settings.model_backend not in BACKENDS:
        raise ValueError(f"MODEL_BACKEND 只能是 {'/'.join(BACKENDS)}，当前：{settings.model_backend}")
    from .gemini import Gemini

    return Gemini(settings)


class Meter:
    """一次 run 的调用计数与用量。"""

    def __init__(self, limit: int, usage: Usage | None = None):
        self.limit = limit
        self.usage = usage or Usage()
        self.warnings: list[str] = []
        self._warned_levels: set[str] = set()

    def before_call(self) -> None:
        if self.usage.calls >= self.limit:
            raise CallLimitExceeded(f"已达本次运行模型调用上限 {self.limit}（MAX_MODEL_CALLS_PER_RUN）")
        self.usage.calls += 1

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        thought_tokens: int = 0,
        image_tokens: int = 0,
        n_images: int = 0,
        media_res: str = "",
        priced: bool = True,
    ) -> None:
        u = self.usage
        u.input_tokens += input_tokens
        u.output_tokens += output_tokens
        u.thought_tokens += thought_tokens
        u.image_tokens += image_tokens
        u.images += n_images
        if priced:
            u.usd = round(
                u.usd
                + input_tokens * PRICE_INPUT_PER_M / 1e6
                + (output_tokens + thought_tokens) * PRICE_OUTPUT_PER_M / 1e6,
                4,
            )
        if n_images and image_tokens and media_res in IMAGE_TOKENS and media_res not in self._warned_levels:
            expected = IMAGE_TOKENS[media_res]
            actual = image_tokens / n_images
            if abs(actual - expected) / expected > TOKEN_DEVIATION_WARN:
                self._warned_levels.add(media_res)
                self.warnings.append(
                    f"单图输入 token ≈ {actual:.0f}，与 media_resolution={media_res} 的设定值 {expected} 偏差超过 "
                    f"{TOKEN_DEVIATION_WARN:.0%}：检查分辨率参数是否生效，费用可能高于预期。"
                )


# ---- 提示词 ----


def render(template: str, **values: str) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def regions_prompt(scene: Scene, scopes: Sequence[str], grounded: bool) -> str:
    verify = "请用 Google 搜索核实后，" if grounded else "根据你已有的知识（无法联网核实，不确定的地名宁可不给），"
    return render(
        load_prompt("regions"),
        verify=verify,
        summary=scene.summary,
        elements="\n".join(f"- {e.name}（权重 {e.weight:.1f}）：{e.description}" for e in scene.elements),
        scopes="、".join(scopes) if scopes else "全球",
    )


def score_prompt(elements: Sequence[Element], n: int) -> str:
    return render(
        load_prompt("score"),
        n=str(n),
        last=str(n - 1),
        elements="\n".join(f"- {e.name}：{e.description}" for e in elements),
        names=json.dumps([e.name for e in elements], ensure_ascii=False),
    )


# ---- JSON 解析 ----

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> str:
    """去掉 ``` 围栏；否则截取第一个 { 到最后一个 }。"""
    m = _FENCE.search(text or "")
    if m:
        return m.group(1).strip()
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start == -1 or end <= start:
        raise ModelOutputError("返回中没有 JSON")
    return text[start : end + 1]


def parse_output(resp, model: type[M]) -> M:
    """resp 为字符串，或带 .parsed / .text 的 SDK 响应。"""
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, model):
        return parsed
    text = resp if isinstance(resp, str) else (getattr(resp, "text", "") or "")
    try:
        return model.model_validate_json(extract_json(text))
    except (ValidationError, ValueError) as e:
        raise ModelOutputError(f"输出不合 schema：{e}") from e
