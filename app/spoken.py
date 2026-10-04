"""Helpers for verifying a spoken 3-digit code, enforced in code (not in the prompt).

Speech-to-text delivers digits in many shapes: "380", "3 8 0", "3 80", "three eight zero",
"three eighty" is NOT accepted (ambiguous). We reduce everything to a digit string,
then compare. This widens accepted *formats* only; the code itself is still 3 digits
and a per-call attempt limit stops guessing.
"""
import json
import re

from .database import get_connection

MAX_FAILED_ATTEMPTS = 3

_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "shunya": "0",
    "one": "1", "ek": "1",
    "two": "2", "do": "2",
    "three": "3", "teen": "3",
    "four": "4", "char": "4", "chaar": "4",
    "five": "5", "paanch": "5", "panch": "5",
    "six": "6", "chhe": "6", "che": "6",
    "seven": "7", "saat": "7",
    "eight": "8", "aath": "8", "ath": "8",
    "nine": "9", "nau": "9",
}


def normalize_spoken_digits(text: object) -> str:
    """'3 80' -> '380'; 'three eight zero' -> '380'; 'my code is 380.' -> '380'."""
    out = []
    for token in re.findall(r"[a-zA-Z]+|\d", str(text).lower()):
        if token.isdigit():
            out.append(token)
        elif token in _WORDS:
            out.append(_WORDS[token])
    return "".join(out)


def too_many_failed_attempts(db_path: str | None, call_id: str) -> bool:
    """True once this call has MAX_FAILED_ATTEMPTS failed verify_identity results."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT result FROM tool_calls WHERE call_id = ? AND tool_name = 'verify_identity'",
            (call_id,),
        ).fetchall()
    finally:
        conn.close()
    failed = 0
    for r in rows:
        try:
            if json.loads(r["result"]).get("verified") is False:
                failed += 1
        except (ValueError, AttributeError):
            continue
    return failed >= MAX_FAILED_ATTEMPTS
