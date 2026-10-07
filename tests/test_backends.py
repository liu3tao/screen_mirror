"""后端选择、Vertex、Ollama。全部离线。"""

import base64
import io
import json

import httpx
import pytest
from google.auth import exceptions as auth_errors
from PIL import Image

from dreamview.config import Settings
from dreamview.gemini import Gemini, error_hint
from dreamview.llm import Meter, ModelError, make_backend, regions_prompt
from dreamview.ollama import Ollama, inline_refs
from dreamview.schemas import Element, RegionsOut, Scene, SceneAnalysis, ScoreBatchOut

from .conftest import FakeClient, make_image

SCENE = Scene(summary="海边", elements=[Element(name="海景", description="看到海", weight=1.0)])


# ---- 选择与配置 ----


def test_make_backend_by_setting(tmp_settings):
    assert make_backend(tmp_settings).name == "gemini"
    tmp_settings.model_backend = "vertex"
    assert make_backend(tmp_settings).name == "vertex"
    tmp_settings.model_backend = "ollama"
    b = make_backend(tmp_settings)
    assert isinstance(b, Ollama) and b.grounded is False
    tmp_settings.model_backend = "openai"
    with pytest.raises(ValueError):
        make_backend(tmp_settings)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "Vertex")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("SCORE_BATCH_SIZE", "4")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5vl:7b")
    s = Settings.from_env()
    assert (s.model_backend, s.google_cloud_project, s.google_cloud_location, s.score_batch_size) == ("vertex", "my-proj", "global", 4)
    assert s.model_name == s.gemini_model
    s.model_backend = "ollama"
    assert s.model_name == "qwen2.5vl:7b"


def test_regions_prompt_mentions_verification_only_when_grounded():
    assert "Google 搜索核实" in regions_prompt(SCENE, ["日本"], grounded=True)
    p = regions_prompt(SCENE, ["日本"], grounded=False)
    assert "无法联网核实" in p and "Google" not in p and "日本" in p


def test_meter_unpriced_and_accumulates():
    m = Meter(10)
    m.add(1000, 100, priced=False)
    assert m.usage.usd == 0 and m.usage.input_tokens == 1000
    m.add(1_000_000, 0)
    m.add(0, 1_000_000)
    assert m.usage.usd == pytest.approx(0.30 + 2.50)


# ---- Vertex ----


def test_vertex_client_uses_project_and_location(tmp_settings, monkeypatch):
    from google import genai

    seen = {}
    monkeypatch.setattr(genai, "Client", lambda **kw: seen.update(kw) or "client")
    tmp_settings.model_backend = "vertex"
    tmp_settings.google_cloud_project = "my-proj"
    assert Gemini(tmp_settings).client == "client"
    assert seen == {"vertexai": True, "project": "my-proj", "location": "global"}


def test_vertex_requires_project(tmp_settings):
    tmp_settings.model_backend = "vertex"
    with pytest.raises(ModelError) as ei:
        Gemini(tmp_settings).analyze_scene(Meter(10), b"x")
    assert "GOOGLE_CLOUD_PROJECT" in str(ei.value)


def test_vertex_missing_credentials_gives_gcloud_hint(tmp_settings):
    tmp_settings.model_backend = "vertex"
    g = Gemini(tmp_settings, client=FakeClient([auth_errors.DefaultCredentialsError("not found")]))
    with pytest.raises(ModelError) as ei:
        g.analyze_scene(Meter(10), b"x")
    assert "gcloud auth application-default login" in ei.value.hint


@pytest.mark.parametrize(
    "code,msg,needle",
    [
        (403, "Permission denied on resource project", "Vertex AI API"),
        (404, "Publisher model not found", "GOOGLE_CLOUD_LOCATION"),
        (429, "Resource exhausted", "Vertex AI 配额"),
        (400, "billing account disabled", "试用额度"),
    ],
)
def test_vertex_error_hints(code, msg, needle):
    assert needle in error_hint(code, msg, "vertex")


# ---- Ollama ----


def test_inline_refs_removes_refs():
    for model in (RegionsOut, ScoreBatchOut, SceneAnalysis):
        s = json.dumps(inline_refs(model.model_json_schema()))
        assert "$ref" not in s and "$defs" not in s
    schema = inline_refs(ScoreBatchOut.model_json_schema())
    item = schema["properties"]["results"]["items"]
    assert item["properties"]["scores"]["items"]["properties"]["score"]["maximum"] == 10


