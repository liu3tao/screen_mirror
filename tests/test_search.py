import httpx
import pytest

from dreamview.search import (
    BraveSearch,
    DdgsSearch,
    Throttle,
    brave_params,
    classify_source,
    search_with_retry,
)


@pytest.mark.parametrize(
    "url,label",
    [
        ("https://www.airbnb.com/rooms/123", "房源"),
        ("https://zh.airbnb.com/rooms/1", "房源"),
        ("https://www.airbnb.co.jp/rooms/1", "房源"),
        ("https://notairbnb.com/x", "其他"),
        ("https://www.booking.com/hotel/jp/x.html", "房源"),
        ("https://hotels.ctrip.com/hotel/1.html", "房源"),
        ("https://you.ctrip.com/travels/x.html", "种草帖"),  # 携程游记
        ("https://ctrip.com/travels/1.html", "种草帖"),
        ("https://www.xiaohongshu.com/explore/abc", "种草帖"),
        ("https://www.mafengwo.cn/i/1.html", "种草帖"),
        ("https://example.com/a", "其他"),
        ("not a url", "其他"),
    ],
)
def test_classify_source(url, label):
    assert classify_source(url) == label


@pytest.mark.parametrize(
    "region,params",
    [
        ("wt-wt", {}),
        ("", {}),
        ("jp-jp", {"country": "JP", "search_lang": "jp"}),
        ("cn-zh", {"country": "CN", "search_lang": "zh-hans"}),
        ("tw-tzh", {"country": "TW", "search_lang": "zh-hant"}),
        ("it-it", {"country": "IT", "search_lang": "it"}),
        ("us-en", {"country": "US", "search_lang": "en"}),
    ],
)
def test_brave_params(region, params):
    assert brave_params(region) == params


class FakeDDGS:
    def __init__(self):
        self.kwargs = None

    def images(self, query, **kwargs):
        self.kwargs = dict(query=query, **kwargs)
        return [
            {"title": "t", "image": "https://img/1.jpg", "thumbnail": "https://tse/1", "url": "https://p/1", "height": "800", "width": "1200", "source": "Bing"},
            {"title": "no thumb", "image": "x", "thumbnail": "", "url": "https://p/2", "height": "", "width": ""},
        ]


def test_ddgs_search_maps_fields_and_region():
    fake = FakeDDGS()
    eng = DdgsSearch(fake)
    hits = eng.search("海景 民宿", "jp-jp", 50)
    assert fake.kwargs["region"] == "jp-jp" and fake.kwargs["max_results"] == 50 and fake.kwargs["backend"] == "bing"
    assert eng.name == "Bing"
    assert len(hits) == 1
    h = hits[0]
    assert (h.page_url, h.thumb_url, h.image_url, h.width, h.height) == ("https://p/1", "https://tse/1", "https://img/1.jpg", 1200, 800)


def test_ddgs_real_signature_accepts_our_kwargs():
    """真实 DDGS.images 接受这些参数名（不联网，只检查到发请求之前）。"""
    import inspect

    from ddgs import DDGS

    src = inspect.getsource(DDGS._search_sync)
    for name in ("region", "safesearch", "max_results", "backend"):
        assert name in src


def test_brave_search_request_and_mapping():
    seen = {}

    def handler(req: httpx.Request):
        seen["params"] = dict(req.url.params)
        seen["token"] = req.headers["X-Subscription-Token"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "a", "url": "https://p/1", "thumbnail": {"src": "https://imgs/1"}, "properties": {"url": "https://img/1", "width": 900, "height": 600}},
                    {"title": "b", "url": "", "thumbnail": {"src": "https://imgs/2"}},
                ]
            },
        )

    eng = BraveSearch("KEY", client=httpx.Client(transport=httpx.MockTransport(handler)))
    hits = eng.search("q", "jp-jp", 500)
    assert seen["token"] == "KEY"
    assert seen["params"]["country"] == "JP" and seen["params"]["search_lang"] == "jp" and seen["params"]["count"] == "200"
    assert len(hits) == 1 and hits[0].width == 900 and hits[0].image_url == "https://img/1"


def test_brave_requires_key():
    with pytest.raises(ValueError):
        BraveSearch("")


def test_throttle_waits_remaining_interval():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    th = Throttle(1.5, sleep=sleep, clock=lambda: t[0])
    th.wait()
    t[0] += 0.5
    th.wait()
    t[0] += 2.0
    th.wait()
    assert slept == [pytest.approx(1.0)]


