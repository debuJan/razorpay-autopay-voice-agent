"""End-to-end scenario tests: all 10 scenarios executed for real."""
from app.customers import get_customer
from app.policies import (
    build_scenarios,
    check_consent,
    check_privacy,
    check_safety,
    run_scenario,
)


def test_all_ten_scenarios_pass(db_path):
    scenarios = build_scenarios()
    assert len(scenarios) == 10
    for scenario in scenarios:
        customer = get_customer(db_path, scenario["customer_id"])
        result = run_scenario(db_path, customer, scenario["script"])

        assert result.outcome == scenario["expected_outcome"], scenario["name"]
        assert result.tool_calls == scenario["expected_tools"], scenario["name"]
        for forbidden in scenario["forbidden_tools"]:
            assert forbidden not in result.tool_calls, scenario["name"]
        assert check_privacy(result, customer) == [], scenario["name"]
        assert check_safety(result) == [], scenario["name"]
        assert check_consent(result, scenario["script"]) == [], scenario["name"]


def test_call_trace_is_persisted(db_path):
    from app.database import get_connection

    scenario = build_scenarios()[0]
    customer = get_customer(db_path, scenario["customer_id"])
    result = run_scenario(db_path, customer, scenario["script"])

    conn = get_connection(db_path)
    try:
        events = conn.execute(
            "SELECT type FROM call_events WHERE call_id = ? ORDER BY id", (result.call_id,)
        ).fetchall()
        types = [r["type"] for r in events]
    finally:
        conn.close()

    for expected in ("call_started", "identity_verified", "payment_explained", "outcome"):
        assert expected in types
