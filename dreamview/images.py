"""图片：缩略图下载与压缩、dHash 近重复判定、按地区轮转截断。"""

from __future__ import annotations

import io
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from typing import TypeVar

import httpx
from PIL import Image

from .schemas import WallItem

T = TypeVar("T")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


def dhash(img: Image.Image, size: int = 8) -> int:
    """差值哈希：缩到 (size+1)×size 灰度，比较相邻像素。"""
    small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(small.tobytes())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def normalize_thumb(data: bytes, max_side: int) -> tuple[bytes, int, int, int]:
    """解码 → RGB → 长边 ≤ max_side → JPEG。返回 (jpeg, 宽, 高, dhash)。解码失败抛异常。"""
    img = Image.open(io.BytesIO(data))
    img.load()
    w, h = img.size
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), w, h, dhash(img)


def round_robin(groups: "OrderedDict[str, list[T]]") -> Iterator[T]:
    """按组轮流取：A1 B1 C1 A2 B2 …，保证截断时各地区均衡。"""
    iters = [iter(v) for v in groups.values()]
    while iters:
        alive = []
        for it in iters:
            try:
                yield next(it)
            except StopIteration:
                continue
            alive.append(it)
        iters = alive


def round_robin_items(items: Iterable[WallItem]) -> list[WallItem]:
    groups: OrderedDict[str, list[WallItem]] = OrderedDict()
    for it in items:
        groups.setdefault(it.region_id, []).append(it)
    return list(round_robin(groups))


def dedupe_urls(items: Iterable[WallItem]) -> list[WallItem]:
    """同一缩略图 URL 或同一（来源页 + 原图）只留第一个。"""
    seen: set[str] = set()
    out = []
    for it in items:
        keys = {it.thumb_url, f"{it.page_url}|{it.image_url}"} if it.image_url else {it.thumb_url}
        if keys & seen:
            continue
        seen |= keys
        out.append(it)
    return out


class NearDupIndex:
    """dHash 近重复索引：汉明距 ≤ max_distance 视为同图，保留分辨率高者。"""

    def __init__(self, max_distance: int):
        self.max_distance = max_distance
        self.kept: list[WallItem] = []

    def add(self, item: WallItem) -> tuple[bool, WallItem | None]:
        """返回 (是否保留 item, 被替换掉的旧 item)。"""
        h = int(item.dhash, 16)
        for i, other in enumerate(self.kept):
            if hamming(h, int(other.dhash, 16)) <= self.max_distance:
                if item.width * item.height > other.width * other.height:
                    self.kept[i] = item
                    return True, other
                return False, None
        self.kept.append(item)
        return True, None


def download(client: httpx.Client, url: str, timeout: float = 15.0) -> bytes:
    r = client.get(url, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
        raise ValueError(f"非图片内容：{ctype}")
    return r.content


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT})


def chunks(seq: Sequence[T], n: int) -> Iterator[Sequence[T]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
