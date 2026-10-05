"""Gemini 后端：AI Studio（MODEL_BACKEND=gemini，默认）与 Vertex AI（MODEL_BACKEND=vertex）。

两者同一 SDK（google-genai），只有客户端初始化与报错提示不同。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .config import Settings, load_prompt
from .llm import Meter, ModelError, parse_output, regions_prompt, score_prompt
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
)

log = logging.getLogger(__name__)


def error_hint(code: int | None, message: str, backend: str = "gemini") -> str:
    msg = (message or "").lower()
    vertex = backend == "vertex"
    if code in (401, 403) or "api key" in msg or "permission" in msg:
        if vertex:
            return (
                "确认项目已启用 Vertex AI API、本机已执行 gcloud auth application-default login，"
                "且账号对 GOOGLE_CLOUD_PROJECT 有 Vertex AI User 权限。"
            )
        return "检查 GEMINI_API_KEY 是否正确、项目是否已启用 Gemini API（H2）。"
    if "billing" in msg or "free tier" in msg or "failed_precondition" in msg:
        if vertex:
            return "确认 Cloud 项目已关联结算账户，且试用额度未用完、未过期。"
        return "Gemini 3 与 Search grounding 需要付费层：确认 AI Studio 项目已关联账单或预付费（H1/H2）。"
    if code == 429 or "quota" in msg or "exhausted" in msg:
        if vertex:
            return "触发 Vertex AI 配额（试用账户不能申请提额）：稍后重试。"
        return "触发配额或 Spend Cap（H3）：稍后重试，或在 AI Studio 查看用量与上限。"
    if code == 404 or "not found" in msg:
        if vertex:
            return "该区域可能没有此模型：检查 GEMINI_MODEL，或试 GOOGLE_CLOUD_LOCATION=us-central1。"
        return "模型名可能已变更或下线：检查 .env 中的 GEMINI_MODEL。"
    if code and code >= 500:
        return "Gemini 服务端错误，稍后重试。"
    return ""


def record_usage(meter: Meter, resp, n_images: int = 0, media_res: str = "") -> None:
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return
    image_tokens = sum(
        (d.token_count or 0)
        for d in (um.prompt_tokens_details or [])
        if str(getattr(d.modality, "value", d.modality)).upper() == "IMAGE"
    )
    meter.add(
        input_tokens=(um.prompt_token_count or 0) + (getattr(um, "tool_use_prompt_token_count", 0) or 0),
        output_tokens=um.candidates_token_count or 0,
        thought_tokens=um.thoughts_token_count or 0,
        image_tokens=image_tokens,
        n_images=n_images,
        media_res=media_res,
    )


class Gemini:
    grounded = True

    def __init__(self, settings: Settings, client=None):
        self.s = settings
        self.name = "vertex" if settings.model_backend == "vertex" else "gemini"
        self._client = client
        self._thinking_fallback: dict[str, str] = {}

    @property
    def client(self):
        if self._client is None:
            from google import genai

            if self.name == "vertex":
                if not self.s.google_cloud_project:
                    raise ModelError(
                        "未设置 GOOGLE_CLOUD_PROJECT", "在 .env 填入 Cloud 项目 ID（控制台首页可见），见 README 的 Vertex 一节。"
                    )
                self._client = genai.Client(
                    vertexai=True, project=self.s.google_cloud_project, location=self.s.google_cloud_location
                )
            else:
                if not self.s.gemini_api_key:
                    raise ModelError("未设置 GEMINI_API_KEY", "复制 .env.example 为 .env 并填入 key（H2/H11）。")
                self._client = genai.Client(api_key=self.s.gemini_api_key)
        return self._client

    # ---- 底层 ----

    def _config(self, *, media_res: str = "", thinking: str = "", schema=None, tools=None):
        from google.genai import types

        kw: dict = {}
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
        from google.auth import exceptions as auth_errors
        from google.genai import errors

        meter.before_call()
        try:
            resp = self.client.models.generate_content(
                model=model, contents=contents, config=self._config(media_res=media_res, thinking=thinking, **cfg)
            )
        except auth_errors.GoogleAuthError as e:
            raise ModelError(
                f"Google Cloud 认证失败：{e}", "本机执行 gcloud auth application-default login，然后重启 dreamview。"
            ) from e
        except errors.APIError as e:
            # minimal 思考档在部分模型不可用 → 降为 low 再试一次（3.5）
            if thinking == "minimal" and "thinking" in (e.message or "").lower() and "minimal" not in self._thinking_fallback:
                log.warning("thinking_level=minimal 不可用，改用 low：%s", e.message)
                self._thinking_fallback["minimal"] = "low"
                return self._generate(meter, model, contents, n_images=n_images, media_res=media_res, thinking=thinking, **cfg)
            raise ModelError(
                f"Gemini API 错误 {e.code}：{e.message}", error_hint(e.code, e.message or "", self.name), code=e.code
            ) from e
        record_usage(meter, resp, n_images=n_images, media_res=media_res)
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
        raise ModelError(f"场景解析失败：{last}", "重试上传，或换一张图。")

    # ---- FR-4 找地区 ----

    def find_regions(self, meter: Meter, scene: Scene, scopes: Sequence[str]) -> RegionsState:
        from google.genai import types

        prompt = regions_prompt(scene, scopes, grounded=True)
        tools = [types.Tool(google_search=types.GoogleSearch())]
        state = RegionsState(scopes=list(scopes))
        try:
            resp = self._generate(
                meter, self.s.gemini_model, prompt, thinking=self.s.thinking_level_regions, tools=tools, schema=RegionsOut
            )
        except ModelError as e:
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
        state.regions = [Region(id=f"r{i + 1}", **r.model_dump()) for i, r in enumerate(out.regions)]
        return state

    # ---- FR-7 打分 ----

    def score_batch(self, meter: Meter, elements: Sequence[Element], thumbs: Sequence[bytes]) -> ScoreBatchOut:
        contents: list = []
        for i, data in enumerate(thumbs):
            contents += [f"图 {i}：", self._image_part(data)]
        contents.append(score_prompt(elements, len(thumbs)))
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
