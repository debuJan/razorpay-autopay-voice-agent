"""The five backend tools Asha can call.

Every tool validates its inputs server-side, records the invocation in the
`tool_calls` table (for call traces), and never leaks the verification_suffix.

Server-side authorization (never rely on the LLM prompt alone):
- Each tool call is bound to a call record via `call_id`. The call record is
  bound to exactly one `customer_id`, and the tool's `customer_id` argument
  must match it — otherwise the call is rejected.
- `verify_identity` flips the call record's `verified` flag on success.
- `send_payment_link` and `schedule_retry` refuse to run on unverified calls.

`call_id=None` means trusted internal invocation (scenario engine, unit tests,
scripts) and skips the call-binding checks. The Vapi webhook always resolves
a real internal call record and threads it through, so live calls are always
authorized.
"""

import json
import uuid
from datetime import date

from . import payments
from .customers import get_customer
from .database import get_connection, utcnow
from .spoken import normalize_spoken_digits, too_many_failed_attempts


ALLOWED_OUTCOMES = {
    "payment_link_sent",
    "method_update_sent",
    "retry_scheduled",
    "claims_paid",
    "cancel_requested",
    "hardship",
    "dispute",
    "wrong_person",
    "verification_failed",
    "callback_requested",
    "do_not_call",
    "escalated",
    "no_resolution",
}


class ToolError(Exception):
    """Raised when a tool call is invalid. The agent must handle this gracefully."""


