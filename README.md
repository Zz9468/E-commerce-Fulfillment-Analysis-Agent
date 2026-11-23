# 电商履约分析 Agent

面向电商和物流运营的本地分析工作台。用户可以用中文查询履约指标、对比两个月的表现，并根据查询证据追查地区或配送阶段。

## 本地运行

以下命令在项目根目录执行，需安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)、Docker Compose 和 Node.js。后端使用 Python 3.12，前端在 Node.js 24 上验证。依赖分别锁定在 [uv.lock](backend/uv.lock) 和 [package-lock.json](frontend/package-lock.json)。

```powershell
Copy-Item .env.example .env
docker compose up -d --wait db
uv sync --directory backend --locked
uv run --directory backend --locked --env-file ../.env python -m app.migrate
uv run --directory backend --locked --env-file ../.env python -m scripts.seed_demo
npm --prefix frontend ci
```

已有 `.env` 时沿用现有配置。数据库默认监听 `127.0.0.1:55432`，迁移和导入读取 `DATABASE_URL`。合成演示包含 96 单，重复初始化会返回同一数据版本。

打开两个终端启动后端和前端。

```powershell
uv run --directory backend --locked --env-file ../.env uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```powershell
npm --prefix frontend run dev
```

在[履约分析工作台](http://127.0.0.1:5173/)选择总部运营或圣保罗州运营。应用使用固定本地演示身份，API 分别接受 `Authorization: Bearer demo-hq` 和 `Authorization: Bearer demo-sp`。会话和证据保存在单个后端进程的内存中，重启后开启新会话。

| 想验证的路径 | 提问或操作 |
| --- | --- |
| 单月查数 | 2018 年 8 月超期率是多少？ |
| 地区调查 | 2018 年 7 月和 8 月超期率，排查新增超期集中在哪些地区。展开州拆分查询，查看 SP 城市。 |
| 提前停止 | 2018 年 8 月和 9 月超期率变化原因。 |
| 变化分散 | 2018 年 9 月和 10 月超期率变化原因。查看阶段证据。 |
| 权限限制 | 切换到圣保罗州运营，再问 2018 年 8 月全国超期率。 |

### 调查模式

将 `.env` 内的 `ANALYSIS_MODE` 设为 `rule` 可运行规则调查，设为 `fixed` 可离线重放模型工具调用。两种模式都执行实际的 PostgreSQL 查询。`fixed` 重放默认地区调查示例，明确查数和手动下钻仍走单次查询。

真实模型配置可放在项目根目录的 `.env.model.local`，该文件由 Git 忽略。填写 `ANALYSIS_MODE=model`、`MODEL_BASE_URL`、`MODEL_NAME` 和 `MODEL_API_KEY`。`MODEL_BASE_URL` 使用兼容 API 的基础路径，后端会追加 `/chat/completions`。可用 `MODEL_TIMEOUT_SECONDS` 调整单次请求超时，按端点能力设置 `MODEL_REASONING_EFFORT`。

阿里云百炼 `qwen3.8-flash` 冒烟使用北京接入地址 `https://dashscope.aliyuncs.com/compatible-mode/v1`、`MODEL_REASONING_EFFORT=low` 和单次请求超时 45 秒。API Key 与 Base URL 需来自同一地域和计费方案。[百炼 Base URL 总览](https://help.aliyun.com/zh/model-studio/base-url)

加载本地模型配置启动后端。工作台显示「模型调查」，选择「排查超期集中地区」验证完整工具流程。

```powershell
uv run --directory backend --locked --env-file ../.env --env-file ../.env.model.local uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 检查与验收

```powershell
uv run --directory backend --locked ruff check .
uv run --directory backend --locked ruff format --check .
uv run --directory backend --locked --env-file ../.env pytest -q
npm --prefix frontend run lint -- --max-warnings 0
npm --prefix frontend run format:check
npm --prefix frontend run build
```

pytest 使用 `TEST_DATABASE_ADMIN_URL` 在每次运行时创建独立空库，应用迁移，并在结束时清理该次生成的测试库。使用自备 PostgreSQL 时，在 `.env` 中配置两个连接地址，测试连接的角色需能创建数据库。默认回归使用合成数据，无需模型密钥。

显式运行真实模型冒烟。用例在隔离 PostgreSQL 中导入合成数据，验证真实模型请求了多步查询，以及最终响应中的证据 ID 与统计值。

```powershell
uv run --directory backend --locked --env-file ../.env --env-file ../.env.model.local pytest -q -s tests/test_investigations.py -k live_model --live-model
```

浏览器回归使用 96 单合成数据和规则模式。保持上述后端运行。如果已经安装 Chrome，可在 PowerShell 中执行以下命令，Playwright 会按需启动前端。

```powershell
$env:PLAYWRIGHT_CHANNEL = 'chrome'
npm --prefix frontend run test:e2e
```

## 导入和复算 Olist

从 [Kaggle 的 Olist 数据集](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) 下载并解压原始文件，将 `olist_orders_dataset.csv` 和 `olist_customers_dataset.csv` 放到 `data/olist/`。

```powershell
uv run --directory backend --locked --env-file ../.env python -m scripts.import_olist ../data/olist/olist_orders_dataset.csv ../data/olist/olist_customers_dataset.csv
uv run --directory backend --locked --env-file ../.env pytest -q --olist-dir=../data/olist
```

带 `--olist-dir` 的回归会将原始文件导入隔离测试库，对照系统查询、[独立参考 SQL](eval/reference_sql/) 和已知验收值，并检查规则调查能从整体比较追查到圣保罗市。样本数、阶段耗时与文件 SHA-256 见 [M1 验收记录](docs/里程碑.md#m1-把订单数据算对)。

开发文档见[项目索引](项目索引.md)。
