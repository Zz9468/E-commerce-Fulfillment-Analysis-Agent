from pathlib import Path

import pytest

from app.analysis import ToolRun
from app.auth import DEMO_ACCOUNTS, SessionStore
from app.import_data import import_dataset

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def dataset(migrated_database_url):
    return import_dataset(
        migrated_database_url,
        FIXTURES / "synthetic_orders.csv",
        FIXTURES / "synthetic_customers.csv",
        source="synthetic",
    ).dataset_id


def test_tool_budget_counts_rejections_and_scope(db, dataset):
    session = SessionStore().create(DEMO_ACCOUNTS["demo-sp"], dataset)
    run = ToolRun(db, session)
    args = {"month": "2018-08", "metric": "delivered_count"}

    assert (
        run.execute("compare_delivery", {**args, "state": "RJ"}).status
        == "scope_denied"
    )
    assert (
        run.execute("compare_delivery", {**args, "authorized_state": "RJ"}).status
        == "invalid"
    )
    result = run.execute("compare_delivery", args)
    assert result.status == "ok"
    assert result.evidence.result.state == "SP"
    assert result.evidence.result.rows[0].current.value == 2
    assert (
        run.execute("compare_delivery_stages", {"month": "2018-08"}).status
        == "budget_exhausted"
    )
    assert [step.sequence for step in run.trace] == [1, 2, 3]
    assert len(run.evidence) == 1


def test_scope_normalizes_duplicate_calls_and_session_ownership(db, dataset):
    store = SessionStore()
    sp = DEMO_ACCOUNTS["demo-sp"]
    hq = DEMO_ACCOUNTS["demo-hq"]
    session = store.create(sp, dataset)
    unrelated = store.create(sp, dataset)
    run = ToolRun(db, session)
    args = {"month": "2018-08", "metric": "late_rate"}
    item = run.execute("compare_delivery", args).evidence
    assert (
        run.execute("compare_delivery", {**args, "state": "SP"}).status == "duplicate"
    )
    assert session.get_evidence(item.evidence_id) == item
    with pytest.raises(LookupError):
        store.get(hq, session.session_id)
    with pytest.raises(LookupError):
        unrelated.get_evidence(item.evidence_id)
