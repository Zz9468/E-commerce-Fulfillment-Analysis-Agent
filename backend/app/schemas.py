from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.queries import GroupBy, Metric
from app.tools.delivery import Month, StateCode, ToolResult

AnalysisStatus = Literal[
    "answered",
    "needs_clarification",
    "scope_denied",
    "no_data",
    "unsupported",
    "partial",
    "unavailable",
]
ToolStatus = Literal[
    "ok", "invalid", "scope_denied", "duplicate", "failed", "budget_exhausted"
]
AnalysisMode = Literal["rule", "fixed", "model"]
AnalysisMetric = Metric | Literal["stages"]
Intent = Literal["single", "compare", "investigate"]
Ranking = Literal["late_count", "rate"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UiDefaults(InputModel):
    year: int | None = Field(None, ge=1, le=9999, strict=True)
    month: int | None = Field(None, ge=1, le=12, strict=True)
    baseline_month: Month | None = None
    metric: AnalysisMetric | None = None


class QuestionRequest(InputModel):
    mode: Literal["question"]
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    session_id: UUID | None = None
    dataset_id: UUID | None = None
    ui_defaults: UiDefaults = Field(default_factory=UiDefaults)


class DrilldownRequest(InputModel):
    mode: Literal["drilldown"]
    session_id: UUID
    source_evidence_id: UUID
    state: StateCode


AnalysisRequest = Annotated[
    QuestionRequest | DrilldownRequest, Field(discriminator="mode")
]


class EffectiveQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_id: UUID
    month: Month
    baseline_month: Month | None = None
    metric: AnalysisMetric
    state: StateCode | None = None
    group_by: GroupBy = GroupBy.NONE
    city: str | None = None
    intent: Intent = "single"
    ranking: Ranking = "late_count"
    condition_sources: dict[str, str] = Field(default_factory=dict)


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: UUID
    analysis_id: UUID
    tool: str
    completed_at: datetime
    result: ToolResult


class TraceStep(BaseModel):
    sequence: int
    tool: str
    parameters: dict[str, str | None] | None
    status: ToolStatus
    duration_ms: float
    evidence_id: UUID | None = None


class ToolReply(BaseModel):
    status: ToolStatus
    evidence: Evidence | None = None


class AnalysisResponse(BaseModel):
    session_id: UUID
    analysis_id: UUID
    status: AnalysisStatus
    answer: str
    effective_query: EffectiveQuery | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    pending_fields: list[str] = Field(default_factory=list)