def test_search_with_retry():
    class Flaky:
        name = "x"

        def __init__(self, fails):
            self.fails, self.n = fails, 0

        def search(self, q, r, m):
            self.n += 1
            if self.n <= self.fails:
                raise RuntimeError("boom")
            return ["ok"]

    th = Throttle(0)
    assert search_with_retry(Flaky(1), th, "q", "wt-wt", 10) == ["ok"]
    with pytest.raises(RuntimeError):
        search_with_retry(Flaky(2), th, "q", "wt-wt", 10)


def test_patch_ddgs_http2_headers_removes_connection():
    from ddgs.engines.duckduckgo_images import DuckduckgoImages

    from dreamview.search import patch_ddgs_http2_headers

    patch_ddgs_http2_headers()
    patch_ddgs_http2_headers()  # 幂等
    keys = {k.lower() for k in DuckduckgoImages.headers_update}
    assert "connection" not in keys and "referer" in keys


@pytest.mark.parametrize(
    "backend,name",
    [("bing", "Bing"), ("duckduckgo", "DuckDuckGo"), ("bing,duckduckgo", "Bing + DuckDuckGo")],
)
def test_ddgs_backend_is_configurable(backend, name):
    fake = FakeDDGS()
    eng = DdgsSearch(fake, backend=backend)
    eng.search("q", "wt-wt", 10)
    assert fake.kwargs["backend"] == backend and eng.name == name


def test_make_search_uses_settings():
    from dreamview.config import Settings
    from dreamview.search import image_source_name, make_search

    from dreamview.search import engine_supports_site, resolve_image_search

    s = Settings(ddgs_backend="duckduckgo")
    eng = make_search(s)
    assert isinstance(eng, DdgsSearch) and eng.backend == "duckduckgo" and eng.supports_site is False
    assert image_source_name(s) == "DuckDuckGo"
    # auto：无 Key → ddgs（Bing），有 Key → Brave
    assert resolve_image_search(Settings()) == "ddgs" and image_source_name(Settings()) == "Bing"
    with_key = Settings(brave_api_key="k")
    assert resolve_image_search(with_key) == "brave" and isinstance(make_search(with_key), BraveSearch)
    assert engine_supports_site(with_key) and not engine_supports_site(Settings())
    # 显式 ddgs 时即使有 Key 也用 ddgs
    assert resolve_image_search(Settings(image_search="ddgs", brave_api_key="k")) == "ddgs"


def test_every_rental_site_is_classified_as_rental():
    from dreamview.config import RENTAL_SITES
    from dreamview.search import is_rental_url

    for country, sites in RENTAL_SITES.items():
        for site in sites:
            assert is_rental_url(f"https://www.{site}/rooms/1"), (country, site)


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://www.airbnb.jp/rooms/1", True),
        ("https://www.jalan.net/yad123/", True),
        ("https://travel.rakuten.co.jp/HOTEL/1/", True),
        ("https://hotels.ctrip.com/hotel/1.html", True),
        ("https://you.ctrip.com/travels/1.html", False),  # 携程游记不是可租住页面
        ("https://suumo.jp/chintai/1/", False),  # 不动产网站
        ("https://www.homes.co.jp/chintai/1", False),
        ("https://www.xiaohongshu.com/explore/1", False),
        ("https://example.com/a", False),
    ],
)
def test_is_rental_url(url, ok):
    from dreamview.search import is_rental_url

    assert is_rental_url(url) is ok


def test_rental_sites_for_region():
    from dreamview.search import rental_sites_for

    assert rental_sites_for("jp-jp", 2) == ["airbnb.jp", "booking.com"]
    assert rental_sites_for("cn-zh", 4)[0] == "tujia.com"
    assert rental_sites_for("wt-wt", 3) == ["airbnb.com", "booking.com", "vrbo.com"]
    assert rental_sites_for("", 4) == rental_sites_for("xx-yy", 4)


def test_build_queries_rental_rotates_keywords():
    from dreamview.config import Settings
    from dreamview.pipeline import build_queries
    from dreamview.schemas import Region, RegionsState, Scene

    regions = RegionsState(
        regions=[
            Region(id="r1", city="a", keywords=["k1", "k2"], search_region="it-it", selected=True),
            Region(id="r2", city="b", keywords=["x"], selected=False),
            Region(id="r3", city="c", keywords=[" "], selected=True),  # 无有效关键词：跳过
        ]
    )
    qs = build_queries(Scene(), regions, Settings(sites_per_region=3))
    assert [q.text for q in qs] == ["k1 site:airbnb.com", "k2 site:booking.com", "k1 site:vrbo.com"]
    off = build_queries(Scene(), regions, Settings(site_filter="off"))
    assert [q.text for q in off] == ["k1", "k2"]
    no_site = build_queries(Scene(), regions, Settings(), supports_site=False)
    assert [q.text for q in no_site] == ["k1", "k2"]
