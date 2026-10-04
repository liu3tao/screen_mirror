"""Gemini 调用：场景解析、找地区（grounding）、批量打分。M2 加体检。

所有调用经过 Meter：计数（MAX_MODEL_CALLS_PER_RUN）、记录 usage、核对单图 token。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import (
    IMAGE_TOKENS,
    PRICE_INPUT_PER_M,
    PRICE_OUTPUT_PER_M,
    TOKEN_DEVIATION_WARN,
    Settings,
    load_prompt,
)
from .schemas import (
    Citation,
    Element,
    ModelOutputError,
    Region,
    RegionsOut,
    RegionsState,
    Scene,
    SceneAnalysis,
    ScoreBatchOut,
    Usage,
)

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)


class CallLimitExceeded(RuntimeError):
    pass


class GeminiError(RuntimeError):
    """API 报错：原文 + 提示，UI 原样展示。"""

    def __init__(self, message: str, hint: str = "", code: int | None = None):
        super().__init__(message)
        self.hint = hint
        self.code = code


def error_hint(code: int | None, message: str) -> str:
    msg = (message or "").lower()
    if code in (401, 403) or "api key" in msg or "permission" in msg:
        return "检查 GEMINI_API_KEY 是否正确、项目是否已启用 Gemini API（H2）。"
    if "billing" in msg or "free tier" in msg or "failed_precondition" in msg:
        return "Gemini 3 与 Search grounding 需要付费层：确认 AI Studio 项目已关联账单账户（H1/H2）。"
    if code == 429 or "quota" in msg or "exhausted" in msg:
        return "触发配额或 Spend Cap（H3）：稍后重试，或在 AI Studio 查看用量与上限。"
    if code == 404 or "not found" in msg:
        return "模型名可能已变更或下线：检查 .env 中的 GEMINI_MODEL。"
    if code and code >= 500:
        return "Gemini 服务端错误，稍后重试。"
    return ""


def render(template: str, **values: str) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


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
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, model):
        return parsed
    try:
        return model.model_validate_json(extract_json(resp.text or ""))
    except (ValidationError, ValueError) as e:
        raise ModelOutputError(f"输出不合 schema：{e}") from e


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

    def record(self, resp, n_images: int = 0, media_res: str = "") -> None:
        um = getattr(resp, "usage_metadata", None)
        if um is None:
            return
        prompt = (um.prompt_token_count or 0) + (getattr(um, "tool_use_prompt_token_count", 0) or 0)
        out = um.candidates_token_count or 0
        thoughts = um.thoughts_token_count or 0
        image_tokens = sum(
            (d.token_count or 0)
            for d in (um.prompt_tokens_details or [])
            if str(getattr(d.modality, "value", d.modality)).upper() == "IMAGE"
        )
        u = self.usage
        u.input_tokens += prompt
        u.output_tokens += out
        u.thought_tokens += thoughts
        u.image_tokens += image_tokens
        u.images += n_images
        u.usd = round(
            u.input_tokens * PRICE_INPUT_PER_M / 1e6 + (u.output_tokens + u.thought_tokens) * PRICE_OUTPUT_PER_M / 1e6,
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


class Gemini:
    def __init__(self, settings: Settings, client=None):
        self.s = settings
        self._client = client
        self._thinking_fallback: dict[str, str] = {}

    @property
    def client(self):
        if self._client is None:
            if not self.s.gemini_api_key:
                raise GeminiError("未设置 GEMINI_API_KEY", "复制 .env.example 为 .env 并填入 key（H2/H11）。")
            from google import genai

            self._client = genai.Client(api_key=self.s.gemini_api_key)
        return self._client

    # ---- 底层 ----

    def _config(self, *, media_res: str = "", thinking: str = "", schema=None, tools=None, system: str = ""):
        from google.genai import types

        kw: dict = {}
        if system:
            kw["system_instruction"] = system
        if media_res:
            kw["media_resolution"] = getattr(types.MediaResolution, f"MEDIA_RESOLUTION_{media_res.upper()}")
        if thinking:
            level = self._thinking_fallback.get(thinking, thinking)
            kw["thinking_config"] = types.ThinkingConfig(thinking_level=getattr(types.ThinkingLevel, level.upper()))
        if schema is not None:
            kw["response_mime_type"] = "application/json"
            kw["response_schema"] = schema
        if tools:
            kw["tools"] = tools
            kw["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        return types.GenerateContentConfig(**kw)

    def _generate(self, meter: Meter, model: str, contents, *, n_images=0, media_res="", thinking="", **cfg):
        from google.genai import errors

        meter.before_call()
        try:
            resp = self.client.models.generate_content(
                model=model, contents=contents, config=self._config(media_res=media_res, thinking=thinking, **cfg)
            )
        except errors.APIError as e:
            # minimal 思考档在部分模型不可用 → 降为 low 再试一次（3.5）
            if thinking == "minimal" and "thinking" in (e.message or "").lower() and "minimal" not in self._thinking_fallback:
                log.warning("thinking_level=minimal 不可用，改用 low：%s", e.message)
                self._thinking_fallback["minimal"] = "low"
                return self._generate(meter, model, contents, n_images=n_images, media_res=media_res, thinking=thinking, **cfg)
            raise GeminiError(
                f"Gemini API 错误 {e.code}：{e.message}", error_hint(e.code, e.message or ""), code=e.code
            ) from e
        meter.record(resp, n_images=n_images, media_res=media_res)
        return resp

    @staticmethod
    def _image_part(data: bytes, mime: str = "image/jpeg"):
        from google.genai import types

        return types.Part.from_bytes(data=data, mime_type=mime)

    # ---- FR-1 场景解析 ----

    def analyze_scene(self, meter: Meter, image: bytes, mime: str = "image/jpeg") -> SceneAnalysis:
        prompt = load_prompt("analyze")
        last: Exception | None = None
        for _ in range(2):
            resp = self._generate(
                meter,
                self.s.strong_model,
                [self._image_part(image, mime), prompt],
                n_images=1,
                media_res=self.s.media_res_analyze,
                thinking=self.s.thinking_level,
                schema=SceneAnalysis,
            )
            try:
                return parse_output(resp, SceneAnalysis)
            except ModelOutputError as e:
                last = e
        raise GeminiError(f"场景解析失败：{last}", "重试上传，或换一张图。")

    # ---- FR-4 找地区 ----

    def find_regions(self, meter: Meter, scene: Scene, scopes: Sequence[str]) -> RegionsState:
        from google.genai import types

        prompt = render(
            load_prompt("regions"),
            summary=scene.summary,
            elements="\n".join(f"- {e.name}（权重 {e.weight:.1f}）：{e.description}" for e in scene.elements),
            scopes="、".join(scopes) if scopes else "全球",
        )
        tools = [types.Tool(google_search=types.GoogleSearch())]
        state = RegionsState(scopes=list(scopes))
        try:
            resp = self._generate(
                meter, self.s.gemini_model, prompt, thinking=self.s.thinking_level_regions, tools=tools, schema=RegionsOut
            )
        except GeminiError as e:
            # 只有「无已知原因」的 400 才视为 schema + grounding 不兼容；key / 账单问题直接报错
            if e.code != 400 or e.hint:
                raise
            # response_schema + google_search 不可用 → JSON 文本回退（3.5）
            log.warning("schema + grounding 不可用，回退为 JSON 文本：%s", e)
            resp = self._generate(
                meter,
                self.s.gemini_model,
                prompt + "\n\n" + load_prompt("regions_json_fallback"),
                thinking=self.s.thinking_level_regions,
                tools=tools,
            )
        state.raw_text = resp.text or ""
        fill_grounding(state, resp)
        try:
            out = parse_output(resp, RegionsOut)
        except ModelOutputError as e:
            state.parse_error = f"{e}。请参考原始返回与 Search Suggestions 手动添加地区。"
            return state
        state.regions = [
            Region(id=f"r{i + 1}", **r.model_dump()) for i, r in enumerate(out.regions)
        ]
        return state

    # ---- FR-7 打分 ----

    def score_batch(self, meter: Meter, elements: Sequence[Element], thumbs: Sequence[bytes]) -> ScoreBatchOut:
        prompt = render(
            load_prompt("score"),
            n=str(len(thumbs)),
            last=str(len(thumbs) - 1),
            elements="\n".join(f"- {e.name}：{e.description}" for e in elements),
            names=json.dumps([e.name for e in elements], ensure_ascii=False),
        )
        contents: list = []
        for i, data in enumerate(thumbs):
            contents += [f"图 {i}：", self._image_part(data)]
        contents.append(prompt)
        resp = self._generate(
            meter,
            self.s.gemini_model,
            contents,
            n_images=len(thumbs),
            media_res=self.s.media_res_score,
            thinking=self.s.thinking_level,
            schema=ScoreBatchOut,
        )
        return parse_output(resp, ScoreBatchOut)


def fill_grounding(state: RegionsState, resp) -> None:
    """Search Suggestions 原样保存（条款要求展示），引用为直接链接。"""
    cands = getattr(resp, "candidates", None) or []
    gm = getattr(cands[0], "grounding_metadata", None) if cands else None
    if gm is None:
        return
    sep = getattr(gm, "search_entry_point", None)
    state.search_suggestions_html = (getattr(sep, "rendered_content", "") or "") if sep else ""
    state.web_search_queries = list(getattr(gm, "web_search_queries", None) or [])
    seen = set()
    for ch in getattr(gm, "grounding_chunks", None) or []:
        web = getattr(ch, "web", None)
        if web and web.uri and web.uri not in seen:
            seen.add(web.uri)
            state.citations.append(Citation(title=web.title or web.domain or "", uri=web.uri))
