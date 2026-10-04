#!/usr/bin/env python3
"""Seed the SQLite database with the 10 fictional customers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import default_db_path
from app.customers import load_customers_json, seed_customers
from app.database import init_db


def main() -> None:
    db_path = default_db_path()
    init_db(db_path)
    count = seed_customers(db_path, load_customers_json())
    print(f"Seeded {count} customers into {db_path}")


if __name__ == "__main__":
    main()
