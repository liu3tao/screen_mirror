"""真实搜图跑完整流程。默认用伪造模型（不花钱）；LIVE_GEMINI=1 时用 .env 中配置的真实后端。"""

import os

import pytest
from fastapi.testclient import TestClient

from dreamview import images
from dreamview.app import create_app
from dreamview.llm import make_backend
from dreamview.pipeline import JobDeps
from dreamview.search import BraveSearch, DdgsSearch, Throttle

from ..test_app import FakeGemini
from .conftest import QUERIES, env_settings, require_host, search_live

pytestmark = pytest.mark.live


def _engine(name, settings):
    if name in ("bing", "duckduckgo"):
        return DdgsSearch(backend=name)
    return BraveSearch(settings.brave_api_key or os.environ.get("BRAVE_API_KEY", ""))


def _run_flow(client, regions_payload):
    run_id = client.post("/api/runs", files={"file": ("ref.jpg", regions_payload["ref"], "image/jpeg")}).json()
    assert "state" in run_id, run_id
    run_id = run_id["state"]["id"]
    client.put(f"/api/runs/{run_id}/regions", json={"regions": regions_payload["regions"]})
    assert client.post(f"/api/runs/{run_id}/search").status_code == 202
    return client.get(f"/api/runs/{run_id}").json()["state"]


def _ref_image(engine_name):
    """用一张真实搜索到的缩略图当参考图。"""
    query, region = QUERIES[0]
    hits = search_live(engine_name, query, region, n=10)
    require_host(hits[0].thumb_url)
    with images.make_client() as c:
        for h in hits:
            try:
                return images.normalize_thumb(images.download(c, h.thumb_url), 1024)[0]
            except Exception:  # noqa: BLE001
                continue
    pytest.skip("参考图下载失败")


def test_pipeline_real_search_fake_model(engine_name, tmp_path):
    ref = _ref_image(engine_name)  # 同时确认引擎可用，否则跳过
    s = env_settings(tmp_path)
    s.max_images = 10
    model, holder = FakeGemini(s), {}

    def deps_factory():
        return JobDeps(s, holder["app"].state.store, model, _engine(engine_name, s), images.make_client(), Throttle(2.0))

    app = create_app(s, model=model, deps_factory=deps_factory, run_jobs_inline=True)
    holder["app"] = app
    query, region = QUERIES[1]
    st = _run_flow(
        TestClient(app),
        {"ref": ref, "regions": [{"id": "m1", "city": "厦门", "keywords": [query], "search_region": region, "selected": True, "manual": True}]},
    )
    assert st["status"] == "done", (st["error"], st["search_errors"])
    assert len(st["items"]) >= 5 and all(it["status"] == "scored" for it in st["items"])
    assert all(it["region_label"] == "厦门" for it in st["items"])


@pytest.mark.skipif(os.environ.get("LIVE_GEMINI") != "1", reason="真实模型调用需显式 LIVE_GEMINI=1")
def test_pipeline_real_model(engine_name, tmp_path):
    """真实后端：解析 1 次 + 找地区 1 次 + 打分 1 批（8 张）。核对 token 校验无警告、思考档位可用。"""
    s = env_settings(tmp_path)
    s.max_images = 8
    model = make_backend(s)
    if model.name in ("gemini", "vertex"):
        require_host("https://generativelanguage.googleapis.com" if model.name == "gemini" else "https://aiplatform.googleapis.com")
    ref = _ref_image(engine_name)
    holder = {}

    def deps_factory():
        return JobDeps(s, holder["app"].state.store, model, _engine(engine_name, s), images.make_client(), Throttle(2.0))

    app = create_app(s, model=model, deps_factory=deps_factory, run_jobs_inline=True)
    holder["app"] = app
    client = TestClient(app)

    r = client.post("/api/runs", files={"file": ("ref.jpg", ref, "image/jpeg")})
    assert r.status_code == 200, r.json()
    d = r.json()
    run_id = d["state"]["id"]
    assert d["scene"]["elements"], "场景解析没有核心要素"

    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": ["日本"]})
    assert regions.status_code == 200, regions.json()
    regions = regions.json()
    assert regions["regions"], f"找地区无结果：{regions['parse_error']}"
    if model.grounded:
        assert regions["citations"] or regions["search_suggestions_html"], "grounding 没有返回引用"
    regions["regions"][0]["selected"] = True
    regions["regions"][0]["keywords"] = regions["regions"][0]["keywords"][:1]
    client.put(f"/api/runs/{run_id}/regions", json={"regions": regions["regions"][:1]})

    assert client.post(f"/api/runs/{run_id}/search").status_code == 202
    st = client.get(f"/api/runs/{run_id}").json()["state"]
    assert st["status"] == "done", (st["error"], st["error_hint"])
    assert any(it["status"] == "scored" for it in st["items"])
    assert st["warnings"] == [], st["warnings"]  # 含单图 token 偏差警告（media_resolution 未生效时出现）
    print(f"\n[{model.name}] usage: {st['usage']}")
