import re
from dataclasses import dataclass
from datetime import date, timedelta

from app.queries import GroupBy, Metric
from app.schemas import AnalysisMetric, Intent, Ranking, UiDefaults

STATE_NAMES = {"SP": "圣保罗州", "RJ": "里约热内卢州", "MG": "米纳斯吉拉斯州"}
_STATE_CODES = (
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO"
)
_REGIONS = {
    "圣保罗市": ("SP", "sao paulo"),
    "里约热内卢市": ("RJ", "rio de janeiro"),
    "坎皮纳斯": ("SP", "campinas"),
    "圣保罗": ("SP", None),
    "里约": ("RJ", None),
    "米纳斯吉拉斯": ("MG", None),
}
_PERIOD = re.compile(
    r"(?<![0-9])(?:(?P<year>[0-9]{4})\s*(?:年\s*|[-/]))?(?P<month>[0-9]{1,2})\s*月"
    r"|(?<![0-9])(?P<iso>[0-9]{4}-[0-9]{2})(?![0-9])"
)
_STATE = re.compile(
    r"(?<![A-Za-z0-9])(" + _STATE_CODES.replace(" ", "|") + r")(?![A-Za-z0-9])"
)
_YEAR = re.compile(r"(?<![0-9])([0-9]{4})\s*年")
_PREVIOUS = re.compile(r"(?:与|和|比|对比)?\s*(?:上[一个]?月|前一个月)")
_CHANGE = re.compile(r"变差|变好|上升|下降|变慢|变快|增加|减少|慢了|恶化")
_INVESTIGATE = re.compile(
    r"排查|调查|追查|定位|集中|原因|为什么|从哪|优先查|异常|哪个阶段"
)
_COMPARE = re.compile(r"比较|对比|环比|两[个月期]{1,3}|差了|变化|(?:与|和).{0,16}比")
_SINGLE = re.compile(r"多少|只查|单月|数值|^查(?:看|询)?")


@dataclass(frozen=True)
class Period:
    year: int | None
    month: int

    def iso(self, year: int | None = None) -> str | None:
        known_year = self.year or year
        if known_year and 1 <= self.month <= 12:
            return f"{known_year:04d}-{self.month:02d}"
        return None


@dataclass(frozen=True)
class QuestionContext:
    year: int | None = None
    month: int | None = None
    baseline: Period | None = None
    metric: AnalysisMetric | None = None
    state: str | None = None
    city: str | None = None
    nationwide: bool = False
    group_by: GroupBy = GroupBy.NONE
    intent: Intent = "single"
    ranking: Ranking = "late_count"
    infer_previous: bool = False
    baseline_is_derived: bool = False


def _period(match: re.Match[str]) -> Period:
    if iso := match["iso"]:
        return Period(int(iso[:4]) or None, int(iso[5:]))
    return Period(int(match["year"] or 0) or None, int(match["month"]))


def _question_periods(
    question: str, prior: QuestionContext
) -> tuple[Period | None, Period | None]:
    matches = list(_PERIOD.finditer(question))
    if not matches:
        return None, None
    first = _period(matches[0])
    if len(matches) == 1:
        if (
            re.search(
                r"(?:^|[，,。；;\s])(?:和|与|比|对比|基准(?:月)?(?:是|为)?)\s*$",
                question[: matches[0].start()],
            )
            and prior.month is not None
        ):
            return None, first
        return first, None
    second = _period(matches[1])
    if first.year and not second.year:
        second = Period(first.year, second.month)
    elif second.year and not first.year:
        first = Period(second.year, first.month)
    connector = question[matches[0].end() : matches[1].start()]
    if re.search(r"比|相比", connector):
        return first, second
    return second, first


def _metric(question: str) -> AnalysisMetric | None:
    if re.search(
        r"阶段(?:耗时|时长|用时)|交接[前后]|揽收[前后]|P50|P90|中位", question, re.I
    ):
        return "stages"
    if re.search(r"超期|逾期|超时率|延迟率|晚到", question):
        return Metric.LATE_RATE
    if re.search(
        r"送达(?:的)?(?:订单|单数|单量|多少单)|签收(?:订单|单数|量)", question
    ):
        return Metric.DELIVERED_COUNT
    if re.search(r"平均.*(?:耗时|时长|天数|多久)|(?:耗时|时长|天数).*平均", question):
        return Metric.AVG_DELIVERY_DAYS
    if "阶段" in question and "哪个阶段" not in question:
        return "stages"
    return None


def _region(question: str) -> tuple[str | None, str | None, bool]:
    nationwide = bool(
        re.search(r"全国|全巴西|巴西整体|所有州|全部(?:地区|州)|全量", question)
    )
    for name, (state, city) in _REGIONS.items():
        if name in question:
            return state, city, nationwide
    match = _STATE.search(question.upper())
    return (match[1] if match else None), None, nationwide


