"""FastAPI：单页 UI + JSON 接口 + run 文件（参考图、缩略图）。只监听 127.0.0.1。"""

from __future__ import annotations

import threading
from collections.abc import Callable

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from .config import SCOPE_TAGS, STATIC_DIR, Settings
from .gemini import CallLimitExceeded, Gemini, GeminiError, Meter
from .pipeline import JobDeps, build_queries, make_deps_factory, run_search_job
from .schemas import Region, RegionsState, Scene
from .search import ImageSearch, make_search
from .store import RunNotFound, RunStore, log_json

ACTIVE_STATUSES = {"searching", "downloading", "scoring"}


class ScopesIn(BaseModel):
    scopes: list[str] = []


class RegionsIn(BaseModel):
    regions: list[Region]


def create_app(
    settings: Settings | None = None,
    *,
    gemini: Gemini | None = None,
    engine_factory: Callable[[], ImageSearch] | None = None,
    deps_factory: Callable[[], JobDeps] | None = None,
    run_jobs_inline: bool = False,
) -> FastAPI:
    s = settings or Settings.from_env()
    store = RunStore(s.runs_dir)
    gem = gemini or Gemini(s)
    deps_factory = deps_factory or make_deps_factory(s, store, gem, engine_factory or (lambda: make_search(s)))
    running: set[str] = set()
    running_lock = threading.Lock()

    app = FastAPI(title="dreamview")
    app.state.store = store
    app.state.settings = s

    @app.exception_handler(RunNotFound)
    async def _not_found(_: Request, exc: RunNotFound):
        return JSONResponse({"error": f"run 不存在：{exc}"}, status_code=404)

    @app.exception_handler(GeminiError)
    async def _gemini_error(_: Request, exc: GeminiError):
        return JSONResponse({"error": str(exc), "hint": exc.hint}, status_code=502)

    @app.exception_handler(CallLimitExceeded)
    async def _limit(_: Request, exc: CallLimitExceeded):
        return JSONResponse({"error": str(exc), "hint": "新建一个 run，或在 .env 调高上限。"}, status_code=429)

    def meter_for(run_id: str) -> Meter:
        return Meter(s.max_model_calls_per_run, store.load_state(run_id).usage)

    def save_usage(run_id: str, meter: Meter) -> None:
        with store.lock:
            state = store.load_state(run_id)
            state.warnings = list(dict.fromkeys(state.warnings + meter.warnings))
            store.save_state(state)
        log_json(store.logger(run_id), "usage", **meter.usage.model_dump())

    # ---- 页面与文件 ----

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/files/{run_id}/{path:path}")
    def run_file(run_id: str, path: str):
        d = store.dir(run_id).resolve()
        f = (d / path).resolve()
        if d not in f.parents or not f.is_file() or f.suffix != ".jpg":
            raise HTTPException(404)
        return FileResponse(f)

    # ---- 接口 ----

    @app.get("/api/config")
    def config():
        return {
            "scope_tags": SCOPE_TAGS,
            "image_search": s.image_search,
            "max_images": s.max_images,
            "score_batch_size": s.score_batch_size,
            "model": s.gemini_model,
        }

    @app.get("/api/runs")
    def list_runs():
        return store.list()

    @app.post("/api/runs")
    async def create_run(file: UploadFile = File(...)):
        data = await file.read()
        try:
            state = store.create(data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        meter = meter_for(state.id)
        try:
            analysis = await run_in_threadpool(gem.analyze_scene, meter, store.ref_image(state.id))
        except (GeminiError, CallLimitExceeded) as e:
            save_usage(state.id, meter)
            with store.lock:
                state.status, state.error, state.error_hint = "error", str(e), getattr(e, "hint", "")
                store.save_state(state)
            return JSONResponse({"error": str(e), "hint": getattr(e, "hint", ""), "run_id": state.id}, status_code=502)
        scene = Scene(**analysis.model_dump())
        store.save_scene(state.id, scene)
        save_usage(state.id, meter)
        with store.lock:
            state.status = "analyzed"
            store.save_state(state)
        return run_detail(state.id)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        with store.lock:
            st = store.load_state(run_id)
            if st.status in ACTIVE_STATUSES and run_id not in running:
                st.status, st.error = "error", "任务已中断（服务重启）。可重新点「搜图并打分」。"
                store.save_state(st)
            state = st.model_dump()
        return {
            "state": state,
            "scene": store.load_scene(run_id).model_dump(),
            "regions": store.load_regions(run_id).model_dump(),
            "running": run_id in running,
        }

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        if run_id in running:
            raise HTTPException(409, "run 正在运行")
        store.delete(run_id)
        return {"ok": True}

    @app.put("/api/runs/{run_id}/scene")
    def save_scene(run_id: str, scene: Scene):
        store.dir(run_id)
        for e in scene.elements:
            e.name = e.name.strip()
        names = [e.name for e in scene.elements]
        if any(not n for n in names) or len(set(names)) != len(names):
            raise HTTPException(400, "核心要素名称不能为空或重复")
        store.save_scene(run_id, scene)
        return scene

    @app.post("/api/runs/{run_id}/regions")
    def find_regions(run_id: str, body: ScopesIn):
        scene = store.load_scene(run_id)
        if not scene.elements:
            raise HTTPException(400, "请先完成场景解析")
        scopes = [x.strip() for x in body.scopes if x.strip()] or ["全球"]
        meter = meter_for(run_id)
        try:
            new = gem.find_regions(meter, scene, scopes)
        finally:
            save_usage(run_id, meter)
        # 保留手动添加的地区
        # 保留手动添加的地区（id 以 m 开头，不与 grounding 的 r1… 冲突）
        new.regions += [r for r in store.load_regions(run_id).regions if r.manual]
        store.save_regions(run_id, new)
        return new

    @app.put("/api/runs/{run_id}/regions")
    def save_regions(run_id: str, body: RegionsIn):
        current = store.load_regions(run_id)
        ids = [r.id for r in body.regions]
        if len(set(ids)) != len(ids) or any(not r.city.strip() for r in body.regions):
            raise HTTPException(400, "地区 id 重复或城市为空")
        current.regions = body.regions
        store.save_regions(run_id, current)
        return current

    @app.post("/api/runs/{run_id}/search", status_code=202)
    def start_search(run_id: str):
        store.dir(run_id)
        queries = build_queries(store.load_scene(run_id), store.load_regions(run_id))
        if not queries:
            raise HTTPException(400, "没有可搜索的关键词：请勾选至少一个地区，或开启通用关键词。")
        with running_lock:
            if run_id in running:
                raise HTTPException(409, "该 run 正在运行")
            running.add(run_id)
        with store.lock:
            state = store.load_state(run_id)
            state.status, state.stage, state.progress = "searching", "排队", 0.0
            state.warnings = []
            store.save_state(state)

        def job():
            try:
                run_search_job(run_id, deps_factory())
            finally:
                with running_lock:
                    running.discard(run_id)

        if run_jobs_inline:
            job()
        else:
            threading.Thread(target=job, name=f"job-{run_id}", daemon=True).start()
        return {"run_id": run_id, "queries": len(queries)}

    return app


def main() -> None:
    import uvicorn

    s = Settings.from_env()
    print(f"dreamview: http://{s.host}:{s.port}")
    uvicorn.run(create_app(s), host=s.host, port=s.port)


if __name__ == "__main__":
    main()
