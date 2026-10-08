"""后台任务：图片搜索 → URL 去重 → 按地区轮转 → 下载 + dHash 去重截断 → 批量打分。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from . import images
from .config import Settings
from .llm import CallLimitExceeded, Meter, ModelBackend, ModelError
from .schemas import RegionsState, RunState, Scene, WallItem
from .scoring import score_items, sort_wall
from .search import ImageSearch, Throttle, classify_source, is_rental_url, rental_sites_for, search_with_retry
from .store import RunStore, log_json

KEYWORDS_PER_REGION = 3


@dataclass
class Query:
    text: str
    region_id: str
    region_label: str
    search_region: str


def build_queries(scene: Scene, regions: RegionsState, settings: Settings | None = None) -> list[Query]:
    """site_filter=off：勾选地区 × 2–3 条关键词。
    site_filter=rental（默认）：勾选地区 × N 个租住网站，每条为「关键词 site:域名」，关键词轮换使用。"""
    rental = settings is None or settings.site_filter == "rental"
    n_sites = settings.sites_per_region if settings else Settings.sites_per_region
    qs = []
    for r in regions.regions:
        if not r.selected:
            continue
        kws = [k.strip() for k in r.keywords if k.strip()][:KEYWORDS_PER_REGION]
        if not kws:
            continue
        region = r.search_region or "wt-wt"
        if rental:
            for i, site in enumerate(rental_sites_for(region, n_sites)):
                qs.append(Query(f"{kws[i % len(kws)]} site:{site}", r.id, r.label, region))
        else:
            qs.extend(Query(kw, r.id, r.label, region) for kw in kws)
    return qs


@dataclass
class JobDeps:
    settings: Settings
    store: RunStore
    model: ModelBackend
    engine: ImageSearch
    http: httpx.Client
    throttle: Throttle
    cancel: threading.Event = field(default_factory=threading.Event)


class Cancelled(Exception):
    """用户点了「停止」。"""


def _check(deps: JobDeps) -> None:
    if deps.cancel.is_set():
        raise Cancelled


def run_search_job(run_id: str, deps: JobDeps) -> None:
    store = deps.store
    lg = store.logger(run_id)
    state = store.load_state(run_id)
    scene = store.load_scene(run_id)
    regions = store.load_regions(run_id)

    def update(**kw) -> None:
        with store.lock:
            for k, v in kw.items():
                setattr(state, k, v)
            store.save_state(state)

    with store.lock:
        state.items = []
        state.search_errors = []
        state.filtered_out = 0
        state.error = state.error_hint = ""
    try:
        candidates = _search(state, scene, regions, deps, update, lg)
        _download(run_id, state, candidates, deps, update, lg)
        _score(state, scene, deps, update, lg)
    except Cancelled:
        with store.lock:
            for it in state.items:
                if it.status == "pending":
                    it.status = "unscored"
            state.items = sort_wall(state.items)
        update(status="stopped", stage="已停止", message="已按要求停止；已打分的图片保留。")
        log_json(lg, "stopped", n_items=len(state.items))
        return
    except Exception as e:  # noqa: BLE001 - 任何异常都要落到 UI
        lg.exception("job failed")
        hint = getattr(e, "hint", "")
        update(status="error", stage="", error=str(e) or e.__class__.__name__, error_hint=hint)
        return
    with store.lock:
        state.items = sort_wall(state.items)
    update(status="done", stage="完成", progress=1.0)
    log_json(lg, "done", usage=state.usage.model_dump(), n_items=len(state.items))


def _search(state: RunState, scene: Scene, regions: RegionsState, deps: JobDeps, update, lg) -> list[WallItem]:
    queries = build_queries(scene, regions, deps.settings)
    rental = deps.settings.site_filter == "rental"
    if not queries:
        raise ValueError("没有可搜索的关键词：请勾选至少一个有关键词的地区。")
    update(status="searching", stage=f"图片搜索（{deps.engine.name}）", progress=0.0, search_count=len(queries))
    found: list[WallItem] = []
    errors = []
    filtered = 0
    for i, q in enumerate(queries):
        _check(deps)
        try:
            hits = search_with_retry(deps.engine, deps.throttle, q.text, q.search_region, deps.settings.search_per_query)
        except Exception as e:  # noqa: BLE001 - 搜索失败跳过
            errors.append(f"「{q.text}」：{e}")
            log_json(lg, "search_error", query=q.text, error=str(e))
            hits = []
        kept = [h for h in hits if is_rental_url(h.page_url)] if rental else hits
        filtered += len(hits) - len(kept)
        log_json(lg, "search", query=q.text, region=q.search_region, hits=len(hits), kept=len(kept))
        for h in kept:
            found.append(
                WallItem(
                    id="",
                    page_url=h.page_url,
                    image_url=h.image_url,
                    thumb_url=h.thumb_url,
                    title=h.title,
                    region_id=q.region_id,
                    region_label=q.region_label,
                    query=q.text,
                    source_type=classify_source(h.page_url),
                    width=h.width,
                    height=h.height,
                )
            )
        update(progress=(i + 1) / len(queries) * 0.3, search_errors=list(errors), filtered_out=filtered)
    candidates = images.round_robin_items(images.dedupe_urls(found))
    for n, it in enumerate(candidates):
        it.id = f"i{n:04d}"
    if not candidates:
        detail = f"{len(errors)} 次搜索失败，见上方错误" if errors else ""
        if filtered:
            detail = (detail + "；" if detail else "") + f"{filtered} 张来自非短租 / 酒店网站，已过滤（可设 SITE_FILTER=off）"
        raise ValueError("图片搜索没有可用结果" + (f"（{detail}）" if detail else ""))
    return candidates


def _download(run_id: str, state: RunState, candidates: list[WallItem], deps: JobDeps, update, lg) -> None:
    s, store = deps.settings, deps.store
    thumbs = store.thumbs_dir(run_id)
    for old in thumbs.glob("*.jpg"):  # 重跑时清掉上次的缩略图
        old.unlink()
    index = images.NearDupIndex(s.dhash_max_distance)
    update(status="downloading", stage="下载缩略图并去重", progress=0.3)

    def fetch(item: WallItem):
        try:
            data = images.download(deps.http, item.thumb_url)
            return item, images.normalize_thumb(data, s.thumb_max_side)
        except Exception as e:  # noqa: BLE001 - 单张失败跳过
            return item, e

    failed = 0
    with ThreadPoolExecutor(max_workers=s.download_workers) as pool:
        for chunk in images.chunks(candidates, s.download_workers * 4):
            _check(deps)
            if len(index.kept) >= s.max_images:
                break
            for item, res in pool.map(fetch, chunk):
                if len(index.kept) >= s.max_images:
                    break
                if isinstance(res, Exception):
                    failed += 1
                    continue
                jpeg, w, h, dh = res
                item.width, item.height = item.width or w, item.height or h
                item.dhash = f"{dh:016x}"
                keep, replaced = index.add(item)
                if not keep:
                    continue
                (thumbs / f"{item.id}.jpg").write_bytes(jpeg)
                item.thumb_file = f"thumbs/{item.id}.jpg"
                with store.lock:
                    if replaced is not None:
                        (thumbs / f"{replaced.id}.jpg").unlink(missing_ok=True)
                        pos = next(i for i, x in enumerate(state.items) if x.id == replaced.id)
                        state.items[pos] = item
                    else:
                        state.items.append(item)
            update(progress=0.3 + 0.2 * min(1.0, len(index.kept) / s.max_images))
    log_json(lg, "download", kept=len(index.kept), failed=failed, candidates=len(candidates))
    if not state.items:
        raise ValueError(f"缩略图全部下载失败（{failed} 张）")


def _score(state: RunState, scene: Scene, deps: JobDeps, update, lg) -> None:
    s, store = deps.settings, deps.store
    elements = [e for e in scene.elements if e.weight > 0]
    if not elements:
        raise ValueError("场景没有权重 > 0 的核心要素")
    meter = Meter(s.max_model_calls_per_run, state.usage)
    run_dir = store.dir(state.id)
    batches = list(images.chunks(list(state.items), s.score_batch_size))
    update(status="scoring", stage="打分", progress=0.5)
    for bi, batch in enumerate(batches):
        _check(deps)
        data = [(run_dir / it.thumb_file).read_bytes() for it in batch]
        try:
            err = score_items(batch, elements, data, lambda imgs: deps.model.score_batch(meter, elements, imgs))
        except CallLimitExceeded as e:
            for it in [it for b in batches[bi:] for it in b]:
                it.status = "unscored"
            meter.warnings.append(str(e) + "，其余图片未打分。")
            break
        except ModelError as e:
            if e.code and e.code >= 500:
                for it in batch:
                    it.status = "unscored"
                meter.warnings.append(f"第 {bi + 1} 批打分失败（{e}），已跳过。")
                continue
            for it in [it for b in batches[bi:] for it in b]:
                it.status = "unscored"
            raise
        if err:
            log_json(lg, "score_unscored", batch=bi, error=err)
        log_json(lg, "usage", **meter.usage.model_dump())
        with store.lock:
            state.warnings = list(dict.fromkeys(state.warnings + meter.warnings))
        update(progress=0.5 + 0.5 * (bi + 1) / len(batches), usage=meter.usage)
    with store.lock:
        state.warnings = list(dict.fromkeys(state.warnings + meter.warnings))


def make_deps_factory(settings: Settings, store: RunStore, model: ModelBackend, engine_factory: Callable[[], ImageSearch]):
    def factory() -> JobDeps:
        return JobDeps(
            settings=settings,
            store=store,
            model=model,
            engine=engine_factory(),
            http=images.make_client(),
            throttle=Throttle(settings.search_interval_s),
        )

    return factory
