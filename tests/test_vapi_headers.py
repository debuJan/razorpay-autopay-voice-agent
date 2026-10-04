"""Run from your repo's tests/ folder. Checks the webhook secret reaches Vapi's requests."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from app.vapi import build_assistant_overrides

CUSTOMER = {"customer_id": "CUST001", "customer_name": "Rohan Mehta", "merchant_name": "FitNest Gym",
            "plan_name": "Gold", "amount": 1499, "currency": "INR", "failure_reason": "insufficient_balance",
            "failure_date": "28 September 2026", "payment_method": "debit card", "last4": "4821",
            "language": "English", "attempt_no": 1}


def _settings(secret):
    return SimpleNamespace(public_base_url="https://x.ngrok.test", vapi_webhook_secret=secret,
                           vapi_voice_provider="11labs", vapi_voice_id="")


def test_secret_header_on_assistant_and_every_tool():
    o = build_assistant_overrides(CUSTOMER, _settings("s3cret"))
    assert o["server"]["url"] == "https://x.ngrok.test/webhooks/vapi"
    assert o["server"]["headers"] == {"X-Webhook-Secret": "s3cret"}
    for t in o["model"]["tools"]:
        assert t["server"]["headers"] == {"X-Webhook-Secret": "s3cret"}


def test_no_invalid_serverUrl_field():
    assert "serverUrl" not in build_assistant_overrides(CUSTOMER, _settings("s3cret"))


def test_no_header_when_secret_unset():
    o = build_assistant_overrides(CUSTOMER, _settings(""))
    assert "headers" not in o["server"]
    assert "X-Webhook-Secret" not in json.dumps(o)
