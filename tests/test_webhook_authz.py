"""Server-side authorization + Vapi webhook tests.

Covers: unverified-call rejection for payment tools, wrong-customer
rejection, webhook shared-secret auth, toolCallList/toolCalls payloads,
JSON-string argument parsing, single-line JSON results, and the
provider-call-ID -> internal CALL-XXXX mapping.
"""
import json
from datetime import date, timedelta

import pytest

from app import tools
from app.customers import load_customers_json, seed_customers
from app.database import get_connection, init_db, utcnow
from app.tools import ToolError


def _insert_call(db_path, call_id, customer_id, provider_call_id=None, verified=0):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO calls (call_id, customer_id, provider_call_id, started_at, verified)"
            " VALUES (?, ?, ?, ?, ?)",
            (call_id, customer_id, provider_call_id, utcnow(), verified),
        )
        conn.commit()
    finally:
        conn.close()


def _call_verified(db_path, call_id):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT verified FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
    finally:
        conn.close()
    return bool(row["verified"])


# ---------------------------------------------------------------------------
# Tool-level authorization
# ---------------------------------------------------------------------------

def test_unverified_call_cannot_send_payment_link(db_path):
    _insert_call(db_path, "CALL-U1", "CUST001")
    with pytest.raises(ToolError, match="not verified"):
        tools.send_payment_link(db_path, "CUST001", "sms", "pay_now", call_id="CALL-U1")


def test_unverified_call_cannot_schedule_retry(db_path):
    _insert_call(db_path, "CALL-U2", "CUST004")
    future = (date.today() + timedelta(days=3)).isoformat()
    with pytest.raises(ToolError, match="not verified"):
        tools.schedule_retry(db_path, "CUST004", future, call_id="CALL-U2")


def test_verified_call_can_send_payment_link(db_path):
    _insert_call(db_path, "CALL-V1", "CUST001")
    assert tools.verify_identity(db_path, "CUST001", "380", call_id="CALL-V1")["verified"] is True
    assert _call_verified(db_path, "CALL-V1")
    result = tools.send_payment_link(db_path, "CUST001", "sms", "pay_now", call_id="CALL-V1")
    assert result["sent"] is True
    # link_id is UUID-based, not a timestamp
    assert result["payment_link_id"].startswith("LINK")
    assert len(result["payment_link_id"]) == len("LINK") + 8


def test_wrong_customer_tool_call_rejected(db_path):
    # Call is bound to CUST001; tools invoked for CUST002 must be refused.
    _insert_call(db_path, "CALL-W1", "CUST001")
    with pytest.raises(ToolError, match="does not match"):
        tools.verify_identity(db_path, "CUST002", "915", call_id="CALL-W1")
    with pytest.raises(ToolError, match="does not match"):
        tools.send_payment_link(db_path, "CUST002", "sms", "pay_now", call_id="CALL-W1")
    with pytest.raises(ToolError, match="does not match"):
        tools.log_outcome(db_path, "CUST002", "no_resolution", call_id="CALL-W1")


def test_unknown_call_id_rejected(db_path):
    with pytest.raises(ToolError, match="Unknown call_id"):
        tools.verify_identity(db_path, "CUST001", "380", call_id="NOPE")


def test_case_id_is_uuid_based(db_path):
    result = tools.transfer_to_human(db_path, "CUST008", "hardship")
    assert result["case_id"].startswith("CASE")
    assert len(result["case_id"]) == len("CASE") + 8


