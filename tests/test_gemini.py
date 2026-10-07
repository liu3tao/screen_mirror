from types import SimpleNamespace

import pytest
from google.genai import errors, types

from dreamview.gemini import Gemini, error_hint, record_usage
from dreamview.llm import CallLimitExceeded, Meter, extract_json, parse_output
from dreamview.llm import ModelError as GeminiError
from dreamview.schemas import (
    Element,
    ElementScore,
    ImageScore,
    ModelOutputError,
    RegionOut,
    RegionsOut,
    Scene,
    SceneAnalysis,
    ScoreBatchOut,
)

from .conftest import FakeClient, fake_response, fake_usage

SCENE = Scene(
    summary="海边高层俯瞰",
    elements=[Element(name="海景", description="看到海", weight=1.0), Element(name="高层", description="俯瞰", weight=0.6)],
)


def api_error(code, msg):
    return errors.ClientError(code, {"error": {"code": code, "message": msg, "status": "X"}})


# ---- 纯函数 ----


def test_extract_json_fenced_and_bare():
    assert extract_json('说明\n```json\n{"a": 1}\n```\n') == '{"a": 1}'
    assert extract_json('前缀 {"a": {"b": 2}} 后缀') == '{"a": {"b": 2}}'
    with pytest.raises(ModelOutputError):
        extract_json("没有 json")


def test_parse_output_prefers_parsed_then_text():
    parsed = RegionsOut(regions=[])
    assert parse_output(fake_response(parsed=parsed), RegionsOut) is parsed
    r = fake_response(text='```json\n{"regions": []}\n```')
    assert parse_output(r, RegionsOut).regions == []
    with pytest.raises(ModelOutputError):
        parse_output(fake_response(text='{"regions": [{"city": 1}]}'), RegionsOut)


@pytest.mark.parametrize(
    "code,msg,needle",
    [
        (403, "permission denied", "GEMINI_API_KEY"),
        (400, "Billing is not enabled / FAILED_PRECONDITION", "账单"),
        (429, "Resource exhausted", "Spend Cap"),
        (404, "model not found", "GEMINI_MODEL"),
        (503, "unavailable", "稍后"),
    ],
)
def test_error_hint(code, msg, needle):
    assert needle in error_hint(code, msg)


# ---- Meter ----


def test_meter_counts_cost_and_limit():
    m = Meter(limit=2)
    m.before_call()
    record_usage(m, fake_response(usage=fake_usage(prompt=1_000_000, out=100_000, thoughts=100_000)))
    assert m.usage.calls == 1
    assert m.usage.usd == pytest.approx(0.30 + 0.2 * 2.5)
    m.before_call()
    with pytest.raises(CallLimitExceeded):
        m.before_call()


def test_meter_warns_when_image_tokens_deviate():
    m = Meter(limit=10)
    record_usage(m, fake_response(usage=fake_usage(image_tokens=8 * 1120)), n_images=8, media_res="low")
    assert len(m.warnings) == 1 and "media_resolution=low" in m.warnings[0]
    record_usage(m, fake_response(usage=fake_usage(image_tokens=8 * 1120)), n_images=8, media_res="low")
    assert len(m.warnings) == 1  # 每档只警告一次


def test_meter_no_warning_within_tolerance():
    m = Meter(limit=10)
    record_usage(m, fake_response(usage=fake_usage(image_tokens=8 * 300)), n_images=8, media_res="low")
    assert m.warnings == [] and m.usage.image_tokens == 2400 and m.usage.images == 8


# ---- 调用 ----


def make(settings, responses):
    client = FakeClient(responses)
    return Gemini(settings, client=client), client.models


def test_config_sets_media_resolution_thinking_and_schema(tmp_settings):
    g, _ = make(tmp_settings, [])
    cfg = g._config(media_res="low", thinking="minimal", schema=ScoreBatchOut)
    assert cfg.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert cfg.response_mime_type == "application/json" and cfg.response_schema is ScoreBatchOut
    assert g._config(media_res="medium").media_resolution == types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
    assert g._config(media_res="high").media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH


def test_analyze_scene_uses_high_res_and_retries_bad_output(tmp_settings):
    good = SceneAnalysis(summary="s", elements=SCENE.elements)
    g, models = make(tmp_settings, [fake_response(text="乱码"), fake_response(parsed=good)])
    m = Meter(10)
    assert g.analyze_scene(m, b"jpg") is good
    assert len(models.calls) == 2 and m.usage.calls == 2
    assert models.calls[0].config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
    assert isinstance(models.calls[0].contents[0], types.Part)


def test_analyze_scene_gives_up_after_two(tmp_settings):
    g, _ = make(tmp_settings, [fake_response(text="x"), fake_response(text="y")])
    with pytest.raises(GeminiError):
        g.analyze_scene(Meter(10), b"jpg")


