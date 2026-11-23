"""Calibrate the rule baseline and compare it with the live model on frozen cases."""

import argparse
import json
import os
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import Connection

from app.analysis import TOOL_BUDGET, ToolRun, analyze, analyze_resolved
from app.auth import DEMO_ACCOUNTS, Session
from app.database import configured_query_urls, connect_for_query
from app.investigations import RULE_THRESHOLDS, RuleThresholds
from app.model import ChatModel
from app.queries import DELIVERY_METRIC_VERSION
from app.schemas import EffectiveQuery, Evidence, QuestionRequest
from scripts.evaluation.reference import EVAL_DIR, Reference
from scripts.evaluation.scoring import Case, score


class ModelTransport(httpx.BaseTransport):
    """Capture provider usage without writing requests, completions or credentials."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        record: dict = {}
        self.requests.append(record)
        start = perf_counter()
        try:
            with httpx.HTTPTransport() as transport:
                response = transport.handle_request(request)
                response.read()
            record["http_status"] = response.status_code
            if response.is_success:
                payload = response.json()
                record["model"] = payload.get("model")
                usage = payload.get("usage") or {}
                record["usage"] = {
                    key: usage[key]
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if type(usage.get(key)) is int
                }
            return response
        except httpx.HTTPError as exc:
            record["error"] = type(exc).__name__
            raise
        finally:
            record["duration_ms"] = round((perf_counter() - start) * 1000, 2)


def digest(path: Path) -> str:
    return sha256(path.read_text(encoding="utf-8").encode()).hexdigest()


def cases_for(name: str) -> list[Case]:
    path = EVAL_DIR / "cases" / f"{name}.jsonl"
    cases = [
        Case.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError(f"Duplicate case ID in {name}")
    expected = (
        {"single": 10, "compare": 10, "investigate": 12, "boundary": 8}
        if name == "holdout"
        else {"single": 4, "compare": 4, "investigate": 8, "boundary": 4}
    )
    if Counter(case.category for case in cases) != expected:
        raise ValueError(f"Unexpected distribution in {name}")
    return cases


def dataset_ids(connection: Connection, frozen: dict) -> dict[str, UUID]:
    versions = {}
    for source, expected in frozen.items():
        row = connection.execute(
            """
            SELECT dataset_id, order_count, has_carrier_handoff
            FROM dataset_version
            WHERE source = %s AND orders_sha256 = %s AND customers_sha256 = %s
            """,
            (source, expected["orders_sha256"], expected["customers_sha256"]),
        ).fetchone()
        if not row or row[1:] != (expected["order_count"], True):
            raise ValueError(
                f"Import the frozen {source} dataset before running evaluation"
            )
        versions[source] = row[0]
    return versions


def evidence_receipt(item: Evidence) -> dict:
    result = item.model_dump(mode="json")["result"]
    checksum = sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    rows = result.pop("rows", None)
    return {
        "id": str(item.evidence_id),
        "result_sha256": checksum,
        "result": result,
        "row_count": len(rows) if rows is not None else None,
        "sample_rows": rows[:1] if rows is not None else None,
    }


def run_case(
    case: Case,
    manifest: dict,
    versions: dict[str, UUID],
    urls: dict[str, str],
    reference: Reference,
    *,
    model: ChatModel | None = None,
    transport: ModelTransport | None = None,
    thresholds: RuleThresholds = RULE_THRESHOLDS,
) -> dict:
    dataset = versions[case.dataset]
    session = Session(uuid4(), DEMO_ACCOUNTS[case.account], dataset)
    query = (
        EffectiveQuery(dataset_id=dataset, **case.query)
        if case.query is not None
        else None
    )
    start = perf_counter()
    if transport:
        transport.requests.clear()
    with connect_for_query(
        urls.get(case.account), case.account, **manifest["sql"]
    ) as connection:
        run = ToolRun(connection, session)
        if query is None:
            response = analyze(
                connection,
                session,
                QuestionRequest(mode="question", question=case.question),
                model,
            )
        else:
            response = analyze_resolved(
                run, query, case.question, model, thresholds=thresholds
            )
    elapsed = round((perf_counter() - start) * 1000, 2)
    grading = score(case, query, response, reference, run.overflow_requests)
    model_requests = list(transport.requests) if transport else []
    return {
        "id": case.id,
        "category": case.category,
        "account": case.account,
        "dataset": case.dataset,
        "dataset_id": str(dataset),
        "question": case.question,
        "effective_query": query.model_dump(mode="json") if query else None,
        "expected": {"status": case.status, "paths": case.paths},
        "status": response.status,
        "answer": response.answer,
        "pending_fields": response.pending_fields,
        "analysis_id": str(response.analysis_id),
        "trace": [step.model_dump(mode="json") for step in response.trace],
        "evidence": [evidence_receipt(item) for item in response.evidence],
        "duration_ms": elapsed,
        "model_requests": model_requests,
        **grading,
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def policy(thresholds: RuleThresholds) -> dict:
    return dict(
        min_change_pp=str(thresholds.min_change_pp),
        min_samples=thresholds.min_samples,
        concentration=str(thresholds.concentration),
    )


def calibrate(run, cases: list[Case], output: Path) -> None:
    investigations = [case for case in cases if case.category == "investigate"]
    incumbent = RULE_THRESHOLDS
    candidates = [incumbent] + [
        item
        for values in product(("1", "2", "3"), (1, 5, 20), ("0.50", "0.60", "0.85"))
        if (item := RuleThresholds(Decimal(values[0]), values[1], Decimal(values[2])))
        != incumbent
    ]
    trials = []
    for thresholds in candidates:
        results = [run(case, thresholds=thresholds) for case in investigations]
        trials.append(
            {
                "thresholds": policy(thresholds),
                "passed": sum(row["passed"] for row in results),
                "total": len(results),
                "sql_attempts": sum(row["sql_attempts"] for row in results),
                "failures": {
                    row["id"]: row["failures"] for row in results if not row["passed"]
                },
            }
        )
    best_index = max(
        range(len(trials)),
        key=lambda index: (trials[index]["passed"], -trials[index]["sql_attempts"]),
    )
    best = candidates[best_index]
    development = [run(case, thresholds=best) for case in cases]
    write_json(
        output / "calibration.json",
        {
            "selected": trials[best_index],
            "trials": trials,
            "development": development,
        },
    )
    print(json.dumps(trials[best_index], ensure_ascii=False), flush=True)


def summarize(rows: list[dict]) -> dict:
    counts = {
        category: {
            "passed": sum(row["passed"] for row in rows if row["category"] == category),
            "total": sum(row["category"] == category for row in rows),
        }
        for category in ("single", "compare", "investigate", "boundary")
    }
    answerable = [row for row in rows if row["expected"]["status"] == "answered"]
    requests = [item for row in rows for item in row["model_requests"]]
    usage = [
        item["usage"] for item in requests if "total_tokens" in item.get("usage", {})
    ]
    tokens = {
        key: sum(item.get(key, 0) for item in usage)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return {
        "by_category": counts,
        "accuracy": {
            "passed": sum(row["passed"] for row in answerable),
            "total": len(answerable),
        },
        "sql_attempts": sum(row["sql_attempts"] for row in rows),
        "tool_requests": sum(row["request_count"] for row in rows),
        "rejected_requests": sum(row["rejected_requests"] for row in rows),
        "unhelpful_sql": sum(row["unhelpful_sql"] for row in rows),
        "model_requests": len(requests),
        "model_requests_with_usage": len(usage),
        "provider_models": sorted(
            {item["model"] for item in requests if item.get("model")}
        ),
        "tokens": tokens,
        "mean_total_tokens_per_case": round(tokens["total_tokens"] / len(rows), 2)
        if len(usage) == len(requests)
        else None,
        "p95_ms": round(
            quantiles([row["duration_ms"] for row in rows], n=20, method="inclusive")[
                18
            ],
            2,
        ),
        "failures": {row["id"]: row["failures"] for row in rows if not row["passed"]},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("calibrate", "holdout"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest_path = EVAL_DIR / "freeze.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for file, expected in manifest.get("sha256", {}).items():
        if digest(EVAL_DIR / file) != expected:
            raise ValueError(f"Frozen input changed: {file}")
    if (
        manifest["metric_version"] != DELIVERY_METRIC_VERSION
        or manifest["tool_budget"] != TOOL_BUDGET
    ):
        raise ValueError("Metric version or budget does not match frozen configuration")
    case_set = "development" if args.command == "calibrate" else "holdout"
    cases = cases_for(case_set)
    model = transport = None
    if args.command == "holdout":
        if not manifest.get("sha256") or manifest["thresholds"] != policy(
            RULE_THRESHOLDS
        ):
            raise ValueError(
                "Freeze the inputs and apply the selected thresholds first"
            )
        if not os.environ.get("MODEL_API_KEY"):
            raise ValueError("Set MODEL_API_KEY for the frozen model endpoint")
        transport = ModelTransport()
        model = ChatModel(
            **manifest["model"],
            api_key=os.environ["MODEL_API_KEY"],
            transport=transport,
        )
    urls = configured_query_urls()
    args.output.mkdir(parents=True)
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        autocommit=True,
        options="-c default_transaction_read_only=on",
    ) as connection:
        versions = dataset_ids(connection, manifest["datasets"])
        reference = Reference(connection, manifest["metric_version"])
        write_json(
            args.output / "run.json",
            {
                "started_at_utc": datetime.now(UTC).isoformat(),
                "command": args.command,
                "dataset_ids": {
                    source: str(version) for source, version in versions.items()
                },
                "postgres_version": connection.info.server_version,
                "freeze_sha256": digest(manifest_path),
                "configuration": manifest,
                "implementation_sha256": {
                    str(file.relative_to(EVAL_DIR.parent)).replace("\\", "/"): digest(
                        file
                    )
                    for package in ("app", "scripts")
                    for file in sorted(
                        (EVAL_DIR.parent / "backend" / package).rglob("*.py")
                    )
                },
            },
        )

        def run(case: Case, **options) -> dict:
            return run_case(case, manifest, versions, urls, reference, **options)

        if args.command == "calibrate":
            calibrate(run, cases, args.output)
            return
        summary = {}
        for mode, planner, probe in (("rule", None, None), ("model", model, transport)):
            rows = []
            with (args.output / f"{mode}.jsonl").open("x", encoding="utf-8") as stream:
                for case in cases:
                    row = run(case, model=planner, transport=probe)
                    rows.append(row)
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    print(
                        mode,
                        case.id,
                        row["status"],
                        ",".join(row["failures"]) or "pass",
                        flush=True,
                    )
            summary[mode] = summarize(rows)
        write_json(args.output / "summary.json", summary)


if __name__ == "__main__":
    main()
