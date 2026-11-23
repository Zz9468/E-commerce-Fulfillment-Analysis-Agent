import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import groupby
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.queries import DeliveryComparison
from app.schemas import EffectiveQuery, ToolReply
from app.tools.delivery import TOOLS


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str
    turn: int = 0
    assistant_content: str | None = field(default=None, repr=False)
    reasoning_content: str | None = field(default=None, repr=False)

    def as_message(self) -> dict:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class Decision:
    calls: tuple[ToolCall, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()


class Model(Protocol):
    def choose(
        self,
        query: EffectiveQuery,
        question: str,
        replies: Sequence[tuple[ToolCall, ToolReply]],
        *,
        tools_enabled: bool,
    ) -> Decision: ...


class SelectedEvidence(BaseModel):
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=3)


_SYSTEM = (
    "你负责规划履约调查。服务端提供有效的目标月、基准月、指标和地区。"
    "只在这些条件内使用工具。先比较有效范围整体结果，再根据调查目标和实际证据决定是否查州、州内城市或阶段。"
    "样本不足时保留待查项。第三次工具请求后结束调查。"
    '停止时仅输出 JSON 对象 {"evidence_ids": ["实际获得的证据ID"]}，服务端会用实际数据生成答复。'
)


class ChatModel:
    def __init__(
        self,
        base_url: str,
        name: str,
        api_key: str | None,
        *,
        timeout: float = 20,
        reasoning_effort: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.name = name
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.transport = transport

    def choose(
        self,
        query: EffectiveQuery,
        question: str,
        replies: Sequence[tuple[ToolCall, ToolReply]],
        *,
        tools_enabled: bool,
    ) -> Decision:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": f"有效条件 {query.model_dump_json()}\n用户问题 {question}",
            },
        ]
        for _, batch in groupby(replies, lambda entry: entry[0].turn):
            entries = list(batch)
            first_call = entries[0][0]
            assistant_message = {
                "role": "assistant",
                "content": first_call.assistant_content,
                "tool_calls": [call.as_message() for call, _ in entries],
            }
            # Thinking models can require their assistant turn to be replayed verbatim.
            if first_call.reasoning_content is not None:
                assistant_message["reasoning_content"] = first_call.reasoning_content
            messages.append(assistant_message)
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": reply.model_dump_json(),
                }
                for call, reply in entries
            )
        payload = {"model": self.name, "messages": messages}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools_enabled:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": schema.model_json_schema(),
                    },
                }
                for name, (description, schema) in TOOLS.items()
            ]
            payload["tool_choice"] = "auto"
        with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
            response = client.post(self.url, json=payload, headers=self.headers)
            response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if tools_enabled and calls:
            return Decision(
                calls=tuple(
                    ToolCall(
                        str(call["id"]),
                        str(call["function"]["name"]),
                        call["function"]["arguments"],
                        assistant_content=message.get("content"),
                        reasoning_content=message.get("reasoning_content"),
                    )
                    for call in calls
                )
            )
        try:
            selected = SelectedEvidence.model_validate_json(
                message.get("content") or "{}"
            )
        except ValidationError:
            return Decision()
        return Decision(evidence_ids=tuple(selected.evidence_ids))


class FixedModel:
    """Replay the demonstration tool sequence without a model endpoint."""

    def choose(
        self,
        query: EffectiveQuery,
        question: str,
        replies: Sequence[tuple[ToolCall, ToolReply]],
        *,
        tools_enabled: bool,
    ) -> Decision:
        if not tools_enabled or (query.state and len(replies) >= 2):
            return Decision(
                evidence_ids=tuple(
                    reply.evidence.evidence_id for _, reply in replies if reply.evidence
                )
            )
        args = {
            "month": query.month,
            "baseline_month": query.baseline_month,
            "state": query.state,
        }
        name = "compare_delivery"
        if not replies:
            args.update(metric=query.metric, group_by="none")
        elif query.state:
            args.update(metric=query.metric, group_by="city")
        elif len(replies) == 1:
            args.update(metric=query.metric, group_by="state")
        else:
            evidence = replies[-1][1].evidence
            result = evidence.result if evidence else None
            if isinstance(result, DeliveryComparison) and any(
                row.region == "SP" for row in result.rows
            ):
                args.update(metric=query.metric, group_by="city", state="SP")
            else:
                name = "compare_delivery_stages"
        return Decision(
            calls=(ToolCall(f"fixed-{len(replies) + 1}", name, json.dumps(args)),)
        )
