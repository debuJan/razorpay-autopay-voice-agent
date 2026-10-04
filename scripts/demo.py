#!/usr/bin/env python3
"""Demo runner.

Mock mode (no phone call):
    python scripts/demo.py --mock

Live mode (calls ONLY the configured DEMO_PHONE_NUMBER via Vapi):
    python scripts/demo.py --live CUST001
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import vapi
from app.config import get_settings
from app.customers import get_customer, load_customers_json, seed_customers
from app.database import get_connection, init_db, utcnow
from app.policies import authorize_call, build_scenarios, dial_number, run_scenario


def run_mock() -> None:
    # Self-contained: temporary database, real scenario execution.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    init_db(db_path)
    seed_customers(db_path, load_customers_json())

    scenario = build_scenarios()[0]  # Scenario 1: successful recovery
    customer = get_customer(db_path, scenario["customer_id"])
    result = run_scenario(db_path, customer, scenario["script"])

    link_ok = "send_payment_link" in result.tool_calls
    print("========================================")
    print("AUTOPAY RECOVERY DEMO")
    print("========================================")
    print()
    print(f"Customer: {customer['customer_name']}")
    print(f"Merchant: {customer['merchant_name']}")
    print(f"Amount: \u20b9{customer['amount']:,}")
    print(f"Failure: {customer['failure_reason']}")
    print()
    print("Scenario:")
    print("Customer agrees to payment link.")
    print()
    print("Agent decision:")
    print("SEND PAYMENT LINK")
    print()
    print("Tool:")
    print("send_payment_link()")
    print()
    print("Result:")
    print("SUCCESS" if link_ok else "FAILED")
    print()
    print("Outcome:")
    print(result.outcome)
    print()
    print("========================================")

    os.unlink(db_path)


def run_live(customer_id: str) -> None:
    settings = get_settings()

    from app.config import default_db_path
    from app.customers import get_customer as _get

    db_path = default_db_path()
    init_db(db_path)
    customer = _get(db_path, customer_id)
    if customer is None:
        print(f"Unknown customer_id: {customer_id}")
        sys.exit(1)

    # 1-3: safety checks before anything is dialed.
    ok, reason = authorize_call(settings)
    if not ok:
        print(f"Refusing to call: {reason}")
        sys.exit(1)
    number = dial_number(settings)
    print(f"Authorized demo number: {number}")
    print("Calling window: OK (9 AM - 8 PM)")

    # 4: start the Vapi call.
    from app.customers import public_customer

    try:
        provider = vapi.create_outbound_call(public_customer(customer), settings)
    except vapi.VapiError as exc:
        print(f"Vapi call failed: {exc}")
        sys.exit(1)

    call_id = f"LIVE-{int(time.time())}"
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO calls (call_id, customer_id, provider_call_id, started_at)"
            " VALUES (?, ?, ?, ?)",
            (call_id, customer_id, provider["provider_call_id"], utcnow()),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"Provider call ID: {provider['provider_call_id']}")
    print("Waiting for the webhook/outcome (up to 3 minutes)...")

    # 5-6: wait for the Vapi webhook to record the outcome.
    deadline = time.time() + 180
    outcome = None
    while time.time() < deadline:
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT outcome FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row and row["outcome"]:
                outcome = row["outcome"]
                break
        finally:
            conn.close()
        time.sleep(5)

    print()
    print(f"Final outcome: {outcome or 'no webhook received yet — check /calls/' + call_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Autopay Recovery Voice Agent demo")
    parser.add_argument("--mock", action="store_true", help="Run the mock demo (no phone call).")
    parser.add_argument("--live", metavar="CUSTOMER_ID", help="Run the live Vapi demo.")
    args = parser.parse_args()

    if args.live:
        run_live(args.live)
    else:
        run_mock()


if __name__ == "__main__":
    main()