def mentioned_states(question: str) -> set[str]:
    return set(_STATE.findall(question.upper())) | {
        state for name, (state, _) in _REGIONS.items() if name in question
    }


def resolve_question(
    question: str, prior: QuestionContext, defaults: UiDefaults, *, pending: bool
) -> tuple[QuestionContext, dict[str, str], list[str]]:
    target, baseline = _question_periods(question, prior)
    explicit_previous = bool(_PREVIOUS.search(question))
    year_match = _YEAR.search(question)
    explicit_year = (target.year if target else None) or (
        int(year_match[1]) or None if year_match and not baseline else None
    )
    explicit_metric = _metric(question)
    state, city, nationwide = _region(question)
    explicit_region = state is not None or nationwide or "本州" in question
    explicit_group = (
        GroupBy.CITY
        if city or re.search(r"按城市|各市|哪个城市|城市拆分|分城市", question)
        else GroupBy.STATE
        if re.search(r"按州|各州|哪个州|分州|州拆分", question)
        else None
    )
    stage_intent = explicit_metric == "stages"
    investigate = bool(_INVESTIGATE.search(question) and not stage_intent)
    change = bool(_CHANGE.search(question))
    compare = bool(baseline or explicit_previous or change or _COMPARE.search(question))
    if investigate:
        intent: Intent = "investigate"
    elif compare:
        intent = "compare"
    elif not pending and (_SINGLE.search(question) or (explicit_metric and target)):
        intent = "single"
    elif prior.intent == "investigate" and explicit_region and not pending:
        intent = "compare"
    else:
        intent = prior.intent

    sources: dict[str, str] = {}

    def pick(field: str, explicit, session, default):
        for source, value in (
            ("explicit", explicit),
            ("session", session),
            ("default", default),
        ):
            if value is not None:
                sources[field] = source
                return value
        return None

    year = pick("year", explicit_year, prior.year, defaults.year)
    month = pick("month", target.month if target else None, prior.month, defaults.month)
    if month is not None and not 1 <= month <= 12:
        month = None
    metric = pick("metric", explicit_metric, prior.metric, defaults.metric)
    effective_state = state if explicit_region else prior.state
    effective_city = city if explicit_region else prior.city
    if effective_state:
        sources["state"] = "explicit" if explicit_region else "session"
    national = nationwide if explicit_region else prior.nationwide
    if national:
        sources["state"] = "explicit" if explicit_region else "session"
    group = explicit_group or (
        GroupBy.NONE if explicit_region or (target and not pending) else prior.group_by
    )
    if metric == "stages":
        group = GroupBy.NONE
        effective_city = None
        if intent == "investigate":
            intent = "compare"
    elif effective_city:
        group = GroupBy.CITY
    ranking: Ranking = (
        "rate"
        if re.search(
            r"超期率.{0,12}(?:变化|上升|增幅).{0,8}(?:最大|最多)|百分点.*(?:最大|排序)",
            question,
        )
        else "late_count"
    )
    if not explicit_metric and not target and not explicit_group:
        ranking = prior.ranking

    derived = False
    allow_previous = (
        change or investigate or (prior.infer_previous and prior.intent == intent)
    )
    if intent == "single":
        baseline = None
    elif baseline:
        sources["baseline_month"] = "explicit"
    else:
        same_target = (year, month) == (prior.year, prior.month)
        former_baseline = (
            prior.baseline if not prior.baseline_is_derived or same_target else None
        )
        if former_baseline and former_baseline.iso(year) == (
            Period(year, month).iso() if month else None
        ):
            former_baseline = None
        default_baseline = None
        if defaults.baseline_month:
            default_baseline = Period(
                int(defaults.baseline_month[:4]), int(defaults.baseline_month[5:])
            )
        if not explicit_previous:
            baseline = pick("baseline_month", None, former_baseline, default_baseline)
            derived = (
                baseline is not None
                and baseline == former_baseline
                and prior.baseline_is_derived
            )
        if (
            baseline is None
            and (explicit_previous or allow_previous)
            and year
            and month
            and (year, month) != (1, 1)
        ):
            previous = date(year, month, 1) - timedelta(days=1)
            baseline = Period(previous.year, previous.month)
            sources["baseline_month"] = "previous_month"
            derived = True
    if baseline and not baseline.year and year:
        baseline = Period(year, baseline.month)
    if baseline and (
        not 1 <= baseline.month <= 12
        or (month and year and baseline.iso(year) == Period(year, month).iso())
    ):
        baseline = None

    missing = [
        field
        for field, value in (("year", year), ("month", month), ("metric", metric))
        if value is None
    ]
    if (
        intent != "single"
        and baseline is None
        and (not (allow_previous or explicit_previous) or (year and month))
    ):
        missing.append("baseline_month")

    return (
        QuestionContext(
            year,
            month,
            baseline,
            metric,
            effective_state,
            effective_city,
            national,
            group,
            intent,
            ranking,
            allow_previous or explicit_previous,
            derived,
        ),
        sources,
        missing,
    )
