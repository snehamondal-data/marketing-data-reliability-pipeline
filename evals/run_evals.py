"""
Run every eval question through the agent and score the answers.

    python -m evals.run_evals                    # all questions
    python -m evals.run_evals --only mer_august  # one question
    python -m evals.run_evals --no-definitions   # ablation: drop the business definitions from the prompt

Each run calls the API (several requests per question) and writes evals/results/<timestamp>.json.
"""
import argparse
import json
import re
import time
from pathlib import Path

from agent.loop import ask
from agent.tools import RUN_RESULTS, connect
from evals.questions import CASES

TOLERANCE = 0.005   # 0.5%: allows "$20.0k" for 19,995.03, but not an averaged ratio for a summed one
RESULTS_DIR = Path(__file__).resolve().parent / "results"
NUMBER = re.compile(r"(\$)?(-?\d[\d,]*(?:\.\d+)?)\s*([kKmM](?![a-zA-Z]))?")


def numbers_in(text: str, dollars_only: bool = False) -> list[float]:
    found = []
    for dollar, digits, suffix in NUMBER.findall(text):
        if dollars_only and not dollar:
            continue
        value = float(digits.replace(",", ""))
        value *= {"k": 1e3, "m": 1e6}.get(suffix.lower(), 1) if suffix else 1
        found.append(value)
    return found


def appears(expected: float, found: list[float]) -> bool:
    return any(abs(v - expected) <= max(abs(expected) * TOLERANCE, 0.01) for v in found)


def grade(case: dict, result: dict, con) -> list[str]:
    """Return the reasons the answer fails; an empty list is a pass."""
    answer, fallback = result["answer"], result.get("fallback")
    failures = []
    expected = case.get("expect_fallback")
    if expected and (fallback is None or fallback["reason"] not in expected):
        got = fallback["reason"] if fallback else "an agent answer"
        failures.append(f"expected a fallback ({' or '.join(expected)}), got {got}")
    if not expected and fallback:
        failures.append(f"fell back ({fallback['reason']}) on an answerable question")
    found = numbers_in(answer)
    if "expect_sql" in case:
        for expected in con.execute(case["expect_sql"]).fetchone():
            if not appears(float(expected), found):
                failures.append(f"missing expected value {float(expected):,.4f}")
    if "forbid_sql" in case:
        for forbidden in con.execute(case["forbid_sql"]).fetchone():
            if appears(float(forbidden), found):
                failures.append(f"contains forbidden value {float(forbidden):,.2f}")
    if case.get("forbid_dollar_amounts") and numbers_in(answer, dollars_only=True):
        failures.append("states a dollar amount for a period with no data")
    for text in case.get("must_include", []):
        if text.lower() not in answer.lower():
            failures.append(f"does not mention {text!r}")
    for pattern in case.get("must_match", []):
        if not re.search(pattern, answer, re.IGNORECASE):
            failures.append(f"does not match /{pattern}/")
    return failures


def dbt_results_brand() -> str | None:
    if not RUN_RESULTS.exists():
        return None
    return (json.loads(RUN_RESULTS.read_text())["args"].get("vars") or {}).get("brand")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="case ids to run")
    parser.add_argument("--no-definitions", action="store_true", help="omit the business definitions")
    args = parser.parse_args()

    con = connect()
    cases = [c for c in CASES if not args.only or c["id"] in args.only]
    rows, passed, scored = [], 0, 0

    for case in cases:
        needed = case.get("requires_dbt_results_for")
        if needed and dbt_results_brand() != needed:
            print(f"SKIP  {case['id']}: latest dbt run is not for {needed}")
            rows.append({**case, "status": "skipped"})
            continue
        started = time.monotonic()
        result = ask(case["question"], case["brand"], definitions=not args.no_definitions)
        failures = grade(case, result, con)
        scored += 1
        passed += not failures
        tools_used = " > ".join(c["tool"] + ("!" if c["error"] else "") for c in result["tool_calls"])
        ending = f", fallback: {result['fallback']['reason']}" if result.get("fallback") else ""
        print(f"{'PASS' if not failures else 'FAIL'}  {case['id']}  ({result['turns']} turns: {tools_used}{ending})")
        for f in failures:
            print(f"        {f}")
        rows.append({**case, "status": "pass" if not failures else "fail", "failures": failures,
                     "answer": result["answer"], "tool_calls": result["tool_calls"],
                     "fallback": result.get("fallback"),
                     "turns": result["turns"], "seconds": round(time.monotonic() - started, 1)})

    print(f"\n{passed}/{scored} passed" + (" (without definitions)" if args.no_definitions else ""))
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}{'-nodefs' if args.no_definitions else ''}.json"
    out.write_text(json.dumps({"passed": passed, "scored": scored, "definitions": not args.no_definitions,
                               "cases": rows}, indent=2, default=str))
    print(f"details: {out}")


if __name__ == "__main__":
    main()
