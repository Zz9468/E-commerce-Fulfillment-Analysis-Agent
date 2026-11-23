# 电商履约分析 Agent

面向电商和物流运营的本地分析工作台。用户可以用中文查询履约指标、对比两个月的表现，并根据查询证据追查地区或配送阶段。

## 本地运行

在项目根目录用 Docker Compose 启动。首次启动会创建 PostgreSQL 数据卷、应用迁移、配置两个只读登录，并导入 96 单合成数据。默认使用规则调查，无需模型密钥。

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --build --wait
```

打开[履约分析工作台](http://127.0.0.1:5173/)。可在 `.env` 用 `WORKSPACE_PORT` 和 `POSTGRES_PORT` 调整本地端口。Compose 会等待初始化和 API 就绪，重复启动沿用已发布的合成版本。

### 本地开发

后端使用 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，前端使用 Node.js 24。已有旧版 `.env` 时，从示例补充 `QUERY_HQ_PASSWORD` 和 `QUERY_SP_PASSWORD`。以下命令只启动 Compose 数据库，API 和前端在宿主机运行。两种启动方式使用相同的工作台端口，切换前先停止占用端口的进程或服务。

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --wait db
uv sync --directory backend --locked
uv run --directory backend --locked --env-file ../.env python -m scripts.bootstrap_demo
npm --prefix frontend ci
```

数据库默认监听 `127.0.0.1:55432`。迁移与导入使用 `DATABASE_URL`。查询由 API 按已认证账号选用 `fulfillment_query_hq` 或 `fulfillment_query_sp`。使用自备 PostgreSQL 时，可用 `QUERY_DB_HOST`、`QUERY_DB_PORT` 和 `QUERY_DB_NAME` 指定查询连接地址。

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
| 缺少归因证据 | 完成地区调查后追问「是哪家快递的问题？」，展开已核实的地区证据。 |

### 调查模式

将 `.env` 内的 `ANALYSIS_MODE` 设为 `rule` 可运行规则调查，设为 `fixed` 可离线重放模型工具调用。两种模式都执行实际的 PostgreSQL 查询。`fixed` 重放默认地区调查示例，明确查数和手动下钻仍走单次查询。

可用 `SQL_CONNECT_TIMEOUT_SECONDS` 和 `SQL_STATEMENT_TIMEOUT_MS` 调整数据库连接与单条 SQL 超时。Compose 的 `HTTP_REQUEST_TIMEOUT_SECONDS` 同时配置代理等候和浏览器请求超时，修改后运行 `docker compose up -d --build --wait`。使用 Vite 本地开发时，可在启动前设置 `VITE_HTTP_TIMEOUT_SECONDS`。各默认值见配置示例。 [.env.example](.env.example)

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

pytest 使用 `TEST_DATABASE_ADMIN_URL` 创建独立空库和两个临时只读登录，测试结束后清理。维护连接和测试连接都需要管理本项目查询角色的权限，测试连接还需要建库权限。默认回归使用合成数据，无需模型密钥。

显式运行真实模型冒烟。用例在隔离 PostgreSQL 中导入合成数据，验证真实模型请求了多步查询，以及最终响应中的证据 ID 与统计值。

```powershell
uv run --directory backend --locked --env-file ../.env --env-file ../.env.model.local pytest -q -s tests/test_investigations.py -k live_model --live-model
```

浏览器回归使用 96 单合成数据和规则模式。保持上述后端运行。如果已经安装 Chrome，可在 PowerShell 中执行以下命令，Playwright 会按需启动前端。

```powershell
$env:PLAYWRIGHT_CHANNEL = 'chrome'
npm --prefix frontend run test:e2e
```

可在独立 Compose 项目运行完整浏览器回归。此命令会在一个用例中短暂暂停该项目的数据库，并验证页面提示、历史证据与恢复查询。`PLAYWRIGHT_BASE_URL` 指向这个项目时，Playwright 直接访问 Compose 工作台。

```powershell
$env:POSTGRES_PORT = '55433'
$env:WORKSPACE_PORT = '55173'
$env:PLAYWRIGHT_CHANNEL = 'chrome'
$env:PLAYWRIGHT_BASE_URL = 'http://127.0.0.1:55173'
$env:PLAYWRIGHT_FAULT_COMPOSE_PROJECT = 'fulfillment-m3-check'
docker compose --env-file .env.example -p fulfillment-m3-check up -d --build --wait
npm --prefix frontend run test:e2e

$env:ANALYSIS_MODE = 'fixed'
$env:PLAYWRIGHT_EXPECTED_MODE = 'fixed'
docker compose --env-file .env.example -p fulfillment-m3-check up -d --wait
npm --prefix frontend run test:e2e -- -g '固定模式'
```

## 导入和复算 Olist

从 [Kaggle 的 Olist 数据集](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) 下载并解压原始文件，将 `olist_orders_dataset.csv` 和 `olist_customers_dataset.csv` 放到 `data/olist/`。

```powershell
uv run --directory backend --locked --env-file ../.env python -m scripts.import_olist ../data/olist/olist_orders_dataset.csv ../data/olist/olist_customers_dataset.csv
uv run --directory backend --locked --env-file ../.env pytest -q --olist-dir=../data/olist
```

带 `--olist-dir` 的回归会将原始文件导入隔离测试库，对照系统查询、[独立参考 SQL](eval/reference_sql/) 和已知验收值，并检查规则调查能从整体比较追查到圣保罗市。样本数、阶段耗时与文件 SHA-256 见 [M1 验收记录](docs/里程碑.md#m1-把订单数据算对)。

## 复跑 M4 对照评测

按本地开发步骤启动数据库并运行 `scripts.bootstrap_demo`，再导入 Olist。开发集校准使用两个冻结数据版本，无需模型密钥。留出集使用 `freeze.json` 中的百炼模型配置，凭据从本地环境读取。每次运行指定一个新输出目录。

```powershell
uv run --directory backend --locked --env-file ../.env python -m scripts.evaluate calibrate --output ../eval/local/calibration
uv run --directory backend --locked --env-file ../.env --env-file ../.env.model.local python -m scripts.evaluate holdout --output ../eval/local/holdout
```

题集、评分方法、逐题记录、实跑报告和工作台演示见 [M4 对照评测](eval/README.md)。

开发文档见[项目索引](项目索引.md)。
