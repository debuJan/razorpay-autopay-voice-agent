"""Policy tests: identity, privacy, consent, calling safety."""
from datetime import datetime, timezone

import pytest

from app import tools
from app.config import Settings
from app.customers import get_customer
from app.policies import (
    CustomerScript,
    authorize_call,
    build_scenarios,
    check_consent,
    check_privacy,
    check_safety,
    dial_number,
    run_scenario,
    within_calling_window,
)


def test_correct_verification_succeeds(db_path):
    assert tools.verify_identity(db_path, "CUST001", "380")["verified"] is True


def test_incorrect_verification_fails(db_path):
    assert tools.verify_identity(db_path, "CUST001", "000")["verified"] is False


def test_second_verification_failure_blocks_access(db_path):
    customer = get_customer(db_path, "CUST001")
    script = CustomerScript(99, verification_answers=["111", "222"], intent="pay_now")
    result = run_scenario(db_path, customer, script)
    assert result.outcome == "verification_failed"
    assert "send_payment_link" not in result.tool_calls


def test_wrong_person_no_disclosure(db_path):
    customer = get_customer(db_path, "CUST009")
    scenario = build_scenarios()[8]
    result = run_scenario(db_path, customer, scenario["script"])
    assert result.outcome == "wrong_person"
    assert check_privacy(result, customer) == []
    texts = " ".join(result.agent_messages)
    assert str(customer["amount"]) not in texts
    assert customer["plan_name"] not in texts


def test_dispute_no_payment_link(db_path):
    customer = get_customer(db_path, "CUST001")
    script = CustomerScript(98, verification_answers=["380"], intent="dispute",
                            transfer_reason="dispute")
    result = run_scenario(db_path, customer, script)
    assert result.outcome == "dispute"
    assert "send_payment_link" not in result.tool_calls


def test_already_paid_no_payment_action(db_path):
    scenario = build_scenarios()[5]
    customer = get_customer(db_path, scenario["customer_id"])
    result = run_scenario(db_path, customer, scenario["script"])
    assert result.outcome == "claims_paid"
    assert "send_payment_link" not in result.tool_calls
    assert "schedule_retry" not in result.tool_calls


def test_do_not_call_immediate_termination(db_path):
    scenario = build_scenarios()[9]
    customer = get_customer(db_path, scenario["customer_id"])
    result = run_scenario(db_path, customer, scenario["script"])
    assert result.outcome == "do_not_call"
    assert result.tool_calls == ["verify_identity", "log_outcome"]


def test_consent_required_before_payment_link(db_path):
    customer = get_customer(db_path, "CUST001")
    scenario = build_scenarios()[5]  # claims_paid scenario
    result = run_scenario(db_path, customer, scenario["script"])
    assert check_consent(result, scenario["script"]) == []

    # A forged result where the link went out without consent must be flagged.
    forged = CustomerScript(97, intent="claims_paid")
    result.tool_calls.append("send_payment_link")
    assert check_consent(result, forged) != []


def test_calling_window():
    noon = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    night = datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc)
    # 12:00 UTC and 23:30 UTC are used as-is for the window check here.
    assert within_calling_window(noon) is True
    assert within_calling_window(night) is False


def test_authorize_call_requires_demo_number():
    ok, _ = authorize_call(Settings(demo_phone_number=""))
    assert ok is False
    with pytest.raises(tools.ToolError):
        dial_number(Settings(demo_phone_number=""))


def test_no_sensitive_requests_in_any_scenario(db_path):
    for scenario in build_scenarios():
        customer = get_customer(db_path, scenario["customer_id"])
        result = run_scenario(db_path, customer, scenario["script"])
        assert check_safety(result) == [], scenario["name"]
