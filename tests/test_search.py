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
    hits = DdgsSearch(fake).search("海景 民宿", "jp-jp", 50)
    assert fake.kwargs["region"] == "jp-jp" and fake.kwargs["max_results"] == 50 and fake.kwargs["backend"] == "duckduckgo"
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
