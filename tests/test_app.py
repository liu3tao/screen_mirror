"""接口端到端：伪造 Gemini 与图片搜索，后台任务同步执行。"""

import httpx
import pytest
from fastapi.testclient import TestClient

from dreamview.app import create_app
from dreamview.gemini import Gemini
from dreamview.llm import ModelError as GeminiError
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

from .conftest import make_image

ELEMENTS = [Element(name="海景", description="看到海", weight=1.0), Element(name="高层", description="俯瞰", weight=0.5)]


class FakeGemini(Gemini):
    def __init__(self, settings):
        super().__init__(settings, client=object())
        self.score_calls = 0
        self.fail_analyze = None

    def analyze_scene(self, meter, image, mime="image/jpeg"):
        meter.before_call()
        meter.add(1000, 100, image_tokens=1120, n_images=1, media_res="high")
        if self.fail_analyze:
            raise self.fail_analyze
        return SceneAnalysis(summary="海边高层", elements=ELEMENTS)

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
        meter.add(3000, 400, image_tokens=280 * len(thumbs), n_images=len(thumbs), media_res="low")
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

    app = create_app(tmp_settings, model=gem, deps_factory=deps_factory, run_jobs_inline=True)
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
    assert "keywords" not in d["scene"]
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
    assert r.status_code == 202 and r.json()["queries"] == 8
    # 默认 SITE_FILTER=rental：每个地区 4 个租住网站，关键词轮换
    assert engine.queries == [
        ("熱海 オーシャンビュー site:airbnb.jp", "jp-jp"),
        ("熱海 民泊 site:booking.com", "jp-jp"),
        ("熱海 オーシャンビュー site:jalan.net", "jp-jp"),
        ("熱海 民泊 site:travel.rakuten.co.jp", "jp-jp"),
        ("墾丁 海景民宿 site:booking.com", "tw-tzh"),
        ("墾丁 海景民宿 site:agoda.com", "tw-tzh"),
        ("墾丁 海景民宿 site:airbnb.com.tw", "tw-tzh"),
        ("墾丁 海景民宿 site:trip.com", "tw-tzh"),
    ]

    d = client.get(f"/api/runs/{run_id}").json()
    st = d["state"]
    assert st["status"] == "done", st["error"]
    assert len(st["items"]) == 12  # 截断到 max_images
    assert all(it["status"] == "scored" for it in st["items"])
    scores = [it["score"] for it in st["items"]]
    assert scores == sorted(scores, reverse=True)
    assert {it["region_label"] for it in st["items"]} == {"热海", "垦丁"}
    assert all(it["source_type"] == "房源" for it in st["items"])
    assert st["filtered_out"] == 8 * 2  # 每次查询的 dup.test、broken.test 两条非租住网站结果被过滤
    assert gem.score_calls == 2  # 12 张 / 8 张一批
    assert st["usage"]["calls"] == 1 + 1 + 2
    assert st["warnings"] == []
    thumb = st["items"][0]["thumb_file"]
    assert client.get(f"/files/{run_id}/{thumb}").status_code == 200

    # run 历史
    runs = client.get("/api/runs").json()
    assert runs[0]["id"] == run_id and runs[0]["status"] == "done"


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


