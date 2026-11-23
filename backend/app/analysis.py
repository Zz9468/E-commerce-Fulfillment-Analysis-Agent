import logging
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

from psycopg import Connection, Error
from pydantic import ValidationError

from app.answers import FIELD_NAMES, answer_for, result_status, scope_name
from app.auth import Session
from app.conditions import (
    Period,
    QuestionContext,
    asks_carrier_attribution,
    mentioned_states,
    resolve_question,
)
from app.investigations import (
    evidence_highlight,
    model_investigation,
    rule_investigation,
)
from app.model import Model
from app.queries import DeliveryComparison, GroupBy
from app.schemas import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    DrilldownRequest,
    EffectiveQuery,
    Evidence,
    ToolReply,
    ToolStatus,
    TraceStep,
)
from app.tools.delivery import TOOLS, DeliveryArgs, execute_delivery

logger = logging.getLogger(__name__)
TOOL_BUDGET = 3


class ToolRun:
    def __init__(self, connection: Connection, session: Session) -> None:
        self.connection = connection
        self.session = session
        self.analysis_id: UUID = uuid4()
        self.trace: list[TraceStep] = []
        self.evidence: list[Evidence] = []
        self._seen: set[tuple[str, str]] = set()
        self.restrict_to: EffectiveQuery | None = None

    @property
    def remaining(self) -> int:
        return TOOL_BUDGET - len(self.trace)

    def execute(self, tool: str, raw_args: object) -> ToolReply:
        if not self.remaining:
            return ToolReply(status="budget_exhausted")
        start = perf_counter()
        parameters = None
        evidence = None
        definition = TOOLS.get(tool)
        try:
            if definition is None:
                raise ValueError("Unknown tool")
            model = definition[1]
            args = (
                model.model_validate_json(raw_args)
                if isinstance(raw_args, str)
                else model.model_validate(raw_args)
            )
            parameters = args.model_dump(mode="json")
            expected_state = self.session.account.state
            if not self.session.account.allows(args.state):
                raise PermissionError("State is outside the authorized scope")
            requested_state = self.restrict_to.state if self.restrict_to else None
            if requested_state and args.state not in (None, requested_state):
                raise PermissionError("State is outside the requested scope")
            if self.restrict_to and (args.month, args.baseline_month) != (
                self.restrict_to.month,
                self.restrict_to.baseline_month,
            ):
                raise ValueError("Months must match the requested period")
            args = args.model_copy(
                update={"state": args.state or requested_state or expected_state}
            )
            parameters = args.model_dump(mode="json")
            if (
                isinstance(args, DeliveryArgs)
                and args.group_by == GroupBy.CITY
                and args.state is None
            ):
                raise ValueError("City grouping requires a state")
            fingerprint = (tool, args.model_dump_json())
            if fingerprint in self._seen:
                status: ToolStatus = "duplicate"
            else:
                self._seen.add(fingerprint)
                result = execute_delivery(
                    self.connection, args, self.session.dataset_id, expected_state
                )
                evidence = Evidence(
                    evidence_id=uuid4(),
                    analysis_id=self.analysis_id,
                    tool=tool,
                    completed_at=datetime.now(UTC),
                    result=result,
                )
                self.evidence.append(evidence)
                self.session.evidence[evidence.evidence_id] = evidence
                status = "ok"
        except PermissionError:
            status = "scope_denied"
        except (ValidationError, ValueError, TypeError):
            status = "invalid"
        except (Error, LookupError) as exc:
            logger.warning(
                "analysis_id=%s tool=%s error=%s",
                self.analysis_id,
                tool,
                type(exc).__name__,
            )
            status = "failed"
        self.trace.append(
            TraceStep(
                sequence=len(self.trace) + 1,
                tool=tool,
                parameters=parameters,
                status=status,
                duration_ms=round((perf_counter() - start) * 1000, 2),
                evidence_id=evidence.evidence_id if evidence else None,
            )
        )
        return ToolReply(status=status, evidence=evidence)

    def query(self, query: EffectiveQuery) -> ToolReply:
        args = query.model_dump(
            mode="json", include={"month", "baseline_month", "state"}
        )
        if query.metric == "stages":
            return self.execute("compare_delivery_stages", args)
        return self.execute(
            "compare_delivery",
            {**args, "metric": query.metric, "group_by": query.group_by},
        )


def _drilldown_query(session: Session, request: DrilldownRequest) -> EffectiveQuery:
    evidence = session.get_evidence(request.source_evidence_id)
    result = evidence.result
    if not isinstance(result, DeliveryComparison) or result.group_by != GroupBy.STATE:
        raise LookupError("State evidence not found")
    if not any(row.region == request.state for row in result.rows):
        raise LookupError("State evidence not found")
    prior = session.analyses.get(evidence.analysis_id)
    ranking = (
        prior.effective_query.ranking
        if prior and prior.effective_query
        else "late_count"
    )
    return EffectiveQuery(
        dataset_id=session.dataset_id,
        month=result.month,
        baseline_month=result.baseline_month,
        metric=result.metric,
        state=request.state,
        group_by=GroupBy.CITY,
        intent="compare" if result.baseline_month else "single",
        ranking=ranking,
        condition_sources={"month": "evidence", "metric": "evidence"},
    )


