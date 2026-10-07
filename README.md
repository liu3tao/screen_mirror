# dreamview（窗景找房）

上传理想窗景图 → 编辑场景 → 选搜索范围 → Gemini + Google Search 找候选地区 → 搜图、打分 → 照片墙。

设计文档：[docs/dreamview_tech_design.md](docs/dreamview_tech_design.md)

## 运行（macOS）

```sh
brew install uv
cp .env.example .env      # 填 GEMINI_API_KEY（默认后端：AI Studio）
uv sync
uv run dreamview          # http://127.0.0.1:8765
```

运行数据在 `runs/<run_id>/`（不入库）。删除 run = 删目录。

## 模型后端（`.env` 的 `MODEL_BACKEND`）

| 值 | 用途 | 需要 |
|---|---|---|
| `gemini`（默认） | AI Studio Gemini API | `GEMINI_API_KEY` |
| `vertex` | Vertex AI 上的 Gemini，可用 Google Cloud 试用额度 | 见下 |
| `ollama` | 本机视觉模型，免费；找地区不经 Google 搜索核实 | 见下 |

**Vertex**

```sh
brew install --cask google-cloud-sdk
gcloud auth application-default login
gcloud config set project <项目 ID>
# Cloud 控制台启用 Vertex AI API
# .env：MODEL_BACKEND=vertex，GOOGLE_CLOUD_PROJECT=<项目 ID>
```

**Ollama**

```sh
# 安装 Ollama（ollama.com）并启动
ollama pull qwen2.5vl:7b        # 或其他支持多图的视觉模型
# .env：MODEL_BACKEND=ollama，OLLAMA_MODEL=qwen2.5vl:7b
# 本机较慢时调小 SCORE_BATCH_SIZE；只支持单图的模型设为 1
```

## 测试

```sh
uv run pytest
```

测试不联网、不需要 API Key：Gemini 与图片搜索均为伪造实现。

### 联网集成测试（默认不跑）

```sh
uv run pytest -m live                    # 真实 Bing / DuckDuckGo / Brave 搜图 + 缩略图下载 + 完整流程（模型为伪造，不花钱）
LIVE_GEMINI=1 uv run pytest -m live      # 另跑真实模型（按 .env 的 MODEL_BACKEND；约 3 次调用，< 0.01 USD）
```

- Brave 测试需要 `BRAVE_API_KEY`（`.env` 或环境变量）。
- 以下情况**跳过**而非失败：主机不可达（网络策略）、缺 Key、DuckDuckGo 限流或拦截本机 IP。其他错误（如库升级导致请求出错）会**失败**。
- 真实模型测试会检查单图 token 偏差警告为空（即 `media_resolution` 生效）。
- 在 Claude 云端会话里跑：环境 Network access 需放行 `www.bing.com`、`duckduckgo.com`、`*.duckduckgo.com`、`*.mm.bing.net`、`api.search.brave.com`、`imgs.search.brave.com`；云端出口是数据中心 IP，DuckDuckGo 常被拦截，Brave 更可靠。
