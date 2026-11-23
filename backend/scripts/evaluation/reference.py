"""Build expected evidence from independent SQL and the frozen query conditions."""

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from math import isclose
from pathlib import Path
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from app.queries import DeliveryComparison, StageComparison
from app.schemas import EffectiveQuery

EVAL_DIR = Path(__file__).resolve().parents[3] / "eval"


def equal(actual: object, expected: object) -> bool:
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(actual, tuple) and isinstance(expected, tuple):
        return len(actual) == len(expected) and all(
            equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    if isinstance(actual, Decimal | float) and isinstance(expected, Decimal | float):
        return isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10)
    return actual == expected


class Reference:
    def __init__(self, connection: Connection, version: str) -> None:
        self.connection = connection
        self.version = version
        self._rows: dict[tuple, list[dict]] = {}
        self.statements = {
            name: (EVAL_DIR / "reference_sql" / f"{name}.sql").read_text(
                encoding="utf-8"
            )
            for name in ("delivery", "stages")
        }

    def rows(
        self,
        name: str,
        dataset: UUID,
        month: str,
        baseline: str | None,
        state: str | None,
    ) -> list[dict]:
        key = (name, dataset, month, baseline, state)
        if key in self._rows:
            return self._rows[key]
        months = [
            date.fromisoformat(f"{period}-01") for period in (month, baseline) if period
        ]
        with self.connection.cursor(row_factory=dict_row) as cursor:
            self._rows[key] = cursor.execute(
                self.statements[name],
                {"dataset_id": dataset, "months": months, "state": state},
            ).fetchall()
        return self._rows[key]

    def expected(self, query: EffectiveQuery, selector: str) -> dict:
        kind, _, region = selector.partition(":")
        state = region or query.state
        base = dict(
            dataset_id=query.dataset_id,
            month=query.month,
            baseline_month=query.baseline_month,
            state=state,
            metric_version=self.version,
        )
        if kind == "stages":
            rows = self.rows(
                "stages", query.dataset_id, query.month, query.baseline_month, state
            )
            periods = {row["month"].strftime("%Y-%m"): row for row in rows}

            def stage(period: str) -> dict:
                row = periods.get(period, {})
                return {
                    f"{name}_handoff": dict(
                        p50_days=row.get(f"{name}_p50"),
                        p90_days=row.get(f"{name}_p90"),
                        sample_count=row.get("sample_count", 0),
                        excluded_count=row.get("excluded_count", 0),
                    )
                    for name in ("before", "after")
                }

            return dict(
                **base,
                supported=True,
                current=stage(query.month),
                baseline=stage(query.baseline_month) if query.baseline_month else None,
            )

        group = {"overall": "none", "states": "state", "cities": "city"}[kind]
        rows = self.rows(
            "delivery", query.dataset_id, query.month, query.baseline_month, None
        )
        groups: dict[str | None, dict] = {None: {}} if group == "none" else {}
        for row in rows:
            level = row["region_level"]
            if group == "none":
                if level != (1 if state else 3) or row["state_code"] != state:
                    continue
                label = None
            elif group == "state":
                if level != 1 or (state and row["state_code"] != state):
                    continue
                label = row["state_code"]
            else:
                if level != 0 or row["state_code"] != state:
                    continue
                label = row["city_name"]
            groups.setdefault(label, {})[row["month"].strftime("%Y-%m")] = row

        def stats(row: dict) -> dict:
            match query.metric:
                case "late_rate":
                    return dict(
                        value=row.get("late_rate"),
                        sample_count=row.get("late_samples", 0),
                        numerator=row.get("late", 0),
                    )
                case "delivered_count":
                    delivered = row.get("delivered", 0)
                    return dict(value=delivered, sample_count=delivered, numerator=None)
                case "avg_delivery_days":
                    return dict(
                        value=row.get("avg_days"),
                        sample_count=row.get("avg_samples", 0),
                        numerator=None,
                    )
                case _:
                    raise ValueError("Unknown delivery metric")

        comparisons = []
        for label in sorted(groups, key=lambda name: (name is None, name or "")):
            by_period = groups[label]
            current = stats(by_period.get(query.month, {}))
            baseline = (
                stats(by_period.get(query.baseline_month, {}))
                if query.baseline_month
                else None
            )
            delta = pp = late_delta = None
            if baseline is not None:
                if baseline["value"] is not None and current["value"] is not None:
                    delta = current["value"] - baseline["value"]
                if query.metric == "late_rate":
                    pp = delta * 100 if delta is not None else None
                    delta = None
                    late_delta = current["numerator"] - baseline["numerator"]
            comparisons.append(
                dict(
                    region=label,
                    current=current,
                    baseline=baseline,
                    delta=delta,
                    delta_pp=pp,
                    late_count_delta=late_delta,
                )
            )
        return dict(
            **base, metric=query.metric, group_by=group, rows=tuple(comparisons)
        )

    def matches(
        self,
        actual: DeliveryComparison | StageComparison,
        query: EffectiveQuery,
        selector: str,
    ) -> bool:
        data = asdict(actual)
        if isinstance(actual, DeliveryComparison):
            data["rows"] = tuple(
                sorted(
                    data["rows"],
                    key=lambda row: (row["region"] is None, row["region"] or ""),
                )
            )
        return equal(data, self.expected(query, selector))
