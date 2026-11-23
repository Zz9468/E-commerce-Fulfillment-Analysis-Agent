import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app import queries
from app.main import create_app
from app.model import ChatModel, Decision, ToolCall
from scripts.seed_demo import seed_demo

HQ = {"Authorization": "Bearer demo-hq"}
SP = {"Authorization": "Bearer demo-sp"}
QUESTION = "2018年7月和8月超期率，排查新增超期集中在哪些地区"


@pytest.fixture
def seeded(migrated_database_url):
    result = seed_demo(migrated_database_url)
    assert seed_demo(migrated_database_url) == result
    return result.dataset_id


def ask(client, seeded, *, question=QUESTION, headers=HQ):
    response = client.post(
        "/api/analysis",
        headers=headers,
        json={"mode": "question", "question": question, "dataset_id": str(seeded)},
    )
    return response.status_code, response.json()


def test_rule_investigation_paths(query_database_urls, seeded):
    with TestClient(
        create_app(query_database_urls=query_database_urls, mode="rule")
    ) as client:
        status, concentrated = ask(client, seeded)
        assert status == 200
        assert concentrated["status"] == "answered"
        assert [step["parameters"]["group_by"] for step in concentrated["trace"]] == [
            "none",
            "state",
            "city",
        ]
        assert concentrated["trace"][-1]["parameters"]["state"] == "SP"
        assert (
            concentrated["evidence"][-1]["result"]["rows"][1]["late_count_delta"] == 5
        )
        _, stable = ask(client, seeded, question="2018年8月和9月超期率变化原因")
        assert stable["status"] == "answered"
        assert len(stable["trace"]) == 1
        _, dispersed = ask(client, seeded, question="2018年9月和10月超期率变化原因")
        assert dispersed["status"] == "answered"
        assert dispersed["trace"][-1]["tool"] == "compare_delivery_stages"
        _, local = ask(client, seeded, headers=SP)
        assert local["status"] == "answered"
        assert [step["parameters"]["group_by"] for step in local["trace"]] == [
            "none",
            "city",
        ]
        assert all(step["parameters"]["state"] == "SP" for step in local["trace"])


def test_stage_sql_timeout_keeps_completed_evidence(
    query_database_urls, seeded, monkeypatch
):
    compare_delivery_stages = queries.compare_delivery_stages

    def delay_stages(connection, **params):
        connection.execute("SELECT pg_sleep(2)")
        return compare_delivery_stages(connection, **params)

    monkeypatch.setenv("SQL_STATEMENT_TIMEOUT_MS", "200")
    monkeypatch.setattr(queries, "compare_delivery_stages", delay_stages)
    with TestClient(
        create_app(query_database_urls=query_database_urls, mode="rule")
    ) as client:
        status, direct = ask(client, seeded, question="2018年8月配送阶段耗时")
        assert status == 503 and direct["status"] == "unavailable"
        assert [step["status"] for step in direct["trace"]] == ["failed"]
        status, partial = ask(client, seeded, question="2018年9月和10月超期率变化原因")
        assert status == 200 and partial["status"] == "partial"
        assert [step["status"] for step in partial["trace"]] == ["ok", "ok", "failed"]
        assert len(partial["evidence"]) == 2
        drill = client.post(
            "/api/analysis",
            headers=HQ,
            json={
                "mode": "drilldown",
                "session_id": partial["session_id"],
                "source_evidence_id": partial["evidence"][1]["evidence_id"],
                "state": "SP",
            },
        )
        assert drill.status_code == 200 and drill.json()["status"] == "answered"