class OllamaServer:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def handler(self, req: httpx.Request):
        self.requests.append(json.loads(req.content))
        status, body = self.replies.pop(0)
        return httpx.Response(status, json=body)


def ollama(tmp_settings, replies, model="qwen2.5vl:7b"):
    tmp_settings.model_backend = "ollama"
    tmp_settings.ollama_model = model
    srv = OllamaServer(replies)
    client = httpx.Client(transport=httpx.MockTransport(srv.handler), trust_env=False)
    return Ollama(tmp_settings, client=client), srv


def ok(content: str, p=500, e=50):
    return 200, {"message": {"role": "assistant", "content": content}, "prompt_eval_count": p, "eval_count": e, "done": True}


def test_ollama_analyze_request_shape_and_resize(tmp_settings):
    analysis = SceneAnalysis(summary="s", elements=SCENE.elements)
    o, srv = ollama(tmp_settings, [ok(analysis.model_dump_json())])
    m = Meter(10)
    assert o.analyze_scene(m, make_image(1, size=(3000, 2000))) == analysis
    body = srv.requests[0]
    assert body["model"] == "qwen2.5vl:7b" and body["stream"] is False and body["options"]["temperature"] == 0
    assert "$ref" not in json.dumps(body["format"])
    img = Image.open(io.BytesIO(base64.b64decode(body["messages"][0]["images"][0])))
    assert max(img.size) == 1024
    assert (m.usage.calls, m.usage.input_tokens, m.usage.usd) == (1, 500, 0)


def test_ollama_analyze_retries_then_fails(tmp_settings):
    o, _ = ollama(tmp_settings, [ok("不是 json"), ok("{}")])
    with pytest.raises(ModelError):
        o.analyze_scene(Meter(10), make_image(1))


def test_ollama_find_regions_not_grounded(tmp_settings):
    out = {"regions": [{"city": "热海", "district": "", "reason": "r", "months": "11 月", "keywords": ["熱海"], "search_region": "jp-jp"}]}
    o, srv = ollama(tmp_settings, [ok(json.dumps(out, ensure_ascii=False))])
    st = o.find_regions(Meter(10), SCENE, ["日本"])
    assert st.grounded is False and [r.id for r in st.regions] == ["r1"] and st.citations == []
    assert srv.requests[0]["messages"][0]["images"] == []
    assert "无法联网核实" in srv.requests[0]["messages"][0]["content"]


def test_ollama_find_regions_parse_error_keeps_raw(tmp_settings):
    o, _ = ollama(tmp_settings, [ok("一些文字")])
    st = o.find_regions(Meter(10), SCENE, [])
    assert st.regions == [] and st.parse_error and st.raw_text == "一些文字"


def test_ollama_score_batch_sends_all_images(tmp_settings):
    out = {"results": [{"index": i, "scores": [{"element": "海景", "score": 5}]} for i in range(3)]}
    o, srv = ollama(tmp_settings, [ok(json.dumps(out, ensure_ascii=False))])
    m = Meter(10)
    res = o.score_batch(m, SCENE.elements, [make_image(i) for i in range(3)])
    assert len(res.results) == 3 and len(srv.requests[0]["messages"][0]["images"]) == 3
    assert m.usage.images == 3


@pytest.mark.parametrize(
    "status,body,needle",
    [
        (404, {"error": "model 'qwen2.5vl:7b' not found"}, "ollama pull qwen2.5vl:7b"),
        (500, {"error": "this model does not support images"}, "SCORE_BATCH_SIZE=1"),
    ],
)
def test_ollama_http_errors(tmp_settings, status, body, needle):
    o, _ = ollama(tmp_settings, [(status, body)])
    with pytest.raises(ModelError) as ei:
        o.analyze_scene(Meter(10), make_image(1))
    assert needle in ei.value.hint and ei.value.code == status


def test_ollama_connect_error(tmp_settings):
    def refuse(req):
        raise httpx.ConnectError("refused", request=req)

    tmp_settings.ollama_model = "m"
    o = Ollama(tmp_settings, client=httpx.Client(transport=httpx.MockTransport(refuse), trust_env=False))
    with pytest.raises(ModelError) as ei:
        o.analyze_scene(Meter(10), make_image(1))
    assert "ollama serve" in ei.value.hint


def test_ollama_requires_model(tmp_settings):
    o, _ = ollama(tmp_settings, [], model="")
    with pytest.raises(ModelError) as ei:
        o.analyze_scene(Meter(10), make_image(1))
    assert "OLLAMA_MODEL" in str(ei.value)
