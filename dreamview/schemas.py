"""数据结构：模型输出 schema（交给 Gemini response_schema）与 run 内持久化结构。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelOutputError(ValueError):
    """模型输出缺失或不合 schema。"""

# ---- 模型输出 schema（字段尽量少、全部必填，便于结构化输出） ----


class Element(BaseModel):
    name: str = Field(description="核心要素名称，简短")
    description: str = Field(description="判断标准，一句话")
    weight: float = Field(ge=0, le=1, description="重要度 0–1")


class SceneAnalysis(BaseModel):
    summary: str
    elements: list[Element]


class RegionOut(BaseModel):
    city: str
    district: str
    reason: str
    months: str = Field(description="推荐月份，如 10–11 月")
    keywords: list[str] = Field(description="2–3 条本地语言图片搜索关键词")
    search_region: str = Field(description="DuckDuckGo 区域代码，如 cn-zh、jp-jp、it-it；未知用 wt-wt")


class RegionsOut(BaseModel):
    regions: list[RegionOut]


class ElementScore(BaseModel):
    element: str
    score: int = Field(ge=0, le=10)


class ImageScore(BaseModel):
    index: int
    scores: list[ElementScore]


class ScoreBatchOut(BaseModel):
    results: list[ImageScore]


# ---- run 内持久化结构 ----


class Scene(BaseModel):
    summary: str = ""
    elements: list[Element] = []


class Region(BaseModel):
    id: str
    city: str
    district: str = ""
    reason: str = ""
    months: str = ""
    keywords: list[str] = []
    search_region: str = "wt-wt"
    selected: bool = False
    manual: bool = False

    @property
    def label(self) -> str:
        return f"{self.city} · {self.district}" if self.district else self.city


class Citation(BaseModel):
    title: str = ""
    uri: str


class RegionsState(BaseModel):
    scopes: list[str] = []
    grounded: bool = True  # False：本地模型给出，未经 Google 搜索核实
    regions: list[Region] = []
    raw_text: str = ""
    search_suggestions_html: str = ""
    citations: list[Citation] = []
    web_search_queries: list[str] = []
    parse_error: str = ""


class WallItem(BaseModel):
    id: str
    page_url: str
    image_url: str = ""
    thumb_url: str
    thumb_file: str = ""
    title: str = ""
    region_id: str = ""
    region_label: str = ""
    query: str = ""
    source_type: str = "其他"
    width: int = 0
    height: int = 0
    dhash: str = ""
    scores: dict[str, int] = {}
    score: float | None = None
    status: str = "pending"  # pending | scored | unscored


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    image_tokens: int = 0
    images: int = 0
    usd: float = 0.0


class RunState(BaseModel):
    id: str
    created: str
    status: str = "created"  # created | analyzed | searching | downloading | scoring | done | error
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    error: str = ""
    error_hint: str = ""
    warnings: list[str] = []
    search_count: int = 0
    filtered_out: int = 0  # 非短租 / 酒店网站、被过滤的搜索结果数
    search_errors: list[str] = []
    items: list[WallItem] = []
    usage: Usage = Usage()
