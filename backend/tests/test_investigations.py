import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

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


def test_rule_investigation_paths(migrated_database_url, seeded):
    with TestClient(create_app(migrated_database_url, mode="rule")) as client:
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


def test_model_cannot_turn_rejections_into_evidence(migrated_database_url, seeded):
    with TestClient(create_app(migrated_database_url, model=RejectedModel())) as client:
        http_status, result = ask(client, seeded, headers=SP)
    assert http_status == 503
    assert result["status"] == "unavailable"
    assert len(result["trace"]) == 3
    assert not result["evidence"]


def test_chat_protocol_replays_real_tool_results_by_call_id(
    migrated_database_url, seeded
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
    with TestClient(create_app(migrated_database_url, model=adapter)) as client:
        _, result = ask(client, seeded)
    assert [entry["status"] for entry in result["trace"]] == ["ok", "ok", "ok"]
    assert result["status"] == "answered"
    assert "9999" not in result["answer"]
    assert "fixture_reasoning" not in json.dumps(result)
    assert len(requests) == 4


def test_live_model_tool_round_trip(pytestconfig, migrated_database_url, seeded):
    if not pytestconfig.getoption("--live-model"):
        pytest.skip("Pass --live-model to call the configured model endpoint.")
    for name in ("MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY"):
        if not os.getenv(name):
            pytest.fail(f"Set {name} before running --live-model.")

    with TestClient(create_app(migrated_database_url, mode="model")) as client:
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


def test_fixed_replay_starts_from_actual_evidence(migrated_database_url, seeded):
    with TestClient(create_app(migrated_database_url, mode="fixed")) as client:
        _, result = ask(client, seeded)
        assert result["status"] == "answered"
        assert [step["parameters"]["group_by"] for step in result["trace"]] == [
            "none",
            "state",
            "city",
        ]
        assert client.get("/api/workspace", headers=HQ).json()["mode"] == "fixed"
