"""Ollama 后端（MODEL_BACKEND=ollama）：本机视觉模型，免费、离线。

与 Gemini 的差别：找地区没有 Google 搜索核实（grounded=False，UI 标注）；无费用；
没有 media_resolution，参考图在本地压到长边 ≤ 1024 以控制耗时。
"""

from __future__ import annotations

import base64
import io
from collections.abc import Sequence

import httpx
from PIL import Image
from pydantic import BaseModel

from .config import Settings, load_prompt
from .llm import Meter, ModelError, parse_output, regions_prompt, score_prompt
from .schemas import (
    Element,
    ModelOutputError,
    Region,
    RegionsOut,
    RegionsState,
    Scene,
    SceneAnalysis,
    ScoreBatchOut,
)

REF_MAX_SIDE = 1024


def inline_refs(schema: dict) -> dict:
    """展开 Pydantic schema 中的 $defs/$ref，避免 Ollama 的 schema→语法转换不支持引用。"""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def _b64(data: bytes, max_side: int | None = None) -> str:
    if max_side:
        img = Image.open(io.BytesIO(data))
        if max(img.size) > max_side:
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
    return base64.b64encode(data).decode("ascii")


class Ollama:
    name = "ollama"
    grounded = False

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        # 本机服务不走代理；本地推理可能很慢
        self._client = client or httpx.Client(trust_env=False, timeout=httpx.Timeout(600, connect=5))

    def _chat(self, meter: Meter, prompt: str, images: Sequence[str], schema: type[BaseModel]) -> str:
        if not self.s.ollama_model:
            raise ModelError("未设置 OLLAMA_MODEL", "在 .env 填入已 pull 的视觉模型名，如 qwen2.5vl:7b。")
        meter.before_call()
        body = {
            "model": self.s.ollama_model,
            "messages": [{"role": "user", "content": prompt, "images": list(images)}],
            "format": inline_refs(schema.model_json_schema()),
            "stream": False,
            "options": {"temperature": 0},
        }
        url = self.s.ollama_url.rstrip("/") + "/api/chat"
        try:
            r = self._client.post(url, json=body)
        except httpx.ConnectError as e:
            raise ModelError(f"连不上 Ollama（{url}）", "先启动 Ollama 应用或执行 ollama serve；检查 OLLAMA_URL。") from e
        except httpx.TimeoutException as e:
            raise ModelError("Ollama 响应超时", "换更小的模型，或在 .env 调小 SCORE_BATCH_SIZE。") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text
            hint = f"先执行 ollama pull {self.s.ollama_model}" if r.status_code == 404 else ""
            if "image" in msg.lower() or "vision" in msg.lower():
                hint = "该模型可能不支持图片或多图：换视觉模型，或设 SCORE_BATCH_SIZE=1。"
            raise ModelError(f"Ollama 错误 {r.status_code}：{msg}", hint, code=r.status_code)
        data = r.json()
        meter.add(
            input_tokens=data.get("prompt_eval_count", 0) or 0,
            output_tokens=data.get("eval_count", 0) or 0,
            n_images=len(images),
            priced=False,
        )
        return (data.get("message") or {}).get("content", "")

    # ---- FR-1 场景解析 ----

    def analyze_scene(self, meter: Meter, image: bytes, mime: str = "image/jpeg") -> SceneAnalysis:
        img = _b64(image, REF_MAX_SIDE)
        last: Exception | None = None
        for _ in range(2):
            text = self._chat(meter, load_prompt("analyze"), [img], SceneAnalysis)
            try:
                return parse_output(text, SceneAnalysis)
            except ModelOutputError as e:
                last = e
        raise ModelError(f"场景解析失败：{last}", "重试上传，或换一个更强的本地模型。")

    # ---- FR-4 找地区（无 grounding） ----

    def find_regions(self, meter: Meter, scene: Scene, scopes: Sequence[str]) -> RegionsState:
        state = RegionsState(scopes=list(scopes), grounded=False)
        state.raw_text = self._chat(meter, regions_prompt(scene, scopes, grounded=False), [], RegionsOut)
        try:
            out = parse_output(state.raw_text, RegionsOut)
        except ModelOutputError as e:
            state.parse_error = f"{e}。请参考原始返回手动添加地区。"
            return state
        state.regions = [Region(id=f"r{i + 1}", **r.model_dump()) for i, r in enumerate(out.regions)]
        return state

    # ---- FR-7 打分 ----

    def score_batch(self, meter: Meter, elements: Sequence[Element], thumbs: Sequence[bytes]) -> ScoreBatchOut:
        prompt = "附图按顺序编号。\n" + score_prompt(elements, len(thumbs))
        text = self._chat(meter, prompt, [_b64(t) for t in thumbs], ScoreBatchOut)
        return parse_output(text, ScoreBatchOut)
