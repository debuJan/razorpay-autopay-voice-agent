#!/usr/bin/env python3
"""Evaluate all 10 scenarios by ACTUALLY executing them.

Each scenario runs through the real policy engine and backend tools against a
fresh temporary database. Nothing is hardcoded: outcomes, tool sequences,
privacy/safety/consent checks and metrics are all computed from real runs.

SCOPE NOTE — what this evaluates and what it does NOT:
- It evaluates the DETERMINISTIC backend/policy/tool layer: the scripted
  scenario engine (`app/policies.py::run_scenario`) driving the real tools
  (`app/tools.py`) with scripted customer intents. It checks outcome
  correctness, tool selection, argument validity, privacy (no disclosure
  before verification), safety (no sensitive-credential requests), and
  consent (no payment action without the customer's choice).
- It does NOT evaluate the LLM's actual conversational behavior. A live Vapi
  call involves the real model (prompt rendering, interruptions, ASR errors,
  tool-call decisions), which this script cannot observe. Live-call behavior
  must be reviewed from real call recordings, transcripts, and the call
  traces at GET /calls/{call_id}/trace.

    python scripts/evaluate.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.customers import get_customer, load_customers_json, seed_customers
from app.database import init_db
from app.policies import (
    build_scenarios,
    check_consent,
    check_privacy,
    check_safety,
    run_scenario,
)


def evaluate_scenario(scenario: dict) -> dict:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        init_db(db_path)
        seed_customers(db_path, load_customers_json())
        customer = get_customer(db_path, scenario["customer_id"])
        result = run_scenario(db_path, customer, scenario["script"])

        failures: list[str] = []

        # Outcome check
        if result.outcome != scenario["expected_outcome"]:
            failures.append(
                f"outcome: expected {scenario['expected_outcome']!r}, got {result.outcome!r}"
            )

        # Tool selection check
        if result.tool_calls != scenario["expected_tools"]:
            failures.append(
                f"tools: expected {scenario['expected_tools']}, got {result.tool_calls}"
            )
        for forbidden in scenario["forbidden_tools"]:
            if forbidden in result.tool_calls:
                failures.append(f"forbidden tool used: {forbidden}")

        # Privacy / safety / consent checks
        for v in check_privacy(result, customer):
            failures.append(f"privacy: {v}")
        for v in check_safety(result):
            failures.append(f"safety: {v}")
        for v in check_consent(result, scenario["script"]):
            failures.append(f"consent: {v}")

        # Scenario-specific checks
        if scenario["id"] == 3 and "send_payment_link" in result.tool_calls:
            failures.append("scenario 3: unnecessary tool call made")
        if scenario["id"] == 5:
            texts = " ".join(result.agent_messages).lower()
            if "ai assistant" not in texts:
                failures.append("scenario 5: agent did not identify as AI")
        if scenario["id"] == 9:
            texts = " ".join(result.agent_messages)
            for secret in (str(customer["amount"]), customer["plan_name"], customer["last4"]):
                if secret in texts:
                    failures.append(f"scenario 9: disclosed {secret!r} to wrong person")

        return {
            "id": scenario["id"],
            "name": scenario["name"],
            "passed": not failures,
            "failures": failures,
            "tool_calls": result.tool_calls,
            "outcome": result.outcome,
            "privacy_violations": [f for f in failures if f.startswith("privacy:")],
            "safety_violations": [f for f in failures if f.startswith("safety:")],
            "consent_violations": [f for f in failures if f.startswith("consent:")],
        }
    finally:
        os.unlink(db_path)


def main() -> int:
    scenarios = build_scenarios()
    results = [evaluate_scenario(s) for s in scenarios]

    print("==================================================")
    print("AUTOPAY RECOVERY AGENT EVALUATION")
    print("==================================================")
    print()
    print("Scope: deterministic backend/policy/tool layer (NOT the LLM's live")
    print("conversational behavior). See module docstring for details.")
    print()
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"Scenario {r['id']:<2} {r['name']:<18} {status}")
        for f in r["failures"]:
            print(f"           - {f}")
    print()
    print("--------------------------------------------------")
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    print(f"Total: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print("--------------------------------------------------")
    print()

    safety = sum(len(r["safety_violations"]) for r in results)
    privacy = sum(len(r["privacy_violations"]) for r in results)
    unauthorized = sum(len(r["consent_violations"]) for r in results)
    links_sent = sum(1 for r in results if "send_payment_link" in r["tool_calls"])
    escalations = sum(1 for r in results if "transfer_to_human" in r["tool_calls"])
    retries = sum(1 for r in results if "schedule_retry" in r["tool_calls"])
    dnc = sum(1 for r in results if r["outcome"] == "do_not_call")

    print(f"Safety Violations: {safety}")
    print(f"Unauthorized Payment Actions: {unauthorized}")
    print(f"Privacy Violations: {privacy}")
    print("==================================================")
    print()
    print("Demo metrics (computed from actual results):")
    print(f"Total scenarios: {total}")
    print(f"Passed scenarios: {passed}")
    print(f"Failed scenarios: {failed}")
    print(f"Safety violations: {safety}")
    print(f"Privacy violations: {privacy}")
    print(f"Unauthorized payment actions: {unauthorized}")
    print(f"Payment links sent: {links_sent}")
    print(f"Human escalations: {escalations}")
    print(f"Retry schedules: {retries}")
    print(f"Do-not-call requests: {dnc}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
