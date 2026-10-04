"""配置：.env 可覆盖的运行参数 + 不常改的业务常量。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
STATIC_DIR = PACKAGE_DIR / "static"

# ---- 业务常量（H7 确认） ----

# 搜索范围标签（FR-3）。自由输入另加。
SCOPE_TAGS = ["全球", "中国大陆", "香港", "台湾", "日本", "韩国", "东南亚", "地中海", "欧洲", "北美", "南美", "大洋洲"]

# 硬条件（M2 体检用）
HARD_CONSTRAINTS = {
    "whole_unit": True,  # 整租
    "guests": 2,
    "max_usd_per_month": 10_000,
    "min_nights": 28,
}

# 来源类型规则：域名后缀 → 标签（3.8）
SOURCE_RULES: dict[str, str] = {
    "airbnb.": "房源",
    "booking.com": "房源",
    "agoda.com": "房源",
    "ctrip.com": "房源",
    "trip.com": "房源",
    "tujia.com": "房源",
    "vrbo.com": "房源",
    "expedia.": "房源",
    "xiaohongshu.com": "种草帖",
    "mafengwo.cn": "种草帖",
    "dianping.com": "种草帖",
    "ctrip.com/travels": "种草帖",
}

# Gemini 3 每图 token 数（按 media_resolution 档位）
IMAGE_TOKENS = {"low": 280, "medium": 560, "high": 1120, "ultra_high": 2240}

# 单价（USD / 百万 token）
PRICE_INPUT_PER_M = 0.30
PRICE_OUTPUT_PER_M = 2.50

TOKEN_DEVIATION_WARN = 0.30


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass
class Settings:
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_model_strong: str = ""
    media_res_analyze: str = "high"
    media_res_score: str = "low"
    media_res_audit: str = "medium"
    thinking_level: str = "minimal"
    thinking_level_regions: str = "low"
    image_search: str = "ddgs"  # ddgs | brave
    brave_api_key: str = ""
    search_interval_s: float = 1.5
    search_per_query: int = 100
    max_images: int = 600
    score_batch_size: int = 8
    max_model_calls_per_run: int = 300
    thumb_max_side: int = 512
    dhash_max_distance: int = 6
    download_workers: int = 8
    runs_dir: Path = field(default_factory=lambda: PROJECT_DIR / "runs")
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def strong_model(self) -> str:
        return self.gemini_model_strong or self.gemini_model

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_DIR / ".env")
        return cls(
            gemini_api_key=_env("GEMINI_API_KEY", ""),
            gemini_model=_env("GEMINI_MODEL", cls.gemini_model),
            gemini_model_strong=_env("GEMINI_MODEL_STRONG", ""),
            media_res_analyze=_env("MEDIA_RES_ANALYZE", cls.media_res_analyze),
            media_res_score=_env("MEDIA_RES_SCORE", cls.media_res_score),
            media_res_audit=_env("MEDIA_RES_AUDIT", cls.media_res_audit),
            thinking_level=_env("THINKING_LEVEL", cls.thinking_level),
            thinking_level_regions=_env("THINKING_LEVEL_REGIONS", cls.thinking_level_regions),
            image_search=_env("IMAGE_SEARCH", cls.image_search),
            brave_api_key=_env("BRAVE_API_KEY", ""),
            search_interval_s=float(_env("SEARCH_INTERVAL_S", str(cls.search_interval_s))),
            max_images=int(_env("MAX_IMAGES", str(cls.max_images))),
            max_model_calls_per_run=int(_env("MAX_MODEL_CALLS_PER_RUN", str(cls.max_model_calls_per_run))),
            runs_dir=Path(_env("RUNS_DIR", str(PROJECT_DIR / "runs"))),
            port=int(_env("PORT", str(cls.port))),
        )


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