def test_api_error_becomes_gemini_error_with_hint(tmp_settings):
    g, _ = make(tmp_settings, [api_error(403, "API key not valid")])
    with pytest.raises(GeminiError) as ei:
        g.analyze_scene(Meter(10), b"jpg")
    assert ei.value.code == 403 and "GEMINI_API_KEY" in ei.value.hint


def test_minimal_thinking_falls_back_to_low(tmp_settings):
    good = SceneAnalysis(summary="s", elements=SCENE.elements)
    g, models = make(tmp_settings, [api_error(400, "thinking_level MINIMAL is not supported"), fake_response(parsed=good)])
    g.analyze_scene(Meter(10), b"jpg")
    assert models.calls[1].config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    # 之后的调用直接用 low
    g._client.models.responses.append(fake_response(parsed=good))
    g.analyze_scene(Meter(10), b"jpg")
    assert models.calls[2].config.thinking_config.thinking_level == types.ThinkingLevel.LOW


def grounding():
    return SimpleNamespace(
        search_entry_point=SimpleNamespace(rendered_content="<div>chips</div>"),
        web_search_queries=["沿海 高层 民宿"],
        grounding_chunks=[
            SimpleNamespace(web=SimpleNamespace(uri="https://vertexaisearch/1", title="a.com", domain="a.com")),
            SimpleNamespace(web=SimpleNamespace(uri="https://vertexaisearch/1", title="dup", domain="a.com")),
            SimpleNamespace(web=None),
        ],
    )


REGIONS = RegionsOut(
    regions=[
        RegionOut(city="热海", district="伊豆山", reason="r", months="11 月", keywords=["熱海 オーシャンビュー"], search_region="jp-jp"),
        RegionOut(city="厦门", district="曾厝垵", reason="r", months="4 月", keywords=["厦门 海景民宿"], search_region="cn-zh"),
    ]
)


def test_find_regions_with_schema_and_grounding(tmp_settings):
    g, models = make(tmp_settings, [fake_response(text="{...}", parsed=REGIONS, grounding=grounding())])
    st = g.find_regions(Meter(10), SCENE, ["日本", "中国大陆"])
    cfg = models.calls[0].config
    assert cfg.tools[0].google_search is not None and cfg.response_schema is RegionsOut
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert "日本、中国大陆" in models.calls[0].contents and "海景（权重 1.0）" in models.calls[0].contents
    assert [r.id for r in st.regions] == ["r1", "r2"] and not any(r.selected for r in st.regions)
    assert st.search_suggestions_html == "<div>chips</div>"
    assert [c.uri for c in st.citations] == ["https://vertexaisearch/1"]
    assert st.web_search_queries == ["沿海 高层 民宿"] and st.scopes == ["日本", "中国大陆"]


def test_find_regions_falls_back_to_json_text_on_400(tmp_settings):
    text = "```json\n" + REGIONS.model_dump_json() + "\n```"
    g, models = make(tmp_settings, [api_error(400, "response_schema with tools unsupported"), fake_response(text=text)])
    st = g.find_regions(Meter(10), SCENE, [])
    assert models.calls[1].config.response_schema is None
    assert "```json" in models.calls[1].contents
    assert len(st.regions) == 2 and st.parse_error == "" and st.scopes == []


def test_find_regions_parse_failure_keeps_raw_text(tmp_settings):
    g, _ = make(tmp_settings, [fake_response(text="一些地区介绍，没有 JSON", grounding=grounding())])
    st = g.find_regions(Meter(10), SCENE, ["全球"])
    assert st.regions == [] and st.parse_error and st.raw_text.startswith("一些地区")
    assert st.search_suggestions_html


def test_find_regions_key_error_does_not_trigger_fallback(tmp_settings):
    g, models = make(tmp_settings, [api_error(400, "API key not valid. Please pass a valid API key.")])
    with pytest.raises(GeminiError):
        g.find_regions(Meter(10), SCENE, [])
    assert len(models.calls) == 1


def test_find_regions_non_400_error_raises(tmp_settings):
    g, _ = make(tmp_settings, [api_error(429, "quota")])
    with pytest.raises(GeminiError):
        g.find_regions(Meter(10), SCENE, [])


def test_score_batch_contents_and_low_res(tmp_settings):
    out = ScoreBatchOut(results=[ImageScore(index=0, scores=[ElementScore(element="海景", score=5)])])
    g, models = make(tmp_settings, [fake_response(parsed=out, usage=fake_usage(image_tokens=560))])
    m = Meter(10)
    assert g.score_batch(m, SCENE.elements, [b"a", b"b"]) is out
    call = models.calls[0]
    assert call.config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert call.contents[0] == "图 0：" and call.contents[2] == "图 1："
    assert isinstance(call.contents[1], types.Part) and '["海景", "高层"]' in call.contents[-1]
    assert m.usage.images == 2 and m.warnings == []


def test_missing_api_key_is_reported(tmp_settings):
    tmp_settings.gemini_api_key = ""
    with pytest.raises(GeminiError) as ei:
        Gemini(tmp_settings).analyze_scene(Meter(10), b"x")
    assert ".env" in ei.value.hint
