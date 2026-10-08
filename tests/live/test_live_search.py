"""真实图片搜索：结果数量、字段、区域参数、缩略图可下载。"""

from urllib.parse import urlparse

import pytest

from dreamview import images
from dreamview.search import classify_source

from .conftest import QUERIES, require_host, search_live

pytestmark = pytest.mark.live


@pytest.mark.parametrize("query,region", QUERIES)
def test_search_returns_usable_hits(engine_name, query, region):
    hits = search_live(engine_name, query, region)
    assert len(hits) >= 5, f"{engine_name}「{query}」只返回 {len(hits)} 条"
    for h in hits:
        assert urlparse(h.page_url).scheme in ("http", "https"), h.page_url
        assert urlparse(h.thumb_url).scheme in ("http", "https"), h.thumb_url
    # 来源判定不出错，且至少能给出标签
    labels = {classify_source(h.page_url) for h in hits}
    assert labels <= {"房源", "种草帖", "其他"}


def test_thumbnails_download_and_decode(engine_name):
    query, region = QUERIES[0]
    hits = search_live(engine_name, query, region, n=15)
    if not hits:
        pytest.skip("没有搜索结果")
    require_host(hits[0].thumb_url)
    ok, errors = 0, []
    with images.make_client() as client:
        for h in hits[:6]:
            try:
                data = images.download(client, h.thumb_url)
                jpeg, w, hgt, dh = images.normalize_thumb(data, 512)
                assert w > 0 and hgt > 0 and jpeg[:2] == b"\xff\xd8"
                ok += 1
            except Exception as e:  # noqa: BLE001 - 统计失败率
                errors.append(f"{h.thumb_url[:60]}: {e}")
    assert ok >= 3, f"6 张缩略图只成功 {ok} 张：{errors}"


@pytest.mark.parametrize("site", ["booking.com", "airbnb.com"])
def test_site_operator_is_honored(engine_name, site):
    """「关键词 site:域名」是否真的只返回该站结果。比例打印出来，供决定 SITE_FILTER 是否可行。"""
    from dreamview.search import is_rental_url

    hits = search_live(engine_name, f"Kyoto ryokan view site:{site}", "jp-jp")
    if not hits:
        pytest.skip("没有结果")
    on_site = sum(site.split(".")[0] in (urlparse(h.page_url).hostname or "") for h in hits)
    rental = sum(is_rental_url(h.page_url) for h in hits)
    print(f"\n[{engine_name}] site:{site} → {len(hits)} 条，{on_site} 条来自该站，{rental} 条为租住网站")
    assert rental >= 3, f"site:{site} 几乎无效：{len(hits)} 条中只有 {rental} 条来自租住网站"
