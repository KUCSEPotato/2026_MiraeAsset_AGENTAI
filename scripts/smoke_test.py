import argparse
import sys

import httpx

REQUIRED_FIELDS = {
    "question_id",
    "question",
    "retrieved_context",
    "think_trace",
    "answer",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluation API smoke test")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--question-id", default="Q-001")
    parser.add_argument("--question", default="평가 질의")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    response = httpx.get(
        f"{args.base_url.rstrip('/')}/answer",
        params={"question_id": args.question_id, "question": args.question},
        timeout=args.timeout,
    )
    response.raise_for_status()
    payload = response.json()

    if set(payload) != REQUIRED_FIELDS:
        raise AssertionError(f"unexpected fields: {set(payload)}")
    if not all(isinstance(value, str) for value in payload.values()):
        raise AssertionError("all response values must be strings")
    if payload["question_id"] != args.question_id:
        raise AssertionError("question_id was not preserved")
    if payload["question"] != args.question:
        raise AssertionError("question was not preserved")

    print("Smoke test passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (httpx.HTTPError, AssertionError, ValueError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        sys.exit(1)

