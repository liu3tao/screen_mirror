import math

import pytest

from dreamview.schemas import Element, ElementScore, ImageScore, ModelOutputError, ScoreBatchOut, WallItem
from dreamview.scoring import ScoreValidationError, score_items, sort_wall, validate_batch, weighted_geometric_mean

ELS = [Element(name="海景", description="", weight=1.0), Element(name="大窗", description="", weight=0.5)]


def out(*per_image):
    return ScoreBatchOut(
        results=[ImageScore(index=i, scores=[ElementScore(element=k, score=v) for k, v in s.items()]) for i, s in enumerate(per_image)]
    )


def item(i, score=None, status="pending"):
    return WallItem(id=f"i{i}", page_url=f"https://p/{i}", thumb_url=f"https://t/{i}", score=score, status=status)


def test_geometric_mean_equal_scores():
    assert weighted_geometric_mean({"海景": 8, "大窗": 8}, ELS) == 8.0


def test_geometric_mean_weighted():
    expected = math.exp((1.0 * math.log(9) + 0.5 * math.log(4)) / 1.5)
    assert weighted_geometric_mean({"海景": 9, "大窗": 4}, ELS) == pytest.approx(expected, abs=0.01)


def test_geometric_mean_zero_kills():
    assert weighted_geometric_mean({"海景": 0, "大窗": 10}, ELS) == 0.0


def test_geometric_mean_ignores_zero_weight():
    els = ELS + [Element(name="无关", description="", weight=0.0)]
    assert weighted_geometric_mean({"海景": 6, "大窗": 6, "无关": 0}, els) == 6.0


def test_geometric_mean_clamps():
    assert weighted_geometric_mean({"海景": 15, "大窗": 10}, ELS) == 10.0


def test_validate_batch_reorders_by_index():
    o = ScoreBatchOut(
        results=[
            ImageScore(index=1, scores=[ElementScore(element="海景", score=2), ElementScore(element="大窗", score=3)]),
            ImageScore(index=0, scores=[ElementScore(element="海景", score=7), ElementScore(element="大窗", score=8)]),
        ]
    )
    assert validate_batch(o, 2, ELS) == [{"海景": 7, "大窗": 8}, {"海景": 2, "大窗": 3}]


@pytest.mark.parametrize(
    "bad",
    [
        out({"海景": 5, "大窗": 5}),  # 缺图 1
        out({"海景": 5}, {"海景": 5, "大窗": 5}),  # 缺要素
        ScoreBatchOut(results=[ImageScore(index=5, scores=[])]),  # 越界
    ],
)
def test_validate_batch_rejects(bad):
    with pytest.raises(ScoreValidationError):
        validate_batch(bad, 2, ELS)


def test_score_items_success():
    items = [item(0), item(1)]
    err = score_items(items, ELS, [b"a", b"b"], lambda imgs: out({"海景": 8, "大窗": 8}, {"海景": 0, "大窗": 9}))
    assert err is None
    assert [i.status for i in items] == ["scored", "scored"]
    assert items[0].score == 8.0 and items[1].score == 0.0
    assert items[1].scores == {"海景": 0, "大窗": 9}


def test_score_items_retries_once_then_succeeds():
    calls = []
    responses = [out({"海景": 1}), out({"海景": 5, "大窗": 5})]

    def call(imgs):
        calls.append(1)
        return responses.pop(0)

    items = [item(0)]
    assert score_items(items, ELS, [b"a"], call) is None
    assert len(calls) == 2 and items[0].score == 5.0


def test_score_items_marks_unscored_after_two_failures():
    def call(imgs):
        raise ModelOutputError("bad json")

    items = [item(0), item(1)]
    err = score_items(items, ELS, [b"a", b"b"], call)
    assert "bad json" in err
    assert all(i.status == "unscored" and i.score is None for i in items)


def test_score_items_propagates_api_errors():
    def call(imgs):
        raise RuntimeError("api down")

    with pytest.raises(RuntimeError):
        score_items([item(0)], ELS, [b"a"], call)


def test_sort_wall_puts_unscored_last():
    items = [item(0, None, "unscored"), item(1, 3.0), item(2, 9.0), item(3, None)]
    assert [i.id for i in sort_wall(items)] == ["i2", "i1", "i0", "i3"]
