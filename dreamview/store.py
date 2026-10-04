"""run 存储：每个 run 一个目录，无数据库。删除 run = 删目录。

runs/<run_id>/  ref.jpg  scene.json  regions.json  results.json  thumbs/  run.log
"""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
import shutil
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image

from .schemas import RegionsState, RunState, Scene

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
REF_MAX_SIDE = 2048


class RunNotFound(KeyError):
    pass


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._live: dict[str, RunState] = {}

    # ---- 路径 ----

    def dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id or ""):
            raise RunNotFound(run_id)
        d = self.root / run_id
        if not d.is_dir():
            raise RunNotFound(run_id)
        return d

    def thumbs_dir(self, run_id: str) -> Path:
        d = self.dir(run_id) / "thumbs"
        d.mkdir(exist_ok=True)
        return d

    # ---- 创建 / 列表 ----

    def create(self, image: bytes) -> RunState:
        """保存参考图（转 JPEG，长边 ≤ 2048）。图片无法解码时抛 ValueError。"""
        try:
            img = Image.open(io.BytesIO(image))
            img.load()
        except Exception as e:  # noqa: BLE001 - Pillow 抛多种异常
            raise ValueError(f"无法读取图片：{e}") from e
        img = img.convert("RGB")
        img.thumbnail((REF_MAX_SIDE, REF_MAX_SIDE), Image.Resampling.LANCZOS)
        now = datetime.now()
        run_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        d = self.root / run_id
        d.mkdir(parents=True)
        img.save(d / "ref.jpg", format="JPEG", quality=90)
        state = RunState(id=run_id, created=now.isoformat(timespec="seconds"))
        self.save_state(state)
        return state

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            if not (d.is_dir() and RUN_ID_RE.match(d.name)):
                continue
            try:
                state = self.load_state(d.name)
            except Exception:  # noqa: BLE001 - 损坏的 run 不影响列表
                continue
            summary = ""
            if (d / "scene.json").exists():
                summary = self.load_scene(d.name).summary
            out.append(
                {"id": d.name, "created": state.created, "status": state.status, "summary": summary, "n_items": len(state.items)}
            )
        return out

    # ---- 读写 ----

    def _write(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def save_state(self, state: RunState) -> None:
        with self.lock:
            self._live[state.id] = state
            self._write(self.root / state.id / "results.json", state.model_dump_json(indent=1))

    def load_state(self, run_id: str) -> RunState:
        with self.lock:
            if run_id in self._live:
                return self._live[run_id]
            p = self.dir(run_id) / "results.json"
            state = RunState.model_validate_json(p.read_text(encoding="utf-8"))
            self._live[run_id] = state
            return state

    def save_scene(self, run_id: str, scene: Scene) -> None:
        self._write(self.dir(run_id) / "scene.json", scene.model_dump_json(indent=1))

    def load_scene(self, run_id: str) -> Scene:
        p = self.dir(run_id) / "scene.json"
        return Scene.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else Scene()

    def save_regions(self, run_id: str, regions: RegionsState) -> None:
        self._write(self.dir(run_id) / "regions.json", regions.model_dump_json(indent=1))

    def load_regions(self, run_id: str) -> RegionsState:
        p = self.dir(run_id) / "regions.json"
        return RegionsState.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else RegionsState()

    def ref_image(self, run_id: str) -> bytes:
        return (self.dir(run_id) / "ref.jpg").read_bytes()

    def delete(self, run_id: str) -> None:
        d = self.dir(run_id)
        with self.lock:
            self._live.pop(run_id, None)
        shutil.rmtree(d)

    def logger(self, run_id: str) -> logging.Logger:
        """每个 run 一个 run.log。"""
        name = f"dreamview.run.{run_id}"
        lg = logging.getLogger(name)
        if not lg.handlers:
            h = logging.FileHandler(self.dir(run_id) / "run.log", encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
            lg.propagate = False
        return lg


def log_json(lg: logging.Logger, event: str, **data) -> None:
    lg.info("%s %s", event, json.dumps(data, ensure_ascii=False))
