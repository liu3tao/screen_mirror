"""接口端到端：伪造 Gemini 与图片搜索，后台任务同步执行。"""

import httpx
import pytest
from fastapi.testclient import TestClient

from dreamview.app import create_app
from dreamview.gemini import Gemini, GeminiError
from dreamview.pipeline import JobDeps
from dreamview.schemas import (
    Element,
    ElementScore,
    ImageScore,
    Region,
    RegionsState,
    SceneAnalysis,
    ScoreBatchOut,
)
from dreamview.search import ImageHit, Throttle

from .conftest import fake_response, fake_usage, make_image

ELEMENTS = [Element(name="海景", description="看到海", weight=1.0), Element(name="高层", description="俯瞰", weight=0.5)]


class FakeGemini(Gemini):
    def __init__(self, settings):
        super().__init__(settings, client=object())
        self.score_calls = 0
        self.fail_analyze = None

    def analyze_scene(self, meter, image, mime="image/jpeg"):
        meter.before_call()
        meter.record(fake_response(usage=fake_usage(image_tokens=1120)), n_images=1, media_res="high")
        if self.fail_analyze:
            raise self.fail_analyze
        return SceneAnalysis(summary="海边高层", elements=ELEMENTS, keywords=["海景房 落地窗"])

    def find_regions(self, meter, scene, scopes):
        meter.before_call()
        return RegionsState(
            scopes=list(scopes),
            regions=[
                Region(id="r1", city="热海", keywords=["熱海 オーシャンビュー", "熱海 民泊"], search_region="jp-jp"),
                Region(id="r2", city="厦门", keywords=["厦门 海景民宿"], search_region="cn-zh"),
            ],
            search_suggestions_html="<div>chips</div>",
        )

    def score_batch(self, meter, elements, thumbs):
        meter.before_call()
        meter.record(fake_response(usage=fake_usage(image_tokens=280 * len(thumbs))), n_images=len(thumbs), media_res="low")
        self.score_calls += 1
        base = self.score_calls
        return ScoreBatchOut(
            results=[
                ImageScore(index=i, scores=[ElementScore(element=e.name, score=(base + i) % 10 + 1) for e in elements])
                for i in range(len(thumbs))
            ]
        )


class FakeEngine:
    name = "Fake"

    def __init__(self):
        self.queries = []

    def search(self, query, region, max_results):
        self.queries.append((query, region))
        n = len(self.queries)
        hits = [
            ImageHit(page_url=f"https://www.airbnb.com/rooms/{n}{k}", thumb_url=f"https://thumbs.test/{n}/{k}.png", width=800, height=600)
            for k in range(5)
        ]
        hits.append(ImageHit(page_url="https://dup.test/p", thumb_url="https://thumbs.test/1/0.png"))  # URL 重复
        hits.append(ImageHit(page_url="https://broken.test/p", thumb_url="https://thumbs.test/broken.png"))
        return hits


def thumb_transport():
    def handler(req: httpx.Request):
        if "broken" in req.url.path:
            return httpx.Response(404)
        n, k = req.url.path.strip("/").split("/")
        seed = int(n) * 10 + int(k.split(".")[0])
        return httpx.Response(200, content=make_image(seed), headers={"content-type": "image/png"})

    return httpx.MockTransport(handler)


@pytest.fixture
def env(tmp_settings):
    tmp_settings.max_images = 12
    gem = FakeGemini(tmp_settings)
    engine = FakeEngine()
    app_holder = {}

    def deps_factory():
        http = httpx.Client(transport=thumb_transport(), trust_env=False)
        return JobDeps(tmp_settings, app_holder["app"].state.store, gem, engine, http, Throttle(0))

    app = create_app(tmp_settings, gemini=gem, deps_factory=deps_factory, run_jobs_inline=True)
    app_holder["app"] = app
    return TestClient(app), gem, engine