def _record_tool_call(
    db_path: str | None,
    call_id: str | None,
    tool_name: str,
    arguments: dict,
    result: dict,
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO tool_calls (call_id, tool_name, arguments, result, timestamp)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                call_id,
                tool_name,
                json.dumps(arguments),
                json.dumps(result),
                utcnow(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _require_customer(db_path: str | None, customer_id: str) -> dict:
    customer = get_customer(db_path, customer_id)

    if customer is None:
        raise ToolError(f"Unknown customer_id: {customer_id}")

    return customer


def _authorize_call_context(
    db_path: str | None,
    call_id: str | None,
    customer_id: str,
) -> dict | None:
    """Enforce that a tool call belongs to the call it claims to.

    Returns the call record (or None when call_id is None, i.e. trusted
    internal invocation).

    Raises ToolError when:
    - the call record does not exist, or
    - the tool's customer_id differs from the customer bound to the call.
    """

    if call_id is None:
        return None

    conn = get_connection(db_path)

    try:
        row = conn.execute(
            """
            SELECT call_id, customer_id, verified
            FROM calls
            WHERE call_id = ?
            """,
            (call_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise ToolError(f"Unknown call_id: {call_id}")

    if row["customer_id"] != customer_id:
        raise ToolError(
            f"customer_id {customer_id!r} does not match this call "
            f"(bound to {row['customer_id']!r})."
        )

    return dict(row)


def _require_verified(call_row: dict | None, tool_name: str) -> None:
    """`send_payment_link` / `schedule_retry` need a verified call."""

    if call_row is not None and not call_row["verified"]:
        raise ToolError(
            f"{tool_name} refused: identity is not verified for this call."
        )


def _mark_call_verified(db_path: str | None, call_id: str) -> None:
    conn = get_connection(db_path)

    try:
        conn.execute(
            "UPDATE calls SET verified = 1 WHERE call_id = ?",
            (call_id,),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TOOL 1 — verify_identity
# ---------------------------------------------------------------------------

def verify_identity(
    db_path: str | None,
    customer_id: str,
    answer: str,
    call_id: str | None = None,
) -> dict:
    """Compare the caller's answer against the server-side verification value.

    The expected value is NEVER included in the tool description sent to the LLM.
    Spoken digit formats such as "3 8 0" and "three eight zero" are normalized
    before comparison.
    """

    customer = _require_customer(db_path, customer_id)

    call_row = _authorize_call_context(
        db_path,
        call_id,
        customer_id,
    )

    # Server-side protection against excessive verification attempts.
    # This is enforced here instead of relying only on the LLM prompt.
    if call_id is not None and too_many_failed_attempts(db_path, call_id):
        raise ToolError("Too many failed verification attempts for this call.")

    normalized_answer = normalize_spoken_digits(answer)
    normalized_expected = normalize_spoken_digits(
        customer["verification_suffix"]
    )

    verified = (
        normalized_answer != ""
        and normalized_answer == normalized_expected
    )

    if verified and call_row is not None:
        _mark_call_verified(db_path, call_id)

    result = {
        "verified": verified,
    }

    _record_tool_call(
        db_path,
        call_id,
        "verify_identity",
        {
            "customer_id": customer_id,
            "answer": "***",
        },
        result,
    )

    return result


# ---------------------------------------------------------------------------
# TOOL 2 — send_payment_link
# ---------------------------------------------------------------------------

def send_payment_link(
    db_path: str | None,
    customer_id: str,
    channel: str = "sms",
    purpose: str = "pay_now",
    call_id: str | None = None,
) -> dict:
    _require_customer(db_path, customer_id)

    call_row = _authorize_call_context(
        db_path,
        call_id,
        customer_id,
    )

    _require_verified(
        call_row,
        "send_payment_link",
    )

    if not payments.is_valid_channel(channel):
        raise ToolError(
            f"Invalid channel: {channel!r}. Allowed: 'sms'."
        )

    if not payments.is_valid_purpose(purpose):
        raise ToolError(
            f"Invalid purpose: {purpose!r}. "
            "Allowed: 'pay_now', 'update_method'."
        )

    link_id = f"LINK{uuid.uuid4().hex[:8].upper()}"

    result = payments.make_demo_payment_link(
        customer_id,
        purpose,
        link_id,
    )

    _record_tool_call(
        db_path,
        call_id,
        "send_payment_link",
        {
            "customer_id": customer_id,
            "channel": channel,
            "purpose": purpose,
        },
        result,
    )

    return result


# ---------------------------------------------------------------------------
# TOOL 3 — schedule_retry
# ---------------------------------------------------------------------------

def schedule_retry(
    db_path: str | None,
    customer_id: str,
    retry_date: str,
    call_id: str | None = None,
    today: date | None = None,
) -> dict:
    """Schedule a retry.

    Only dates within the next 7 days are allowed and dates in the past
    are rejected.
    """

    _require_customer(db_path, customer_id)

    call_row = _authorize_call_context(
        db_path,
        call_id,
        customer_id,
    )

    _require_verified(
        call_row,
        "schedule_retry",
    )

    today = today or date.today()

    try:
        parsed = date.fromisoformat(retry_date)
    except ValueError:
        raise ToolError(
            f"Invalid date format: {retry_date!r}. Use YYYY-MM-DD."
        )

    delta = (parsed - today).days

    if delta < 0:
        raise ToolError(
            f"Cannot schedule a retry in the past: {retry_date}."
        )

    if delta > 7:
        raise ToolError(
            f"Retry date {retry_date} is more than 7 days away. "
            "Only dates within the next 7 days are allowed."
        )

    result = {
        "scheduled": True,
        "date": retry_date,
    }

    _record_tool_call(
        db_path,
        call_id,
        "schedule_retry",
        {
            "customer_id": customer_id,
            "date": retry_date,
        },
        result,
    )

    return result


# ---------------------------------------------------------------------------
# TOOL 4 — transfer_to_human
# ---------------------------------------------------------------------------

def transfer_to_human(
    db_path: str | None,
    customer_id: str,
    reason: str,
    call_id: str | None = None,
) -> dict:
    """Simulated human escalation. No real transfer is performed."""

    _require_customer(db_path, customer_id)

    _authorize_call_context(
        db_path,
        call_id,
        customer_id,
    )

    if not reason or not str(reason).strip():
        raise ToolError(
            "A reason is required for human transfer."
        )

    case_id = f"CASE{uuid.uuid4().hex[:8].upper()}"

    result = {
        "transferred": True,
        "case_id": case_id,
    }

    _record_tool_call(
        db_path,
        call_id,
        "transfer_to_human",
        {
            "customer_id": customer_id,
            "reason": reason,
        },
        result,
    )

    return result


# ---------------------------------------------------------------------------
# TOOL 5 — log_outcome
# ---------------------------------------------------------------------------

def log_outcome(
    db_path: str | None,
    customer_id: str,
    outcome: str,
    notes: str = "",
    call_id: str | None = None,
) -> dict:

    if outcome not in ALLOWED_OUTCOMES:
        raise ToolError(
            f"Invalid outcome: {outcome!r}. "
            f"Allowed: {sorted(ALLOWED_OUTCOMES)}."
        )

    _require_customer(
        db_path,
        customer_id,
    )

    _authorize_call_context(
        db_path,
        call_id,
        customer_id,
    )

    conn = get_connection(db_path)

    try:
        conn.execute(
            """
            INSERT INTO outcomes (
                call_id,
                customer_id,
                outcome,
                notes,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                call_id,
                customer_id,
                outcome,
                notes,
                utcnow(),
            ),
        )

        conn.commit()

    finally:
        conn.close()

    result = {
        "logged": True,
        "outcome": outcome,
    }

    _record_tool_call(
        db_path,
        call_id,
        "log_outcome",
        {
            "customer_id": customer_id,
            "outcome": outcome,
            "notes": notes,
        },
        result,
    )

    return result