def test_full_flow_with_ollama_backend(tmp_settings):
    """真实 Ollama 适配器 + 伪造的 Ollama HTTP 服务，走完整流程。"""
    import json

    from dreamview.ollama import Ollama

    def ollama_handler(req: httpx.Request):
        body = json.loads(req.content)
        title = body["format"].get("title")
        images = body["messages"][0]["images"]
        if title == "SceneAnalysis":
            out = {"summary": "海边", "elements": [{"name": "海景", "description": "d", "weight": 1.0}]}
        elif title == "RegionsOut":
            out = {"regions": [{"city": "热海", "district": "", "reason": "r", "months": "", "keywords": ["熱海 民泊"], "search_region": "jp-jp"}]}
        else:
            out = {"results": [{"index": i, "scores": [{"element": "海景", "score": 7}]} for i in range(len(images))]}
        return httpx.Response(200, json={"message": {"content": json.dumps(out, ensure_ascii=False)}, "prompt_eval_count": 10, "eval_count": 5})

    tmp_settings.model_backend = "ollama"
    tmp_settings.ollama_model = "qwen2.5vl:7b"
    tmp_settings.max_images = 5
    model = Ollama(tmp_settings, client=httpx.Client(transport=httpx.MockTransport(ollama_handler), trust_env=False))
    engine, holder = FakeEngine(), {}

    def deps_factory():
        http = httpx.Client(transport=thumb_transport(), trust_env=False)
        return JobDeps(tmp_settings, holder["app"].state.store, model, engine, http, Throttle(0))

    app = create_app(tmp_settings, model=model, deps_factory=deps_factory, run_jobs_inline=True)
    holder["app"] = app
    client = TestClient(app)
    cfg = client.get("/api/config").json()
    assert (cfg["backend"], cfg["model"], cfg["grounded"]) == ("ollama", "qwen2.5vl:7b", False)

    run_id = upload(client)["state"]["id"]
    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": ["日本"]}).json()
    assert regions["grounded"] is False
    regions["regions"][0]["selected"] = True
    client.put(f"/api/runs/{run_id}/regions", json={"regions": regions["regions"]})
    assert client.post(f"/api/runs/{run_id}/search").status_code == 202
    st = client.get(f"/api/runs/{run_id}").json()["state"]
    assert st["status"] == "done", st["error"]
    assert len(st["items"]) == 5 and all(it["score"] == 7.0 for it in st["items"])
    assert st["usage"]["usd"] == 0 and st["usage"]["calls"] == 3


def test_old_scene_with_generic_keywords_still_loads(env):
    """v0.11 之前保存的 scene.json 带 keywords / use_generic_keywords，应被忽略。"""
    client, _, _ = env
    run_id = upload(client)["state"]["id"]
    store = client.app.state.store
    p = store.dir(run_id) / "scene.json"
    import json

    old = json.loads(p.read_text(encoding="utf-8"))
    old.update(keywords=["海景房"], use_generic_keywords=True)
    p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    d = client.get(f"/api/runs/{run_id}").json()
    assert d["scene"]["summary"] == "海边高层" and "use_generic_keywords" not in d["scene"]
    # 没勾地区仍不能搜（不会退回到通用关键词）
    assert client.post(f"/api/runs/{run_id}/search").status_code == 400


def test_stop_without_running_job_is_409(env):
    client, _, _ = env
    run_id = upload(client)["state"]["id"]
    assert client.post(f"/api/runs/{run_id}/stop").status_code == 409


def test_stop_during_scoring_keeps_scored_items(tmp_settings):
    """打分到第 2 批时用户点停止：第 1 批保留分数，其余标 unscored，状态 stopped。"""
    import threading

    tmp_settings.max_images = 20
    gem = FakeGemini(tmp_settings)
    holder = {}
    cancel = threading.Event()
    orig = gem.score_batch

    def score_then_stop(meter, elements, thumbs):
        out = orig(meter, elements, thumbs)
        cancel.set()  # 第一批完成后「点停止」
        return out

    gem.score_batch = score_then_stop

    def deps_factory():
        http = httpx.Client(transport=thumb_transport(), trust_env=False)
        return JobDeps(tmp_settings, holder["app"].state.store, gem, FakeEngine(), http, Throttle(0), cancel)

    app = create_app(tmp_settings, model=gem, deps_factory=deps_factory, run_jobs_inline=True)
    holder["app"] = app
    client = TestClient(app)
    run_id = upload(client)["state"]["id"]
    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": []}).json()["regions"]
    for r in regions:
        r["selected"] = True
    client.put(f"/api/runs/{run_id}/regions", json={"regions": regions})
    assert client.post(f"/api/runs/{run_id}/search").status_code == 202
    st = client.get(f"/api/runs/{run_id}").json()["state"]
    assert st["status"] == "stopped" and st["message"]
    scored = [it for it in st["items"] if it["status"] == "scored"]
    assert len(scored) == 8 and len(st["items"]) > 8
    assert all(it["status"] == "unscored" for it in st["items"][8:])
    assert gem.score_calls == 1


