"""Customer-record tests: exactly 10 fictional customers, valid fields, no real data."""
import re

from app.customers import CUSTOMER_FIELDS, list_customers, public_customer


def test_exactly_ten_customers(db_path):
    assert len(list_customers(db_path)) == 10


def test_required_fields_present(db_path):
    for c in list_customers(db_path):
        missing = [k for k in CUSTOMER_FIELDS if k not in c or c[k] in (None, "")]
        assert not missing, f"{c.get('customer_id')}: missing {missing}"


def test_no_real_phone_numbers(db_path):
    # Fictional placeholders only — must match the +91-DEMO-XXXX pattern.
    for c in list_customers(db_path):
        assert re.fullmatch(r"\+91-DEMO-\d{4}", c["phone"]), c["phone"]
        digits = re.sub(r"\D", "", c["phone"])
        assert len(digits) < 10, "must not look like a real phone number"


def test_indian_names_and_inr(db_path):
    for c in list_customers(db_path):
        assert c["currency"] == "INR"
        assert c["amount"] > 0
        assert " " in c["customer_name"].strip()


def test_public_customer_strips_verification_suffix(db_path, customer):
    public = public_customer(customer)
    assert "verification_suffix" not in public
    assert public["customer_name"] == customer["customer_name"]


def test_api_never_exposes_verification_suffix(api_client):
    for resp in (api_client.get("/customers"), api_client.get("/customers/CUST001")):
        assert resp.status_code == 200
        body = resp.text
        assert "verification_suffix" not in body
        assert "380" not in body  # CUST001's secret must not appear anywhere
