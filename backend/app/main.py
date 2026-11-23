import os
from datetime import datetime
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import Connection, Error
from pydantic import BaseModel

from app.analysis import analyze
from app.auth import DEMO_ACCOUNTS, Account, SessionStore
from app.model import ChatModel, FixedModel, Model
from app.schemas import AnalysisMode, AnalysisRequest, AnalysisResponse, QuestionRequest

_bearer = HTTPBearer(auto_error=False)


def get_account(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Account:
    if credentials and (account := DEMO_ACCOUNTS.get(credentials.credentials)):
        return account
    raise HTTPException(401, "请选择演示账号", headers={"WWW-Authenticate": "Bearer"})


Identity = Annotated[Account, Depends(get_account)]


class DatasetInfo(BaseModel):
    dataset_id: UUID
    source: str
    imported_at: datetime
    covered_months: list[str]
    order_count: int
    has_carrier_handoff: bool


class Workspace(BaseModel):
    account: Account
    mode: AnalysisMode
    default_dataset_id: UUID | None
    datasets: list[DatasetInfo]


def _datasets(connection: Connection, state: str | None) -> list[DatasetInfo]:
    records = connection.execute(
        """
        SELECT dataset_id, source, imported_at, covered_months, order_count, has_carrier_handoff
        FROM dataset_version ORDER BY imported_at DESC, dataset_id
        """
    ).fetchall()
    scoped = {}
    if state:
        scoped = {
            dataset: (months, count)
            for dataset, months, count in connection.execute(
                """
                SELECT dataset_id, array_agg(DISTINCT date_trunc('month', purchased_at)::date
                    ORDER BY date_trunc('month', purchased_at)::date), count(*)
                FROM order_fact WHERE state_code = %s GROUP BY dataset_id
                """,
                (state,),
            ).fetchall()
        }
    datasets = []
    for dataset, source, imported, months, count, has_handoff in records:
        visible_months, visible_count = (
            scoped.get(dataset, ([], 0)) if state else (months, count)
        )
        datasets.append(
            DatasetInfo(
                dataset_id=dataset,
                source=source,
                imported_at=imported,
                covered_months=[month.isoformat()[:7] for month in visible_months],
                order_count=visible_count,
                has_carrier_handoff=has_handoff,
            )
        )
    return datasets


def create_app(
    database_url: str | None = None,
    *,
    mode: AnalysisMode | None = None,
    model: Model | None = None,
) -> FastAPI:
    app = FastAPI(title="电商履约分析 Agent")
    store = SessionStore()
    mode = mode or ("model" if model else os.getenv("ANALYSIS_MODE", "rule"))
    if mode == "fixed" and model is None:
        model = FixedModel()
    elif mode == "model" and model is None:
        model = ChatModel(
            os.environ["MODEL_BASE_URL"],
            os.environ["MODEL_NAME"],
            os.getenv("MODEL_API_KEY"),
            timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "20")),
            reasoning_effort=os.getenv("MODEL_REASONING_EFFORT"),
        )
    elif mode not in {"rule", "fixed", "model"}:
        raise ValueError("ANALYSIS_MODE must be rule, fixed or model")

    def connect() -> Connection:
        conninfo = database_url or os.getenv("DATABASE_URL")
        if not conninfo:
            raise HTTPException(503, "数据库尚未配置")
        return psycopg.connect(conninfo, autocommit=True, connect_timeout=5)

    @app.get("/api/workspace")
    def workspace(account: Identity) -> Workspace:
        try:
            with connect() as connection:
                datasets = _datasets(connection, account.state)
        except Error as exc:
            raise HTTPException(503, "数据库暂不可用") from exc
        source = os.getenv("DEFAULT_DATASET_SOURCE", "synthetic")
        default = next(
            (item.dataset_id for item in datasets if item.source == source), None
        )
        return Workspace(
            account=account, mode=mode, default_dataset_id=default, datasets=datasets
        )

    @app.post("/api/analysis", responses={503: {"model": AnalysisResponse}})
    def post_analysis(
        request: AnalysisRequest, account: Identity, response: Response
    ) -> AnalysisResponse:
        try:
            session = (
                store.get(account, request.session_id) if request.session_id else None
            )
            with connect() as connection:
                if isinstance(request, QuestionRequest):
                    if session is None:
                        if request.dataset_id:
                            record = connection.execute(
                                "SELECT dataset_id FROM dataset_version WHERE dataset_id = %s",
                                (request.dataset_id,),
                            ).fetchone()
                        else:
                            record = connection.execute(
                                "SELECT dataset_id FROM dataset_version WHERE source = %s ORDER BY imported_at DESC, dataset_id LIMIT 1",
                                (os.getenv("DEFAULT_DATASET_SOURCE", "synthetic"),),
                            ).fetchone()
                        if record is None:
                            raise LookupError("Dataset not found")
                        session = store.create(account, record[0])
                    if request.dataset_id and session.dataset_id != request.dataset_id:
                        raise HTTPException(422, "切换数据版本时请开启新会话")
                result = analyze(connection, session, request, model)
        except LookupError as exc:
            raise HTTPException(404, "会话、证据或数据版本不可访问") from exc
        except Error as exc:
            raise HTTPException(503, "数据库暂不可用") from exc
        if result.status == "unavailable":
            response.status_code = 503
        return result

    return app


app = create_app()