def upload(client):
    r = client.post("/api/runs", files={"file": ("ref.png", make_image(99), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()


def test_index_and_config(env):
    client, _, _ = env
    assert "窗景找房" in client.get("/").text
    assert client.get("/static/alpine.min.js").status_code == 200
    cfg = client.get("/api/config").json()
    assert "全球" in cfg["scope_tags"] and cfg["max_images"] == 12


def test_full_flow(env):
    client, gem, engine = env
    d = upload(client)
    run_id = d["state"]["id"]
    assert d["state"]["status"] == "analyzed" and d["scene"]["summary"] == "海边高层"
    assert d["scene"]["use_generic_keywords"] is False
    assert client.get(f"/files/{run_id}/ref.jpg").headers["content-type"] == "image/jpeg"

    # 编辑场景
    scene = d["scene"]
    scene["elements"][1]["weight"] = 0.3
    assert client.put(f"/api/runs/{run_id}/scene", json=scene).status_code == 200

    # 未勾选任何地区 → 不能搜
    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": ["日本", "中国大陆"]}).json()
    assert len(regions["regions"]) == 2 and regions["search_suggestions_html"]
    assert client.post(f"/api/runs/{run_id}/search").status_code == 400

    # 勾选 + 手动加一个
    regions["regions"][0]["selected"] = True
    regions["regions"].append(
        {"id": "mx1", "city": "垦丁", "keywords": ["墾丁 海景民宿"], "search_region": "tw-tzh", "selected": True, "manual": True}
    )
    assert client.put(f"/api/runs/{run_id}/regions", json={"regions": regions["regions"]}).status_code == 200

    r = client.post(f"/api/runs/{run_id}/search")
    assert r.status_code == 202 and r.json()["queries"] == 3
    assert engine.queries == [("熱海 オーシャンビュー", "jp-jp"), ("熱海 民泊", "jp-jp"), ("墾丁 海景民宿", "tw-tzh")]

    d = client.get(f"/api/runs/{run_id}").json()
    st = d["state"]
    assert st["status"] == "done", st["error"]
    assert len(st["items"]) == 12  # 截断到 max_images
    assert all(it["status"] == "scored" for it in st["items"])
    scores = [it["score"] for it in st["items"]]
    assert scores == sorted(scores, reverse=True)
    assert {it["region_label"] for it in st["items"]} == {"热海", "垦丁"}
    assert all(it["source_type"] == "房源" for it in st["items"])
    assert gem.score_calls == 2  # 12 张 / 8 张一批
    assert st["usage"]["calls"] == 1 + 1 + 2
    assert st["warnings"] == []
    thumb = st["items"][0]["thumb_file"]
    assert client.get(f"/files/{run_id}/{thumb}").status_code == 200

    # run 历史
    runs = client.get("/api/runs").json()
    assert runs[0]["id"] == run_id and runs[0]["status"] == "done"


def test_generic_keywords_toggle(env):
    client, _, engine = env
    d = upload(client)
    run_id = d["state"]["id"]
    scene = d["scene"]
    scene["use_generic_keywords"] = True
    client.put(f"/api/runs/{run_id}/scene", json=scene)
    assert client.post(f"/api/runs/{run_id}/search").status_code == 202
    assert engine.queries == [("海景房 落地窗", "wt-wt")]
    items = client.get(f"/api/runs/{run_id}").json()["state"]["items"]
    assert items and {it["region_label"] for it in items} == {"地区未知"}


def test_analyze_error_returns_hint(env):
    client, gem, _ = env
    gem.fail_analyze = GeminiError("Gemini API 错误 400：billing", "关联账单", code=400)
    r = client.post("/api/runs", files={"file": ("ref.png", make_image(1), "image/png")})
    assert r.status_code == 502
    body = r.json()
    assert body["hint"] == "关联账单" and body["run_id"]
    assert client.get(f"/api/runs/{body['run_id']}").json()["state"]["status"] == "error"


def test_rejects_bad_upload_and_bad_paths(env):
    client, _, _ = env
    assert client.post("/api/runs", files={"file": ("x.png", b"not an image", "image/png")}).status_code == 400
    assert client.get("/api/runs/../../etc").status_code == 404
    run_id = upload(client)["state"]["id"]
    assert client.get(f"/files/{run_id}/../results.json").status_code == 404
    assert client.get(f"/files/{run_id}/results.json").status_code == 404
    assert client.get("/api/runs/20990101-000000-abcd").status_code == 404


def test_scene_validation(env):
    client, _, _ = env
    d = upload(client)
    scene = d["scene"]
    scene["elements"][1]["name"] = scene["elements"][0]["name"]
    assert client.put(f"/api/runs/{d['state']['id']}/scene", json=scene).status_code == 400


def test_find_regions_keeps_manual_regions(env):
    client, _, _ = env
    run_id = upload(client)["state"]["id"]
    client.put(
        f"/api/runs/{run_id}/regions",
        json={"regions": [{"id": "mabc", "city": "垦丁", "keywords": ["k"], "manual": True}]},
    )
    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": []}).json()
    assert [r["id"] for r in regions["regions"]] == ["r1", "r2", "mabc"]
    assert regions["scopes"] == ["全球"]


def test_interrupted_job_is_reported(env):
    client, _, _ = env
    run_id = upload(client)["state"]["id"]
    store = client.app.state.store
    st = store.load_state(run_id)
    st.status = "scoring"
    store.save_state(st)
    assert client.get(f"/api/runs/{run_id}").json()["state"]["status"] == "error"
