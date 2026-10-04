"""Customer record access. The verification_suffix is server-side only."""
import json
import os

from .database import get_connection

CUSTOMER_FIELDS = [
    "customer_id", "customer_name", "phone", "merchant_name", "plan_name",
    "amount", "currency", "failure_reason", "failure_date", "payment_method",
    "last4", "language", "verification_suffix", "attempt_no",
]

PUBLIC_FIELDS = [f for f in CUSTOMER_FIELDS if f != "verification_suffix"]

DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "customers.json")


def load_customers_json(path: str = DATA_PATH) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        customers = json.load(f)
    for c in customers:
        missing = [k for k in CUSTOMER_FIELDS if k not in c]
        if missing:
            raise ValueError(f"Customer {c.get('customer_id')} missing fields: {missing}")
    return customers


def seed_customers(db_path: str | None, customers: list[dict]) -> int:
    conn = get_connection(db_path)
    try:
        for c in customers:
            conn.execute(
                """INSERT OR REPLACE INTO customers
                   (customer_id, customer_name, phone, merchant_name, plan_name, amount,
                    currency, failure_reason, failure_date, payment_method, last4,
                    language, verification_suffix, attempt_no)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(c[k] for k in CUSTOMER_FIELDS),
            )
        conn.commit()
    finally:
        conn.close()
    return len(customers)


def get_customer(db_path: str | None, customer_id: str) -> dict | None:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_customers(db_path: str | None) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM customers ORDER BY customer_id").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def public_customer(customer: dict) -> dict:
    """Strip the server-side verification_suffix before any API/LLM exposure."""
    return {k: customer[k] for k in PUBLIC_FIELDS if k in customer}
