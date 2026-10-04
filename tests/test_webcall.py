"""Tests for web-call support. Uses a minimal app that mounts only the webcall router."""
import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.customers import load_customers_json, seed_customers
from app.database import get_connection, init_db
from app.webcall import resolve_internal_call_for_message, router


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = str(tmp_path / "wc.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("VAPI_PUBLIC_KEY", "pk_test_public")
    monkeypatch.setenv("VAPI_ASSISTANT_ID", "asst_test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    init_db(db_file)
    conn = get_connection(db_file)
    try:  # mirror the real schema, which has a `verified` column
        conn.execute("ALTER TABLE calls ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    conn.close()
    seed_customers(db_file, load_customers_json())
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, db_file


def test_start_web_creates_bound_call_and_hides_secret(env):
    client, db = env
    r = client.post("/calls/start-web", json={"customer_id": "CUST001"})
    assert r.status_code == 200
    body = r.json()
    assert body["call_id"].startswith("CALL-")
    assert body["public_key"] == "pk_test_public"
    assert body["assistant_overrides"]["metadata"]["internal_call_id"] == body["call_id"]
    # CUST001's verification code is 380: it must never appear in what the browser gets.
    assert "380" not in json.dumps(body["assistant_overrides"]).replace("4821", "")
    assert "Rohan Mehta" in body["assistant_overrides"]["firstMessage"]
    conn = get_connection(db)
    row = conn.execute("SELECT customer_id, provider_call_id FROM calls WHERE call_id = ?",
                       (body["call_id"],)).fetchone()
    conn.close()
    assert row["customer_id"] == "CUST001" and row["provider_call_id"] is None


def test_start_web_unknown_customer_404(env):
    client, _ = env
    assert client.post("/calls/start-web", json={"customer_id": "NOPE"}).status_code == 404


def test_start_web_missing_public_key_is_clear_error(env, monkeypatch):
    client, _ = env
    monkeypatch.delenv("VAPI_PUBLIC_KEY")
    r = client.post("/calls/start-web", json={"customer_id": "CUST001"})
    assert r.status_code == 500 and "VAPI_PUBLIC_KEY" in r.json()["detail"]


def test_attach_links_and_blocks_relink(env):
    client, db = env
    call_id = client.post("/calls/start-web", json={"customer_id": "CUST001"}).json()["call_id"]
    assert client.post(f"/calls/{call_id}/attach", json={"provider_call_id": "v1"}).status_code == 200
    assert client.post(f"/calls/{call_id}/attach", json={"provider_call_id": "v1"}).status_code == 200
    assert client.post(f"/calls/{call_id}/attach", json={"provider_call_id": "other"}).status_code == 409
    assert client.post("/calls/CALL-NOPE/attach", json={"provider_call_id": "x"}).status_code == 404


def test_resolver_by_provider_id(env):
    client, db = env
    call_id = client.post("/calls/start-web", json={"customer_id": "CUST002"}).json()["call_id"]
    client.post(f"/calls/{call_id}/attach", json={"provider_call_id": "vapi-123"})
    got = resolve_internal_call_for_message(db, {"call": {"id": "vapi-123"}})
    assert got["call_id"] == call_id and got["customer_id"] == "CUST002"


def test_resolver_metadata_fallback_backfills_provider_id(env):
    client, db = env
    call_id = client.post("/calls/start-web", json={"customer_id": "CUST003"}).json()["call_id"]
    msg = {"call": {"id": "vapi-early",
                    "assistantOverrides": {"metadata": {"internal_call_id": call_id}}}}
    got = resolve_internal_call_for_message(db, msg)
    assert got["call_id"] == call_id
    again = resolve_internal_call_for_message(db, {"call": {"id": "vapi-early"}})
    assert again["call_id"] == call_id  # now resolvable by provider id alone


def test_resolver_unknown_returns_none(env):
    _, db = env
    assert resolve_internal_call_for_message(db, {"call": {"id": "ghost"}}) is None
    assert resolve_internal_call_for_message(db, {}) is None
    bad = {"call": {"id": "g", "metadata": {"internal_call_id": "CALL-FAKE"}}}
    assert resolve_internal_call_for_message(db, bad) is None


def test_webcall_page_served(env):
    client, _ = env
    r = client.get("/webcall")
    assert r.status_code == 200 and "Asha web call" in r.text


def test_bind_call_customer_overrides_model_guess():
    from app.webcall import bind_call_customer
    internal = {"call_id": "CALL-1", "customer_id": "CUST001"}
    assert bind_call_customer({"customer_id": "Rohan Mehta", "answer": "380"}, internal) == \
        {"customer_id": "CUST001", "answer": "380"}
    assert bind_call_customer({"answer": "380"}, internal)["customer_id"] == "CUST001"
    assert bind_call_customer({"customer_id": "CUST009"}, internal)["customer_id"] == "CUST001"
