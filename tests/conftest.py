"""Shared fixtures: isolated temp database seeded with the 10 customers."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.customers import get_customer, load_customers_json, seed_customers
from app.database import init_db


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    seed_customers(path, load_customers_json())
    return path


@pytest.fixture()
def customer(db_path):
    return get_customer(db_path, "CUST001")


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    db_file = str(tmp_path / "api.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    init_db(db_file)
    seed_customers(db_file, load_customers_json())
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client
