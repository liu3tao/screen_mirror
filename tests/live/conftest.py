"""联网集成测试的公共工具。

运行：uv run pytest -m live            （只跑联网测试）
      LIVE_GEMINI=1 uv run pytest -m live （额外跑真实模型，约 3 次调用，< 0.01 USD）

跳过而不是失败的情况：主机不可达（网络策略）、缺 Key、DuckDuckGo 限流 / 拦截本 IP。
其他错误（如库升级后请求格式坏了）一律失败。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import httpx
import pytest

from dreamview.config import Settings
from dreamview.search import BraveSearch, DdgsSearch, Throttle

# 固定查询：(关键词, ddgs 区域代码)，覆盖日文 / 简体中文 / 意大利文
QUERIES = [
    ("琵琶湖 湖畔 民泊", "jp-jp"),
    ("厦门 海景民宿", "cn-zh"),
    ("Amalfi appartamento vista mare", "it-it"),
]

# 被拦截 / 限流的特征：DuckDuckGo 拦截 IP 时 ddgs 报「No results found.」或取不到 vqd（返回了验证页）；
# Brave 超频为 429。不按裸状态码匹配：错误里的 URL 编码查询可能恰好含这些数字。
_BLOCKED = re.compile(r"No results found|Could not extract vqd|ratelimit|timed out|429 Too Many Requests", re.I)
_reachable: dict[str, bool] = {}


def require_host(url: str) -> None:
    """主机不可达（多为云端网络策略拦截）则跳过。只要对方有 HTTP 响应就算可达。"""
    host = urlparse(url).hostname or url
    if host not in _reachable:
        try:
            httpx.get(f"https://{host}/", timeout=8, follow_redirects=False)
            _reachable[host] = True
        except httpx.HTTPError:
            _reachable[host] = False
    if not _reachable[host]:
        pytest.skip(f"{host} 不可达（网络策略未放行？）")


def env_settings(tmp_path) -> Settings:
    s = Settings.from_env()  # 读本机 .env 或云端环境变量
    s.runs_dir = tmp_path / "runs"
    s.search_interval_s = 1.5
    return s


_ddgs_throttle = Throttle(2.0)
_brave_throttle = Throttle(1.1)  # Brave 免费档 1 次/秒


def search_live(engine_name: str, query: str, region: str, n: int = 30):
    """调用真实引擎；不可达 / 缺 Key / 被限流时跳过。"""
    if engine_name in ("bing", "duckduckgo"):
        require_host("https://www.bing.com" if engine_name == "bing" else "https://duckduckgo.com")
        engine, throttle = DdgsSearch(backend=engine_name), _ddgs_throttle
    else:
        key = os.environ.get("BRAVE_API_KEY") or Settings.from_env().brave_api_key
        if not key:
            pytest.skip("未设置 BRAVE_API_KEY")
        require_host(BraveSearch.URL)
        engine, throttle = BraveSearch(key), _brave_throttle
    throttle.wait()
    try:
        return engine.search(query, region, n)
    except Exception as e:  # noqa: BLE001 - 区分「被拦截」与「真坏了」
        if _BLOCKED.search(str(e)):
            pytest.skip(f"{engine_name} 被限流或拦截：{str(e)[:120]}")
        raise


ENGINES = ["bing", "duckduckgo", "brave"]


@pytest.fixture(params=ENGINES)
def engine_name(request):
    return request.param
