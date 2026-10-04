import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import get_connection, init_db, utcnow
from app.spoken import normalize_spoken_digits, too_many_failed_attempts


@pytest.mark.parametrize("spoken", ["380", "3 8 0", "3 80", "3 8 0.", "three eight zero",
                                    "Three, eight, oh", "my code is 380", "teen aath shunya"])
def test_formats_normalize_to_380(spoken):
    assert normalize_spoken_digits(spoken) == "380"


@pytest.mark.parametrize("spoken", ["381", "38", "three eighty", "", "0380", "I have 2 cats 380"])
def test_wrong_or_ambiguous_does_not_match(spoken):
    assert normalize_spoken_digits(spoken) != "380"


def _log(conn, call_id, verified):
    conn.execute("INSERT INTO tool_calls (call_id, tool_name, arguments, result, timestamp)"
                 " VALUES (?, 'verify_identity', '{}', ?, ?)",
                 (call_id, json.dumps({"verified": verified}), utcnow()))
    conn.commit()


def test_attempt_limit(tmp_path):
    db = str(tmp_path / "s.db")
    init_db(db)
    conn = get_connection(db)
    _log(conn, "CALL-A", False)
    _log(conn, "CALL-A", False)
    assert not too_many_failed_attempts(db, "CALL-A")
    _log(conn, "CALL-A", False)
    assert too_many_failed_attempts(db, "CALL-A")
    assert not too_many_failed_attempts(db, "CALL-B")      # other calls unaffected
    conn.close()
