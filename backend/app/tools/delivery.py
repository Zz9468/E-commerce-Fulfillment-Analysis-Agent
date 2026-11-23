from datetime import date
from typing import Annotated
from uuid import UUID

from psycopg import Connection
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

from app import queries
from app.queries import DeliveryComparison, GroupBy, Metric, StageComparison


def valid_month(value: str) -> str:
    date.fromisoformat(f"{value}-01")
    return value


Month = Annotated[
    str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(valid_month)
]
StateCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]


class StageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    month: Month = Field(description="目标下单月份 YYYY-MM")
    baseline_month: Month | None = Field(None, description="基准下单月份 YYYY-MM")
    state: StateCode | None = Field(None, description="收货州代码，如 SP")


class DeliveryArgs(StageArgs):
    metric: Metric
    group_by: GroupBy = GroupBy.NONE

    @field_validator("metric", "group_by", mode="before")
    @classmethod
    def parse_enum(cls, value: str, info: ValidationInfo) -> Metric | GroupBy:
        enum = Metric if info.field_name == "metric" else GroupBy
        return enum(value)


ToolArgs = DeliveryArgs | StageArgs
ToolResult = DeliveryComparison | StageComparison

TOOLS = {
    "compare_delivery": (
        "统计单月或两月的送达订单数、超期率或平均送达天数。",
        DeliveryArgs,
    ),
    "compare_delivery_stages": (
        "统计交接前后两个阶段的 P50、P90 和样本数。",
        StageArgs,
    ),
}


def execute_delivery(
    connection: Connection,
    args: ToolArgs,
    dataset_id: UUID,
    authorized_state: str | None,
) -> ToolResult:
    query = (
        queries.compare_delivery
        if isinstance(args, DeliveryArgs)
        else queries.compare_delivery_stages
    )
    return query(
        connection,
        dataset_id=dataset_id,
        authorized_state=authorized_state,
        **args.model_dump(),
    )
