"""Sequential competition-API rehearsal with machine-readable output."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx


REQUIRED_FIELDS = {
    "question_id", "question", "retrieved_context", "think_trace", "answer"
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--cases", type=Path, default=Path("evaluation/m10_9_cases.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--retry-count", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=290.0)
    args = parser.parse_args()
    if args.repetitions < 1 or args.retry_count < 0:
        parser.error("repetitions must be positive and retry-count non-negative")

    base_url = args.base_url.rstrip("/")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    started = time.time()
    with httpx.Client(timeout=args.timeout) as client:
        health_before = _health(client, base_url)
        for repetition in range(args.repetitions):
            for case in cases:
                records.append(
                    _run_case(
                        client,
                        base_url,
                        case,
                        repetition=repetition,
                        retry_count=args.retry_count,
                    )
                )
        health_after = _health(client, base_url)

    by_category: dict[str, list[float]] = defaultdict(list)
    for record in records:
        if record["ok"]:
            by_category[record["category"]].append(record["latency_ms"])
    artifact = {
        "schema_version": "m10.9-rehearsal-v1",
        "base_url": base_url,
        "started_epoch": started,
        "duration_seconds": round(time.time() - started, 3),
        "health_before": health_before,
        "health_after": health_after,
        "request_count": len(records),
        "failure_count": sum(not record["ok"] for record in records),
        "performance_ms": {
            category: _latency_summary(values)
            for category, values in sorted(by_category.items())
        },
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: artifact[k] for k in (
        "request_count", "failure_count", "performance_ms"
    )}, ensure_ascii=False, indent=2))
    return 1 if artifact["failure_count"] else 0


def _run_case(
    client: httpx.Client,
    base_url: str,
    case: dict[str, str],
    *,
    repetition: int,
    retry_count: int,
) -> dict[str, Any]:
    question_id = case["id"]
    attempts = 0
    last_error = ""
    while attempts <= retry_count:
        attempts += 1
        started = time.perf_counter()
        try:
            response = client.get(
                f"{base_url}/answer",
                params={"question_id": question_id, "question": case["question"]},
            )
            latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
            if response.status_code >= 500:
                last_error = f"HTTP_{response.status_code}"
                continue
            response.raise_for_status()
            payload = response.json()
            _validate_payload(payload, question_id, case["question"])
            trace = _json_object(payload["think_trace"])
            context = _json_object(payload["retrieved_context"])
            return {
                "question_id": question_id,
                "category": case["category"],
                "repetition": repetition,
                "attempts": attempts,
                "latency_ms": latency_ms,
                "ok": True,
                "http_status": response.status_code,
                "semantic_fingerprint": {
                    "status": trace.get("status"),
                    "planner": trace.get("planner"),
                    "sources": trace.get("planning_summary", {}).get("sources", []),
                    "cardinality": trace.get("execution_cardinality", {}),
                    "reason_codes": trace.get("validation_summary", {}).get(
                        "reason_codes", []
                    ),
                    "llm_calls": trace.get("llm_call_summary", {}),
                    "stage_performance_ms": trace.get("performance_ms", {}),
                    "context_validation": context.get("validation", {}),
                },
            }
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
            last_error = type(exc).__name__
    return {
        "question_id": question_id,
        "category": case["category"],
        "repetition": repetition,
        "attempts": attempts,
        "latency_ms": latency_ms,
        "ok": False,
        "error": last_error,
    }


def _validate_payload(payload: Any, question_id: str, question: str) -> None:
    if not isinstance(payload, dict) or set(payload) != REQUIRED_FIELDS:
        raise ValueError("invalid response fields")
    if not all(isinstance(value, str) for value in payload.values()):
        raise ValueError("response values must all be strings")
    if payload["question_id"] != question_id or payload["question"] != question:
        raise ValueError("request identity was not preserved")


def _health(client: httpx.Client, base_url: str) -> dict[str, Any]:
    response = client.get(f"{base_url}/health")
    return {"http_status": response.status_code, "payload": response.json()}


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _latency_summary(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "sample_count": len(ordered),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3),
        "max": round(max(ordered), 3),
    }


if __name__ == "__main__":
    raise SystemExit(main())
