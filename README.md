# 电商履约分析 Agent

面向电商和物流运营的本地分析工作台。用户可以用中文查询履约指标、对比两个月的表现，并根据查询证据追查地区或配送阶段。

## 本地运行

M1 提供 PostgreSQL 迁移、数据导入和指标查询。以下命令在项目根目录执行，需安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Docker Compose。依赖锁定在 [uv.lock](backend/uv.lock)，项目使用 Python 3.12。

```powershell
Copy-Item .env.example .env
docker compose up -d --wait db
uv sync --directory backend --locked
uv run --directory backend --locked --env-file ../.env python -m app.migrate
uv run --directory backend --locked --env-file ../.env python -m scripts.import_olist --source synthetic tests/fixtures/synthetic_orders.csv tests/fixtures/synthetic_customers.csv
```

已有 `.env` 时沿用现有配置。数据库默认监听 `127.0.0.1:55432`，迁移和导入读取 `DATABASE_URL`。导入成功后输出 `dataset_id` 和 `order_count`，重导同一来源和同一对文件会返回同一结果。

## 检查与验收

```powershell
uv run --directory backend --locked ruff check .
uv run --directory backend --locked ruff format --check .
uv run --directory backend --locked --env-file ../.env pytest -q
```

pytest 使用 `TEST_DATABASE_ADMIN_URL` 在每次运行时创建独立空库，应用迁移，并在结束时清理该次生成的测试库。使用自备 PostgreSQL 时，在 `.env` 中配置两个连接地址，测试连接的角色需能创建数据库。默认回归使用合成数据，无需模型密钥。

## 导入和复算 Olist

从 [Kaggle 的 Olist 数据集](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) 下载并解压原始文件，将 `olist_orders_dataset.csv` 和 `olist_customers_dataset.csv` 放到 `data/olist/`。

```powershell
uv run --directory backend --locked --env-file ../.env python -m scripts.import_olist ../data/olist/olist_orders_dataset.csv ../data/olist/olist_customers_dataset.csv
uv run --directory backend --locked --env-file ../.env pytest -q --olist-dir=../data/olist
```

带 `--olist-dir` 的回归会将原始文件导入隔离测试库，对照系统查询、[独立参考 SQL](eval/reference_sql/) 和已知验收值。样本数、阶段耗时与文件 SHA-256 见 [M1 验收记录](docs/里程碑.md#m1-把订单数据算对)。

开发文档见[项目索引](项目索引.md)。
