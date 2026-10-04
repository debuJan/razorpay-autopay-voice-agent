"""Payment-link tool tests: purposes, channels, and clearly-mocked links."""
import pytest

from app import tools
from app.tools import ToolError


def test_valid_purposes_succeed(db_path):
    for purpose in ("pay_now", "update_method"):
        result = tools.send_payment_link(db_path, "CUST001", "sms", purpose)
        assert result["sent"] is True
        assert result["demo"] is True
        assert result["payment_link_id"].startswith("LINK")


def test_invalid_purpose_fails(db_path):
    with pytest.raises(ToolError):
        tools.send_payment_link(db_path, "CUST001", "sms", "charge_card")


def test_invalid_channel_fails(db_path):
    with pytest.raises(ToolError):
        tools.send_payment_link(db_path, "CUST001", "email", "pay_now")


def test_payment_link_is_clearly_mocked(db_path):
    result = tools.send_payment_link(db_path, "CUST001", "sms", "pay_now")
    url = result["payment_url"]
    assert ".invalid" in url  # RFC 2606 — can never resolve to a real site
    assert "demo" in url


def test_unknown_customer_fails(db_path):
    with pytest.raises(ToolError):
        tools.send_payment_link(db_path, "NOPE", "sms", "pay_now")
