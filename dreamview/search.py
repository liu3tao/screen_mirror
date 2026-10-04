"""图片搜索：ddgs（默认）与 Brave 两个实现，同一签名；来源类型判定。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from .config import SOURCE_RULES, Settings


@dataclass
class ImageHit:
    page_url: str
    thumb_url: str
    image_url: str = ""
    title: str = ""
    width: int = 0
    height: int = 0


class ImageSearch(Protocol):
    name: str

    def search(self, query: str, region: str, max_results: int) -> list[ImageHit]: ...


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class DdgsSearch:
    name = "DuckDuckGo"

    def __init__(self, ddgs=None):
        if ddgs is None:
            from ddgs import DDGS

            ddgs = DDGS(timeout=15)
        self._ddgs = ddgs

    def search(self, query: str, region: str, max_results: int) -> list[ImageHit]:
        rows = self._ddgs.images(
            query, region=region or "wt-wt", safesearch="moderate", max_results=max_results, backend="duckduckgo"
        )
        hits = []
        for r in rows:
            if not r.get("thumbnail") or not r.get("url"):
                continue
            hits.append(
                ImageHit(
                    page_url=r["url"],
                    thumb_url=r["thumbnail"],
                    image_url=r.get("image", ""),
                    title=r.get("title", ""),
                    width=_int(r.get("width")),
                    height=_int(r.get("height")),
                )
            )
        return hits


# ddgs 区域代码的语言部分 → Brave search_lang
_BRAVE_LANG = {
    "zh": "zh-hans",
    "tzh": "zh-hant",
    "jp": "jp",
    "ja": "jp",
    "ko": "ko",
    "kr": "ko",
    "it": "it",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "pt": "pt-pt",
    "en": "en",
    "th": "th",
    "vi": "vi",
    "id": "id",
    "el": "el",
    "tr": "tr",
}


def brave_params(region: str) -> dict[str, str]:
    """ddgs 区域代码（如 jp-jp、cn-zh、tw-tzh）→ Brave country / search_lang。"""
    if not region or region == "wt-wt" or "-" not in region:
        return {}
    country, lang = region.lower().split("-", 1)
    params = {"country": "ALL" if country == "wt" else country.upper()}
    if lang in _BRAVE_LANG:
        params["search_lang"] = _BRAVE_LANG[lang]
    elif country in _BRAVE_LANG:
        params["search_lang"] = _BRAVE_LANG[country]
    return params


class BraveSearch:
    name = "Brave"
    URL = "https://api.search.brave.com/res/v1/images/search"

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("IMAGE_SEARCH=brave 需要 BRAVE_API_KEY")
        self._key = api_key
        self._client = client or httpx.Client(timeout=20)

    def search(self, query: str, region: str, max_results: int) -> list[ImageHit]:
        params = {"q": query, "count": str(min(max_results, 200)), "safesearch": "strict"}
        params.update(brave_params(region))
        r = self._client.get(
            self.URL, params=params, headers={"X-Subscription-Token": self._key, "Accept": "application/json"}
        )
        r.raise_for_status()
        hits = []
        for row in r.json().get("results", []):
            thumb = (row.get("thumbnail") or {}).get("src", "")
            page = row.get("url", "")
            if not thumb or not page:
                continue
            props = row.get("properties") or {}
            hits.append(
                ImageHit(
                    page_url=page,
                    thumb_url=thumb,
                    image_url=props.get("url", ""),
                    title=row.get("title", ""),
                    width=_int(props.get("width")),
                    height=_int(props.get("height")),
                )
            )
        return hits


def make_search(settings: Settings) -> ImageSearch:
    if settings.image_search == "brave":
        return BraveSearch(settings.brave_api_key)
    return DdgsSearch()


class Throttle:
    """请求间隔（ddgs 1–2 s；Brave 免费档 1 次/秒）。"""

    def __init__(self, interval_s: float, sleep=time.sleep, clock=time.monotonic):
        self.interval = interval_s
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def search_with_retry(engine: ImageSearch, throttle: Throttle, query: str, region: str, max_results: int) -> list[ImageHit]:
    """失败重试 1 次；仍失败抛出，由调用方跳过该查询。"""
    try:
        throttle.wait()
        return engine.search(query, region, max_results)
    except Exception:
        throttle.wait()
        return engine.search(query, region, max_results)


_RULES_SORTED = sorted(SOURCE_RULES.items(), key=lambda kv: -len(kv[0]))


def _host_matches(host: str, domain: str) -> bool:
    if domain.endswith("."):
        # 「airbnb.」匹配 airbnb.com / airbnb.co.jp / zh.airbnb.com
        return f".{domain}" in f".{host}"
    return host == domain or host.endswith("." + domain)


def classify_source(page_url: str) -> str:
    """按「域名[/路径前缀]」规则判定来源类型，最长规则优先；未命中为「其他」。"""
    p = urlparse(page_url)
    host = (p.hostname or "").lower()
    path = p.path.lower()
    for rule, label in _RULES_SORTED:
        domain, _, prefix = rule.partition("/")
        if _host_matches(host, domain) and (not prefix or path.startswith("/" + prefix)):
            return label
    return "其他"
