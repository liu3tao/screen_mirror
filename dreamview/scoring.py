"""打分：带权几何平均 + 批量打分（校验、重试 1 次、失败标 unscored）。"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from .schemas import Element, ModelOutputError, ScoreBatchOut, WallItem


def weighted_geometric_mean(scores: dict[str, int], elements: Sequence[Element]) -> float:
    """核心要素 0–10 分的带权几何平均。任一正权重要素为 0 分 → 0。"""
    total_w = sum(e.weight for e in elements if e.weight > 0)
    if total_w == 0:
        return 0.0
    log_sum = 0.0
    for e in elements:
        if e.weight <= 0:
            continue
        s = max(0, min(10, scores[e.name]))
        if s == 0:
            return 0.0
        log_sum += e.weight * math.log(s)
    return round(math.exp(log_sum / total_w), 2)


class ScoreValidationError(ModelOutputError):
    pass


def validate_batch(out: ScoreBatchOut, n_images: int, elements: Sequence[Element]) -> list[dict[str, int]]:
    """把模型输出整理成按图片顺序的 {要素: 分}；缺图或缺要素即报错。"""
    names = [e.name for e in elements]
    by_index: dict[int, dict[str, int]] = {}
    for r in out.results:
        if not 0 <= r.index < n_images:
            raise ScoreValidationError(f"index 越界：{r.index}")
        got = {s.element.strip(): s.score for s in r.scores}
        missing = [n for n in names if n not in got]
        if missing:
            raise ScoreValidationError(f"图 {r.index} 缺要素：{missing}")
        by_index[r.index] = {n: got[n] for n in names}
    absent = [i for i in range(n_images) if i not in by_index]
    if absent:
        raise ScoreValidationError(f"缺图：{absent}")
    return [by_index[i] for i in range(n_images)]


def score_items(
    items: Sequence[WallItem],
    elements: Sequence[Element],
    images: Sequence[bytes],
    call: Callable[[Sequence[bytes]], ScoreBatchOut],
) -> str | None:
    """给一批图打分，原地写回 items。模型输出不合 schema 重试 1 次，仍失败标 unscored 并返回原因。

    `call` 的异常（API 错误、调用上限）直接抛出，由调用方决定是否中止。
    """
    last_err = ""
    for _ in range(2):
        try:
            out = call(images)
            per_image = validate_batch(out, len(items), elements)
        except ModelOutputError as e:
            last_err = str(e)
            continue
        for item, scores in zip(items, per_image):
            item.scores = scores
            item.score = weighted_geometric_mean(scores, elements)
            item.status = "scored"
        return None
    for item in items:
        item.status = "unscored"
        item.score = None
    return last_err


def sort_wall(items: Sequence[WallItem]) -> list[WallItem]:
    """按分数降序；unscored / pending 排墙底。"""
    return sorted(items, key=lambda it: (it.score is None, -(it.score or 0.0)))
