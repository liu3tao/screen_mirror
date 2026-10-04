# dreamview（窗景找房）

上传理想窗景图 → 编辑场景 → 选搜索范围 → Gemini + Google Search 找候选地区 → 搜图、打分 → 照片墙。

设计文档：[docs/dreamview_tech_design.md](docs/dreamview_tech_design.md)

## 运行（macOS）

```sh
brew install uv
cp .env.example .env      # 填 GEMINI_API_KEY（AI Studio 付费层项目）
uv sync
uv run dreamview          # http://127.0.0.1:8765
```

运行数据在 `runs/<run_id>/`（不入库）。删除 run = 删目录。

## 测试

```sh
uv run pytest
```

测试不联网、不需要 API Key：Gemini 与图片搜索均为伪造实现。
