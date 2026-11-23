from pathlib import Path
from uuid import uuid4

from psycopg import Connection

from app.analysis import ToolRun, analyze_resolved
from app.auth import DEMO_ACCOUNTS, Session
from app.import_data import import_dataset
from app.schemas import EffectiveQuery


def test_resolved_evaluation_uses_real_tools_without_reparsing(
    db: Connection, migrated_database_url: str
) -> None:
    fixtures = Path(__file__).with_name("fixtures")
    dataset = import_dataset(
        migrated_database_url,
        fixtures / "synthetic_orders.csv",
        fixtures / "synthetic_customers.csv",
        source="synthetic",
    )
    run = ToolRun(db, Session(uuid4(), DEMO_ACCOUNTS["demo-sp"], dataset.dataset_id))
    query = EffectiveQuery(
        dataset_id=dataset.dataset_id,
        month="2018-08",
        metric="late_rate",
        state="SP",
    )
    result = analyze_resolved(run, query, "只用于提供调查目标")
    assert result.effective_query == query
    assert result.status == "answered"
    assert len(result.trace) == 1
    assert "1/1" in result.answer

    denied = analyze_resolved(
        ToolRun(db, run.session), query.model_copy(update={"state": "RJ"}), ""
    )
    assert denied.status == "scope_denied"
    assert not denied.trace
