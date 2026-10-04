"""Tool tests: retry scheduling, secret isolation, call authorization, API secrecy."""
import json
from datetime import date, timedelta

import pytest

from app import tools, vapi
from app.config import Settings
from app.customers import get_customer, public_customer
from app.prompts import render_system_prompt
from app.tools import ToolError


def test_retry_valid_date_succeeds(db_path):
    future = (date.today() + timedelta(days=3)).isoformat()
    result = tools.schedule_retry(db_path, "CUST004", future)
    assert result == {"scheduled": True, "date": future}


def test_retry_more_than_7_days_fails(db_path):
    far = (date.today() + timedelta(days=8)).isoformat()
    with pytest.raises(ToolError):
        tools.schedule_retry(db_path, "CUST004", far)


def test_retry_past_date_fails(db_path):
    past = (date.today() - timedelta(days=1)).isoformat()
    with pytest.raises(ToolError):
        tools.schedule_retry(db_path, "CUST004", past)


def test_retry_bad_format_fails(db_path):
    with pytest.raises(ToolError):
        tools.schedule_retry(db_path, "CUST004", "next friday")


def test_transfer_to_human_is_simulated(db_path):
    result = tools.transfer_to_human(db_path, "CUST008", "hardship")
    assert result["transferred"] is True
    assert result["case_id"].startswith("CASE")


def test_log_outcome_rejects_unknown_outcome(db_path):
    with pytest.raises(ToolError):
        tools.log_outcome(db_path, "CUST001", "paid_in_full")


def test_verification_secret_never_reaches_llm(db_path, customer):
    secret = customer["verification_suffix"]
    prompt = render_system_prompt(customer)
    assert secret not in prompt

    settings = Settings(demo_phone_number="+91-DEMO-0000", public_base_url="https://x.test")
    public = public_customer(customer)
    overrides = vapi.build_assistant_overrides(public, settings)
    vapi.assert_no_secret_leak(overrides, secret)  # must not raise
    assert secret not in json.dumps(vapi.build_tool_definitions(settings))


def test_unauthorized_phone_number_cannot_be_called(db_path, customer):
    # No demo number configured -> refused before any HTTP is attempted.
    settings = Settings(vapi_api_key="k", vapi_assistant_id="a",
                        vapi_phone_number_id="p", demo_phone_number="")
    with pytest.raises(vapi.VapiError):
        vapi.create_outbound_call(public_customer(customer), settings)


def test_tool_calls_are_recorded(db_path):
    from app.database import get_connection, utcnow

    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO calls (call_id, customer_id, started_at) VALUES (?, ?, ?)",
            ("TESTCALL", "CUST001", utcnow()),
        )
        conn.commit()
    finally:
        conn.close()

    tools.verify_identity(db_path, "CUST001", "380", call_id="TESTCALL")
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT tool_name FROM tool_calls WHERE call_id = ?", ("TESTCALL",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["tool_name"] == "verify_identity"