def _status_for(reply: ToolReply, query: EffectiveQuery) -> AnalysisStatus:
    if reply.evidence:
        return result_status(reply.evidence, query.city)
    return "scope_denied" if reply.status == "scope_denied" else "unavailable"


def analyze(
    connection: Connection,
    session: Session,
    request: AnalysisRequest,
    model: Model | None = None,
) -> AnalysisResponse:
    with session.lock:
        run = ToolRun(connection, session)
        response = _analyze(run, request, model)
        session.analyses[response.analysis_id] = response
        return response


def _analyze(
    run: ToolRun, request: AnalysisRequest, model: Model | None
) -> AnalysisResponse:
    session = run.session
    base = {"session_id": session.session_id, "analysis_id": run.analysis_id}
    if isinstance(request, DrilldownRequest):
        query = _drilldown_query(session, request)
        if not session.account.allows(query.state):
            return _scope_denied(run)
    else:
        context, sources, missing = resolve_question(
            request.question,
            session.context,
            request.ui_defaults,
            pending=session.awaiting_clarification,
        )
        named_states = mentioned_states(request.question)
        if not session.account.allows(
            context.state, nationwide=context.nationwide
        ) or any(not session.account.allows(state) for state in named_states):
            return _scope_denied(run)
        if len(named_states) > 1:
            return AnalysisResponse(
                **base,
                status="unsupported",
                answer="一次提问可指定一个州。也可以查询全部地区并按州拆分。",
            )
        state = context.state or session.account.state
        if asks_carrier_attribution(request.question):
            return _carrier_attribution(run, context, state)
        if context.group_by == GroupBy.CITY and not state:
            missing.append("state")
        session.context = context
        session.awaiting_clarification = bool(missing)
        if missing:
            return AnalysisResponse(
                **base,
                status="needs_clarification",
                pending_fields=missing,
                answer="请补充"
                + "、".join(FIELD_NAMES[field] for field in missing)
                + "。",
            )
        query = EffectiveQuery(
            dataset_id=session.dataset_id,
            month=Period(context.year, context.month).iso(),
            baseline_month=context.baseline.iso() if context.baseline else None,
            metric=context.metric,
            state=state,
            group_by=context.group_by,
            city=context.city,
            intent=context.intent,
            ranking=context.ranking,
            condition_sources=sources,
        )

    note = ""
    if (
        query.intent == "investigate"
        and not query.city
        and not isinstance(request, DrilldownRequest)
    ):
        if model:
            status, note = model_investigation(run, query, request.question, model)
        else:
            status, note = rule_investigation(run, query, request.question)
    else:
        reply = run.query(query)
        status = _status_for(reply, query)
        if (
            reply.evidence
            and query.baseline_month
            and not query.city
            and isinstance(reply.evidence.result, DeliveryComparison)
        ):
            note = evidence_highlight(reply.evidence, query.ranking)
    return AnalysisResponse(
        **base,
        status=status,
        answer=answer_for(query, run.evidence, status, note),
        effective_query=query,
        evidence=run.evidence,
        trace=run.trace,
    )


def _scope_denied(run: ToolRun) -> AnalysisResponse:
    return AnalysisResponse(
        session_id=run.session.session_id,
        analysis_id=run.analysis_id,
        status="scope_denied",
        answer=f"当前账号可查范围为{scope_name(run.session.account.state)}。请在此范围内提问。",
    )


def _carrier_attribution(
    run: ToolRun, context: QuestionContext, state: str | None
) -> AnalysisResponse:
    month = Period(context.year, context.month).iso() if context.month else None
    baseline_month = context.baseline.iso() if context.baseline else None
    evidence = []
    for prior in reversed(run.session.analyses.values()):
        if not prior.effective_query or prior.effective_query.city != context.city:
            continue
        evidence = [
            item
            for item in prior.evidence
            if isinstance(item.result, DeliveryComparison)
            and item.result.group_by in {GroupBy.STATE, GroupBy.CITY}
            and item.result.month == month
            and item.result.baseline_month == baseline_month
            and (state is None or item.result.state == state)
            and result_status(item, context.city) in {"answered", "partial"}
        ]
        if evidence:
            break
    answer = "确认具体承运商的责任需要承运商标识和物流轨迹。"
    if evidence:
        item = evidence[-1]
        detail = (
            evidence_highlight(item, context.ranking)
            or f"已核实{scope_name(item.result.state)}的地区明细。"
        )
        answer = detail + answer
    return AnalysisResponse(
        session_id=run.session.session_id,
        analysis_id=run.analysis_id,
        status="partial" if evidence else "unsupported",
        answer=answer,
        evidence=evidence,
    )