# ---------------------------------------------------------------------------
# Webhook fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def webhook_client(tmp_path, monkeypatch):
    """TestClient with seeded DB and NO webhook secret (fail-open dev mode)."""
    db_file = str(tmp_path / "webhook.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
    init_db(db_file)
    seed_customers(db_file, load_customers_json())
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client, db_file


@pytest.fixture()
def authed_webhook_client(tmp_path, monkeypatch):
    """TestClient with seeded DB and VAPI_WEBHOOK_SECRET configured."""
    db_file = str(tmp_path / "webhook-auth.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "test-secret-123")
    init_db(db_file)
    seed_customers(db_file, load_customers_json())
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client, db_file


def _tool_calls_payload(provider_call_id, calls, key="toolCallList"):
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": provider_call_id},
            key: [
                {
                    "id": f"tc-{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for i, (name, args) in enumerate(calls)
            ],
        }
    }


# ---------------------------------------------------------------------------
# Webhook authentication
# ---------------------------------------------------------------------------

def test_webhook_rejects_missing_secret(authed_webhook_client):
    client, _ = authed_webhook_client
    resp = client.post("/webhooks/vapi", json={"message": {"type": "status-update"}})
    assert resp.status_code == 401


def test_webhook_rejects_wrong_secret(authed_webhook_client):
    client, _ = authed_webhook_client
    resp = client.post(
        "/webhooks/vapi",
        json={"message": {"type": "status-update"}},
        headers={"X-Webhook-Secret": "wrong"},
    )
    assert resp.status_code == 401


def test_webhook_accepts_correct_secret(authed_webhook_client):
    client, _ = authed_webhook_client
    resp = client.post(
        "/webhooks/vapi",
        json={"message": {"type": "status-update"}},
        headers={"X-Webhook-Secret": "test-secret-123"},
    )
    assert resp.status_code == 200


def test_webhook_open_without_configured_secret(webhook_client):
    # Fail-open dev mode (documented in limitations.md).
    client, _ = webhook_client
    resp = client.post("/webhooks/vapi", json={"message": {"type": "status-update"}})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# toolCallList parsing, JSON-string args, single-line JSON results, ID mapping
# ---------------------------------------------------------------------------

def test_webhook_tool_call_list_with_json_string_args(webhook_client):
    client, db_file = webhook_client
    _insert_call(db_file, "CALL-AUTH-1", "CUST001", provider_call_id="vapi-call-abc123")

    payload = _tool_calls_payload(
        "vapi-call-abc123",
        [("verify_identity", {"customer_id": "CUST001", "answer": "380"})],
    )
    resp = client.post("/webhooks/vapi", json=payload)
    assert resp.status_code == 200

    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["toolCallId"] == "tc-0"
    raw = results[0]["result"]
    # Results are single-line JSON strings, not dicts.
    assert isinstance(raw, str) and "\n" not in raw
    assert json.loads(raw) == {"verified": True}

    # The internal call record was marked verified...
    assert _call_verified(db_file, "CALL-AUTH-1")
    # ...and the tool call was recorded against the INTERNAL call id,
    # never the Vapi provider id.
    conn = get_connection(db_file)
    try:
        row = conn.execute(
            "SELECT call_id, tool_name FROM tool_calls ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["call_id"] == "CALL-AUTH-1"
    assert row["tool_name"] == "verify_identity"


def test_webhook_tool_calls_fallback_key(webhook_client):
    client, db_file = webhook_client
    _insert_call(db_file, "CALL-AUTH-2", "CUST001", provider_call_id="vapi-call-old")
    payload = _tool_calls_payload(
        "vapi-call-old",
        [("verify_identity", {"customer_id": "CUST001", "answer": "380"})],
        key="toolCalls",  # older Vapi field name
    )
    resp = client.post("/webhooks/vapi", json=payload)
    assert resp.status_code == 200
    assert json.loads(resp.json()["results"][0]["result"]) == {"verified": True}


def test_webhook_unverified_call_cannot_send_payment_link(webhook_client):
    client, db_file = webhook_client
    _insert_call(db_file, "CALL-AUTH-3", "CUST001", provider_call_id="vapi-call-unver")
    payload = _tool_calls_payload(
        "vapi-call-unver",
        [("send_payment_link", {"customer_id": "CUST001", "channel": "sms",
                                "purpose": "pay_now"})],
    )
    resp = client.post("/webhooks/vapi", json=payload)
    assert resp.status_code == 200
    err = json.loads(resp.json()["results"][0]["result"])
    assert "error" in err and "not verified" in err["error"]


def test_webhook_binds_customer_to_internal_call(webhook_client):
    client, db_file = webhook_client

    # Call is bound to CUST001, but the model tries to target CUST002.
    # The webhook must ignore the model-supplied customer_id and bind
    # the tool call to the customer associated with the internal call.
    _insert_call(
        db_file,
        "CALL-AUTH-4",
        "CUST001",
        provider_call_id="vapi-call-wrong",
    )

    payload = _tool_calls_payload(
        "vapi-call-wrong",
        [
            (
                "log_outcome",
                {
                    "customer_id": "CUST002",
                    "outcome": "no_resolution",
                },
            )
        ],
    )

    resp = client.post("/webhooks/vapi", json=payload)

    assert resp.status_code == 200

    result = json.loads(resp.json()["results"][0]["result"])

    assert result == {
        "logged": True,
        "outcome": "no_resolution",
    }


def test_webhook_unknown_provider_call_rejected(webhook_client):
    client, _ = webhook_client
    payload = _tool_calls_payload(
        "vapi-call-unknown",
        [("log_outcome", {"customer_id": "CUST001", "outcome": "no_resolution"})],
    )
    resp = client.post("/webhooks/vapi", json=payload)
    assert resp.status_code == 200
    err = json.loads(resp.json()["results"][0]["result"])
    assert "error" in err and "Unknown call" in err["error"]


def test_end_of_call_report_resolves_internal_call(webhook_client):
    client, db_file = webhook_client
    _insert_call(db_file, "CALL-AUTH-5", "CUST001", provider_call_id="vapi-call-eoc")

    # A tool call first, so the trace has content.
    verify_payload = _tool_calls_payload(
        "vapi-call-eoc",
        [("verify_identity", {"customer_id": "CUST001", "answer": "380"})],
    )
    assert client.post("/webhooks/vapi", json=verify_payload).status_code == 200

    report = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "vapi-call-eoc"},
            "summary": "Customer verified; call ended.",
            "endedReason": "assistant-ended-call",
        }
    }
    resp = client.post("/webhooks/vapi", json=report)
    assert resp.status_code == 200
    assert resp.json()["matched"] is True

    # Outcome landed on the INTERNAL call record...
    conn = get_connection(db_file)
    try:
        call = conn.execute(
            "SELECT outcome, summary FROM calls WHERE call_id = ?", ("CALL-AUTH-5",)
        ).fetchone()
        outcome = conn.execute(
            "SELECT outcome FROM outcomes WHERE call_id = ?", ("CALL-AUTH-5",)
        ).fetchone()
    finally:
        conn.close()
    assert call["outcome"] == "no_resolution"
    assert outcome["outcome"] == "no_resolution"

    # ...and the trace endpoint serves it under the internal CALL id.
    trace = client.get("/calls/CALL-AUTH-5/trace")
    assert trace.status_code == 200
    body = trace.json()
    assert body["call_id"] == "CALL-AUTH-5"
    assert body["customer_id"] == "CUST001"
    types = [e["type"] for e in body["events"]]
    assert "tool_call" in types and "outcome" in types


def test_public_base_url_required_for_tool_definitions():
    from app import vapi
    from app.config import Settings

    settings = Settings(public_base_url="")
    with pytest.raises(vapi.VapiError, match="PUBLIC_BASE_URL"):
        vapi.build_tool_definitions(settings)


def test_assistant_overrides_include_server_config():
    from app import vapi
    from app.config import Settings

    settings = Settings(public_base_url="https://tunnel.test")
    overrides = vapi.build_assistant_overrides(
        {"customer_id": "CUST001", "customer_name": "Test",
         "merchant_name": "M", "plan_name": "P", "amount": 1,
         "failure_date": "d", "failure_reason": "bank_declined",
         "payment_method": "card", "last4": "0000", "language": "English",
         "attempt_no": 1},
        settings,
    )
    assert overrides["server"]["url"] == "https://tunnel.test/webhooks/vapi"
    assert "tool-calls" in overrides["serverMessages"]
    assert "end-of-call-report" in overrides["serverMessages"]