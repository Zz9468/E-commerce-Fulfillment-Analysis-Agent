from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.import_data import import_dataset
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"
HQ = {"Authorization": "Bearer demo-hq"}
SP = {"Authorization": "Bearer demo-sp"}


@pytest.fixture
def client(migrated_database_url, query_database_urls):
    import_dataset(
        migrated_database_url,
        FIXTURES / "synthetic_orders.csv",
        FIXTURES / "synthetic_customers.csv",
        source="synthetic",
    )
    with TestClient(
        create_app(query_database_urls=query_database_urls, mode="rule")
    ) as client:
        yield client


def ask(client, text, *, headers=HQ, **fields):
    response = client.post(
        "/api/analysis",
        json={"mode": "question", "question": text, **fields},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_clarify_merge_and_comparison_once(client):
    reply = ask(client, "8月配送变差了吗？")
    assert set(reply["pending_fields"]) == {"year", "metric"}
    assert not reply["trace"]
    reply = ask(client, "2018年，超期率", session_id=reply["session_id"])
    assert reply["effective_query"]["baseline_month"] == "2018-07"
    assert (
        reply["effective_query"]["condition_sources"]["baseline_month"]
        == "previous_month"
    )
    assert len(reply["trace"]) == 1
    row = reply["evidence"][0]["result"]["rows"][0]
    assert (row["baseline"]["numerator"], row["baseline"]["sample_count"]) == (0, 1)
    assert (row["current"]["numerator"], row["current"]["sample_count"]) == (1, 2)
    reply = ask(
        client,
        "2018年7月已送达订单数是多少？",
        session_id=reply["session_id"],
        ui_defaults={"year": 2020, "month": 11, "metric": "late_rate"},
    )
    assert reply["effective_query"]["month"] == "2018-07"
    assert reply["effective_query"]["baseline_month"] is None
    assert reply["effective_query"]["metric"] == "delivered_count"
    assert len(reply["trace"]) == 1


def test_compare_only_when_another_period_can_be_resolved(client):
    reply = ask(client, "2018年8月超期率两月对比")
    assert reply["pending_fields"] == ["baseline_month"]
    assert not reply["trace"]
    reply = ask(client, "与7月比", session_id=reply["session_id"])
    assert reply["effective_query"]["month"] == "2018-08"
    assert reply["effective_query"]["baseline_month"] == "2018-07"
    assert len(reply["trace"]) == 1


def test_drilldown_uses_clicked_evidence_and_enforces_ownership(client):
    original = ask(client, "2018年7月和8月按州比较超期单数增量")
    ask(client, "2018年9月平均送达耗时是多少？", session_id=original["session_id"])
    payload = {
        "mode": "drilldown",
        "session_id": original["session_id"],
        "source_evidence_id": original["evidence"][0]["evidence_id"],
        "state": "SP",
    }
    result = client.post("/api/analysis", headers=HQ, json=payload).json()
    assert result["effective_query"]["month"] == "2018-08"
    assert result["effective_query"]["baseline_month"] == "2018-07"
    assert result["effective_query"]["metric"] == "late_rate"
    assert result["evidence"][0]["result"]["rows"][0]["region"] == "sao paulo"
    assert client.post("/api/analysis", headers=SP, json=payload).status_code == 404
    assert (
        client.post(
            "/api/analysis", headers=HQ, json={**payload, "state": "MG"}
        ).status_code
        == 404
    )
    other = ask(client, "2018年8月超期率", headers=SP)
    via_other_session = {**payload, "session_id": other["session_id"]}
    assert (
        client.post("/api/analysis", headers=SP, json=via_other_session).status_code
        == 404
    )


def test_authorized_range_and_stage_query(client):
    for question in (
        "2018年8月全国超期率",
        "2018年8月RJ州超期率",
        "2018年8月里约热内卢市超期率",
        "2018年8月SP和RJ超期率",
    ):
        reply = ask(client, question, headers=SP)
        assert reply["status"] == "scope_denied"
        assert not reply["trace"]
    local = ask(client, "2018年8月已送达订单数", headers=SP)
    assert local["effective_query"]["state"] == "SP"
    assert local["evidence"][0]["result"]["rows"][0]["current"]["value"] == 2
    stage = ask(client, "2018年8月配送阶段耗时")
    assert stage["status"] == "answered"
    assert [step["tool"] for step in stage["trace"]] == ["compare_delivery_stages"]
    assert stage["evidence"][0]["result"]["current"]["after_handoff"]["p90_days"] == 4
    assert (
        client.post(
            "/api/analysis", json={"mode": "question", "question": "8月超期率"}
        ).status_code
        == 401
    )


def test_sp_workspace_and_own_drilldown_stay_in_scope(client):
    hq = client.get("/api/workspace", headers=HQ).json()
    sp = client.get("/api/workspace", headers=SP).json()
    dataset_id = hq["default_dataset_id"]
    assert sp["default_dataset_id"] == dataset_id
    assert (
        next(item for item in hq["datasets"] if item["dataset_id"] == dataset_id)[
            "order_count"
        ]
        == 5
    )
    assert (
        next(item for item in sp["datasets"] if item["dataset_id"] == dataset_id)[
            "order_count"
        ]
        == 4
    )

    reply = ask(client, "2018年8月按州查已送达订单数", headers=SP)
    assert [row["region"] for row in reply["evidence"][0]["result"]["rows"]] == ["SP"]
    payload = {
        "mode": "drilldown",
        "session_id": reply["session_id"],
        "source_evidence_id": reply["evidence"][0]["evidence_id"],
    }
    outside = client.post("/api/analysis", headers=SP, json={**payload, "state": "RJ"})
    assert outside.status_code == 404
    inside = client.post("/api/analysis", headers=SP, json={**payload, "state": "SP"})
    assert inside.status_code == 200
    assert inside.json()["evidence"][0]["result"]["state"] == "SP"


def test_api_refuses_a_query_login_from_the_wrong_scope(query_database_urls):
    config = {**query_database_urls, "demo-sp": query_database_urls["demo-hq"]}
    with TestClient(create_app(query_database_urls=config, mode="rule")) as client:
        assert client.get("/api/workspace", headers=SP).status_code == 503
        reply = client.post(
            "/api/analysis",
            headers=SP,
            json={
                "mode": "question",
                "question": "2018年8月超期率",
            },
        )
    assert reply.status_code == 503
    assert reply.json()["status"] == "unavailable"
    assert reply.json()["session_id"] is None
    assert not reply.json()["evidence"]


def test_missing_handoff_and_successful_empty_stage_query(
    client, migrated_database_url, tmp_path
):
    original = (FIXTURES / "synthetic_orders.csv").read_text(encoding="utf-8")
    orders = tmp_path / "orders_without_handoff.csv"
    orders.write_text(
        "\n".join(line.rsplit(",", 1)[0] for line in original.splitlines()) + "\n",
        encoding="utf-8",
    )
    dataset_id = import_dataset(
        migrated_database_url,
        orders,
        FIXTURES / "synthetic_customers.csv",
        source="without-handoff",
    ).dataset_id
    missing = ask(client, "2018年8月配送阶段耗时", dataset_id=str(dataset_id))
    assert missing["status"] == "unsupported"
    assert not missing["evidence"][0]["result"]["supported"]
    empty = ask(client, "2018年9月配送阶段耗时")
    assert empty["status"] == "no_data"
    assert empty["evidence"][0]["result"]["supported"]
    assert empty["trace"][0]["status"] == "ok"


def test_no_data_zero_and_one_period_are_distinct(client):
    assert ask(client, "2018年9月已送达订单数")["status"] == "answered"
    assert ask(client, "2018年9月超期率")["status"] == "no_data"
    assert ask(client, "2018年9月比8月超期率")["status"] == "partial"
    small = ask(client, "2018年7月和8月超期率变化原因")
    assert small["status"] == "partial"
    assert len(small["trace"]) == 1
    assert "5 个有效样本" in small["answer"]


def test_carrier_attribution_reuses_only_confirmed_regional_evidence(client):
    fresh = ask(client, "是哪家快递的问题？")
    assert fresh["status"] == "unsupported"
    assert "承运商标识" in fresh["answer"] and "物流轨迹" in fresh["answer"]
    assert not fresh["trace"] and not fresh["evidence"]

    first = ask(client, "2018年7月和8月按州比较超期单数增量")
    follow_up = ask(client, "是哪家快递的问题？", session_id=first["session_id"])
    assert follow_up["status"] == "partial"
    assert follow_up["evidence"] == first["evidence"]
    assert not follow_up["trace"]
    assert "圣保罗州" in follow_up["answer"] and "物流轨迹" in follow_up["answer"]
    different = ask(
        client, "2019年8月是哪家快递的问题？", session_id=first["session_id"]
    )
    assert different["status"] == "unsupported" and not different["evidence"]


def test_explicit_conditions_replace_defaults_and_inferred_baseline(client):
    defaults = {"year": 2018, "month": 7, "metric": "late_rate"}
    first = ask(client, "8月比上个月超期率变差了吗？", ui_defaults=defaults)
    assert first["effective_query"]["month"] == "2018-08"
    assert first["effective_query"]["baseline_month"] == "2018-07"
    assert first["effective_query"]["condition_sources"]["year"] == "default"
    second = ask(
        client,
        "2019年1月变差了吗？",
        session_id=first["session_id"],
        ui_defaults=defaults,
    )
    assert second["effective_query"]["month"] == "2019-01"
    assert second["effective_query"]["baseline_month"] == "2018-12"
    invalid = ask(client, "2018年13月超期率", ui_defaults=defaults)
    assert invalid["pending_fields"] == ["month"]
    assert not invalid["trace"]
