from itertools import combinations
from typing import Literal

from pydantic import Field

from app.analysis import TOOL_BUDGET
from app.queries import DeliveryComparison, StageComparison
from app.schemas import AnalysisResponse, AnalysisStatus, EffectiveQuery, InputModel
from scripts.evaluation.reference import Reference

Category = Literal["single", "compare", "investigate", "boundary"]


class Case(InputModel):
    id: str
    category: Category
    dataset: Literal["olist", "synthetic"]
    account: Literal["demo-hq", "demo-sp"]
    question: str
    status: AnalysisStatus
    query: dict | None = None
    paths: list[list[str]] = Field(min_length=1)
    pending_fields: list[str] = Field(default_factory=list)
    answer_has: list[str] = Field(default_factory=list)


def selector_for(
    result: DeliveryComparison | StageComparison, query: EffectiveQuery
) -> str:
    if isinstance(result, DeliveryComparison):
        selector = {"none": "overall", "state": "states", "city": "cities"}[
            result.group_by
        ]
        if result.group_by == "city":
            return f"cities:{result.state}"
    else:
        selector = "stages"
    if result.state and result.state != query.state:
        return f"{selector}:{result.state}"
    return selector


def score(
    case: Case,
    query: EffectiveQuery | None,
    response: AnalysisResponse,
    reference: Reference,
    overflow: int,
) -> dict:
    failures = []
    if response.status != case.status:
        failures.append("status")
    if set(response.pending_fields) != set(case.pending_fields):
        failures.append("clarification")
    if query != response.effective_query:
        failures.append("conditions")
    if any(fragment not in response.answer for fragment in case.answer_has):
        failures.append("answer")

    returned = {item.evidence_id: item for item in response.evidence}
    successful = [step for step in response.trace if step.status == "ok"]
    linked = {step.evidence_id for step in successful}
    if (
        len(linked) != len(successful)
        or linked != returned.keys()
        or any(item.analysis_id != response.analysis_id for item in returned.values())
    ):
        failures.append("evidence_link")

    path = []
    valid_indexes = []
    for step in successful:
        item = returned.get(step.evidence_id)
        if not item or query is None:
            failures.append("evidence")
            continue
        result = item.result
        selector = selector_for(result, query)
        path.append(selector)
        params = dict(
            month=result.month, baseline_month=result.baseline_month, state=result.state
        )
        if isinstance(result, DeliveryComparison):
            params.update(metric=result.metric, group_by=result.group_by)
        if (
            (query.state and result.state != query.state)
            or item.tool != step.tool
            or step.parameters != params
            or not reference.matches(result, query, selector)
        ):
            failures.append("reference")
        else:
            valid_indexes.append(len(path) - 1)

    best = ()
    path_pass = False
    valid = {path[index] for index in valid_indexes}
    for target in case.paths:
        matches = [
            indexes
            for indexes in combinations(valid_indexes, len(target))
            if [path[index] for index in indexes] == target
        ]
        if matches:
            best = max(best, matches[0], key=len)
            path_pass = True
    if not path_pass:
        # Credit evidence that contributes to an allowed route, even when a step is missing.
        best = max(
            (
                tuple(index for index in valid_indexes if path[index] in target)
                for target in case.paths
            ),
            key=len,
            default=(),
        )
    invalid_or_rejected = (
        sum(step.status not in {"ok", "failed"} for step in response.trace) + overflow
    )
    sql_attempts = sum(step.status in {"ok", "failed"} for step in response.trace)
    if len(response.trace) > TOOL_BUDGET or any(
        step.status == "scope_denied" for step in response.trace
    ):
        failures.append("guard")
    if not path_pass or (
        case.category != "investigate" and len(response.trace) != len(best)
    ):
        failures.append("path")
    if (
        case.category == "investigate"
        and case.paths == [["overall"]]
        and len(response.trace) != 1
    ):
        failures.append("stop")
    if query is not None and not (
        valid & {target[0] for target in case.paths if target}
    ):
        failures.append("primary")
    return {
        "passed": not failures,
        "failures": sorted(set(failures)),
        "path": path,
        "request_count": len(response.trace) + overflow,
        "sql_attempts": sql_attempts,
        "rejected_requests": invalid_or_rejected,
        "unhelpful_sql": sql_attempts - len(best),
    }
