import io
from collections import OrderedDict

from PIL import Image, ImageEnhance

from dreamview import images
from dreamview.schemas import WallItem

from .conftest import make_image


def _img(data):
    return Image.open(io.BytesIO(data))


def test_dhash_identical_and_near_and_different():
    a = _img(make_image(1))
    brighter = ImageEnhance.Brightness(a.convert("RGB")).enhance(1.1)
    resized = a.resize((320, 240))
    other = _img(make_image(7))
    h = images.dhash(a)
    assert images.hamming(h, images.dhash(a)) == 0
    assert images.hamming(h, images.dhash(brighter)) <= 6
    assert images.hamming(h, images.dhash(resized)) <= 6
    assert images.hamming(h, images.dhash(other)) > 6


def test_normalize_thumb_resizes_and_converts():
    data = make_image(3, size=(1200, 600), fmt="PNG")
    jpeg, w, h, dh = images.normalize_thumb(data, 512)
    out = _img(jpeg)
    assert out.format == "JPEG" and max(out.size) == 512
    assert (w, h) == (1200, 600)
    assert 0 <= dh < 2**64


def test_round_robin_interleaves():
    groups = OrderedDict(a=[1, 2, 3], b=[10], c=[20, 21])
    assert list(images.round_robin(groups)) == [1, 10, 20, 2, 21, 3]


def wi(i, region="r1", thumb=None, page=None, image="", w=0, h=0, dh=""):
    return WallItem(
        id=f"i{i}", page_url=page or f"https://p/{i}", thumb_url=thumb or f"https://t/{i}", image_url=image,
        region_id=region, width=w, height=h, dhash=dh,
    )


def test_round_robin_items_by_region():
    items = [wi(0, "a"), wi(1, "a"), wi(2, "b"), wi(3, "a"), wi(4, "b")]
    assert [i.id for i in images.round_robin_items(items)] == ["i0", "i2", "i1", "i4", "i3"]


def test_dedupe_urls():
    items = [
        wi(0, thumb="https://t/x"),
        wi(1, thumb="https://t/x"),  # 同缩略图
        wi(2, page="https://p/same", image="https://img/1"),
        wi(3, page="https://p/same", image="https://img/1", thumb="https://t/other"),  # 同来源页+原图
        wi(4, page="https://p/same", image="https://img/2"),
    ]
    assert [i.id for i in images.dedupe_urls(items)] == ["i0", "i2", "i4"]


def test_near_dup_keeps_higher_resolution():
    idx = images.NearDupIndex(6)
    small = wi(0, w=300, h=200, dh="00000000000000ff")
    big = wi(1, w=1200, h=800, dh="00000000000000fe")  # 汉明距 1
    smaller = wi(2, w=100, h=100, dh="00000000000000fc")
    far = wi(3, w=10, h=10, dh="ffffffffffffff00")
    assert idx.add(small) == (True, None)
    assert idx.add(big) == (True, small)
    assert idx.add(smaller) == (False, None)
    assert idx.add(far) == (True, None)
    assert [i.id for i in idx.kept] == ["i1", "i3"]


def test_chunks():
    assert [list(c) for c in images.chunks([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