def test_stop_endpoint_cancels_running_job(tmp_settings):
    """真实线程：搜索阶段点停止。"""
    import threading
    import time

    started, release = threading.Event(), threading.Event()

    class SlowEngine(FakeEngine):
        def search(self, query, region, max_results):
            started.set()
            release.wait(5)
            return super().search(query, region, max_results)

    gem, holder = FakeGemini(tmp_settings), {}

    def deps_factory():
        http = httpx.Client(transport=thumb_transport(), trust_env=False)
        return JobDeps(tmp_settings, holder["app"].state.store, gem, SlowEngine(), http, Throttle(0))

    app = create_app(tmp_settings, model=gem, deps_factory=deps_factory)
    holder["app"] = app
    client = TestClient(app)
    run_id = upload(client)["state"]["id"]
    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": []}).json()["regions"]
    for r in regions:
        r["selected"] = True
    client.put(f"/api/runs/{run_id}/regions", json={"regions": regions})
    assert client.post(f"/api/runs/{run_id}/search").status_code == 202
    assert started.wait(5)
    assert client.post(f"/api/runs/{run_id}/stop").status_code == 202
    release.set()
    for _ in range(50):
        d = client.get(f"/api/runs/{run_id}").json()
        if not d["running"]:
            break
        time.sleep(0.1)
    assert d["state"]["status"] == "stopped" and not d["running"]
    assert gem.score_calls == 0


def test_site_filter_off_keeps_all_sources(tmp_settings):
    tmp_settings.site_filter = "off"
    tmp_settings.max_images = 50

    class MixedEngine(FakeEngine):
        def search(self, query, region, max_results):
            hits = super().search(query, region, max_results)
            n = len(self.queries)
            return hits + [ImageHit(page_url=f"https://example.com/{n}", thumb_url=f"https://thumbs.test/9/{n}.png")]

    gem, engine, holder = FakeGemini(tmp_settings), MixedEngine(), {}

    def deps_factory():
        http = httpx.Client(transport=thumb_transport(), trust_env=False)
        return JobDeps(tmp_settings, holder["app"].state.store, gem, engine, http, Throttle(0))

    app = create_app(tmp_settings, model=gem, deps_factory=deps_factory, run_jobs_inline=True)
    holder["app"] = app
    client = TestClient(app)
    run_id = upload(client)["state"]["id"]
    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": []}).json()["regions"]
    regions[0]["selected"] = True
    client.put(f"/api/runs/{run_id}/regions", json={"regions": regions})
    assert client.post(f"/api/runs/{run_id}/search").json()["queries"] == 2
    assert engine.queries == [("熱海 オーシャンビュー", "jp-jp"), ("熱海 民泊", "jp-jp")]
    st = client.get(f"/api/runs/{run_id}").json()["state"]
    assert st["filtered_out"] == 0
    assert "其他" in {it["source_type"] for it in st["items"]}  # example.com 的结果被保留


def test_all_results_filtered_gives_clear_error(tmp_settings):
    class NonRentalEngine(FakeEngine):
        def search(self, query, region, max_results):
            return [ImageHit(page_url=f"https://suumo.jp/{k}", thumb_url=f"https://thumbs.test/1/{k}.png") for k in range(5)]

    gem, holder = FakeGemini(tmp_settings), {}

    def deps_factory():
        http = httpx.Client(transport=thumb_transport(), trust_env=False)
        return JobDeps(tmp_settings, holder["app"].state.store, gem, NonRentalEngine(), http, Throttle(0))

    app = create_app(tmp_settings, model=gem, deps_factory=deps_factory, run_jobs_inline=True)
    holder["app"] = app
    client = TestClient(app)
    run_id = upload(client)["state"]["id"]
    regions = client.post(f"/api/runs/{run_id}/regions", json={"scopes": []}).json()["regions"]
    regions[0]["selected"] = True
    client.put(f"/api/runs/{run_id}/regions", json={"regions": regions})
    client.post(f"/api/runs/{run_id}/search")
    st = client.get(f"/api/runs/{run_id}").json()["state"]
    assert st["status"] == "error" and "SITE_FILTER=off" in st["error"] and st["filtered_out"] == 20