@pytest.mark.parametrize("fault", ["timeout", "duplicate"])
def test_model_failure_stops_with_available_evidence(
    query_database_urls, seeded, fault
):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if fault == "timeout" and calls == 2:
            assert request.extensions["timeout"]["read"] == 0.1
            raise httpx.ReadTimeout("model timed out", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": str(calls),
                                    "function": {
                                        "name": "compare_delivery",
                                        "arguments": json.dumps(
                                            {
                                                "month": "2018-08",
                                                "baseline_month": "2018-07",
                                                "metric": "late_rate",
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = ChatModel(
        "https://model.example/v1",
        "test",
        None,
        timeout=0.1,
        transport=httpx.MockTransport(respond),
    )
    with TestClient(
        create_app(query_database_urls=query_database_urls, model=model)
    ) as client:
        status, partial = ask(client, seeded)
    assert status == 200 and partial["status"] == "partial"
    assert calls == 2 and len(partial["evidence"]) == 1
    assert [step["status"] for step in partial["trace"]] == (
        ["ok", "duplicate"] if fault == "duplicate" else ["ok"]
    )


class RejectedModel:
    def __init__(self):
        self.sent = False

    def choose(self, query, question, replies, *, tools_enabled):
        if not tools_enabled:
            assert [reply.status for _, reply in replies] == [
                "scope_denied",
                "invalid",
                "scope_denied",
                "budget_exhausted",
            ]
            return Decision()
        assert not self.sent
        self.sent = True
        args = {
            "month": query.month,
            "baseline_month": query.baseline_month,
            "metric": "late_rate",
        }
        return Decision(
            calls=(
                ToolCall("1", "compare_delivery", json.dumps({**args, "state": "RJ"})),
                ToolCall("2", "compare_delivery", "invalid json"),
                ToolCall("3", "compare_delivery", json.dumps({**args, "state": "MG"})),
                ToolCall("4", "compare_delivery", json.dumps(args)),
            )
        )


def test_model_cannot_turn_rejections_into_evidence(query_database_urls, seeded):
    with TestClient(
        create_app(query_database_urls=query_database_urls, model=RejectedModel())
    ) as client:
        http_status, result = ask(client, seeded, headers=SP)
    assert http_status == 503
    assert result["status"] == "unavailable"
    assert len(result["trace"]) == 3
    assert not result["evidence"]


def test_chat_protocol_replays_real_tool_results_by_call_id(
    query_database_urls, seeded
):
    requests = []

    def mock(request):
        data = json.loads(request.content)
        requests.append(data)
        turn = len(requests)
        assert data["reasoning_effort"] == "low"
        if turn > 1:
            output = data["messages"][-1]
            assert output["tool_call_id"] == f"call-{turn - 1}"
            result = json.loads(output["content"])["evidence"]["result"]
            assert result["month"] == "2018-08"
            assert result["rows"]
            history = [
                message
                for message in data["messages"]
                if message["role"] == "assistant"
            ]
            assert [message["content"] for message in history] == [
                f"fixture_content-{index}" for index in range(1, turn)
            ]
            assert [message["reasoning_content"] for message in history] == [
                f"fixture_reasoning-{index}" for index in range(1, turn)
            ]
        if turn == 4:
            assert "tools" not in data
            message = {
                "content": '{"evidence_ids":["made-up"],"summary":"9999 单由某承运商造成"}'
            }
        else:
            assert len(data["tools"]) == 2
            group = ("none", "state", "city")[turn - 1]
            arguments = {
                "month": "2018-08",
                "baseline_month": "2018-07",
                "group_by": group,
                "metric": "late_rate",
            }
            if group == "city":
                arguments["state"] = "SP"
            message = {
                "content": f"fixture_content-{turn}",
                "reasoning_content": f"fixture_reasoning-{turn}",
                "tool_calls": [
                    {
                        "id": f"call-{turn}",
                        "type": "function",
                        "function": {
                            "name": "compare_delivery",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    adapter = ChatModel(
        "https://model.test/v1",
        "fixture-model",
        None,
        reasoning_effort="low",
        transport=httpx.MockTransport(mock),
    )
    with TestClient(
        create_app(query_database_urls=query_database_urls, model=adapter)
    ) as client:
        _, result = ask(client, seeded)
    assert [entry["status"] for entry in result["trace"]] == ["ok", "ok", "ok"]
    assert result["status"] == "answered"
    assert "9999" not in result["answer"]
    assert "fixture_reasoning" not in json.dumps(result)
    assert len(requests) == 4


def test_live_model_tool_round_trip(pytestconfig, query_database_urls, seeded):
    if not pytestconfig.getoption("--live-model"):
        pytest.skip("Pass --live-model to call the configured model endpoint.")
    for name in ("MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY"):
        if not os.getenv(name):
            pytest.fail(f"Set {name} before running --live-model.")

    with TestClient(
        create_app(query_database_urls=query_database_urls, mode="model")
    ) as client:
        assert client.get("/api/workspace", headers=HQ).json()["mode"] == "model"
        http_status, result = ask(client, seeded)

    print(
        json.dumps(
            {
                "model": os.environ["MODEL_NAME"],
                "http_status": http_status,
                "status": result["status"],
                "trace": result["trace"],
            }
        )
    )
    assert http_status == 200
    assert result["status"] in {"answered", "partial"}
    assert 2 <= len(result["trace"]) <= 3
    assert all(step["status"] == "ok" for step in result["trace"])
    assert {step["evidence_id"] for step in result["trace"]} == {
        item["evidence_id"] for item in result["evidence"]
    }
    overall = result["evidence"][0]["result"]
    assert overall["group_by"] == "none"
    assert overall["month"] == "2018-08"
    assert overall["baseline_month"] == "2018-07"
    row = overall["rows"][0]
    assert (row["baseline"]["numerator"], row["baseline"]["sample_count"]) == (2, 24)
    assert (row["current"]["numerator"], row["current"]["sample_count"]) == (8, 24)


def test_fixed_replay_starts_from_actual_evidence(query_database_urls, seeded):
    with TestClient(
        create_app(query_database_urls=query_database_urls, mode="fixed")
    ) as client:
        _, result = ask(client, seeded)
        assert result["status"] == "answered"
        assert [step["parameters"]["group_by"] for step in result["trace"]] == [
            "none",
            "state",
            "city",
        ]
        assert client.get("/api/workspace", headers=HQ).json()["mode"] == "fixed"
