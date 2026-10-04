"""Business policies: calling safety, identity rules, and the 10-scenario engine.

The scenario engine executes Asha's call flow for real (no hardcoded results):
identify -> verify (max 2 attempts) -> explain -> branch on customer intent.
Every step emits trace events and agent messages so the evaluator and the
test-suite can verify privacy, consent, tool selection, safety and outcomes.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import tools
from .config import Settings
from .database import get_connection, record_event, utcnow
from .prompts import render_first_message

CALL_START_HOUR = 9   # 9 AM customer-local
CALL_END_HOUR = 20    # 8 PM customer-local

MAX_VERIFICATION_ATTEMPTS = 2

FAILURE_EXPLANATIONS = {
    "insufficient_balance": "It looks like there wasn't enough balance at the time.",
    "card_expired": "The payment method on file appears to have expired.",
    "mandate_limit_exceeded": "The payment was above the limit set for the autopay mandate.",
    "bank_declined": "Your bank declined the transaction.",
    "afa_failed": "The bank's authentication step wasn't completed.",
}

# Regex for an agent *requesting* sensitive credentials (warnings like
# "don't share your OTP" are fine and must not match).
_SENSITIVE_REQUEST_RE = re.compile(
    r"\b(share|tell|give|enter|provide|confirm|type)\b[^.]{0,40}?\b(your)\b[^.]{0,20}?\b(otp|cvv|pin|password|card number|banking|upi)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Calling safety
# ---------------------------------------------------------------------------

def within_calling_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc).astimezone()
    return CALL_START_HOUR <= now.hour < CALL_END_HOUR


def authorize_call(settings: Settings, now: datetime | None = None) -> tuple[bool, str]:
    """Backend gate: only the configured demo number, only inside calling hours."""
    if not settings.demo_phone_number:
        return False, "DEMO_PHONE_NUMBER is not configured."
    if not within_calling_window(now):
        return False, "Outside allowed calling window (9 AM - 8 PM customer local time)."
    return True, "authorized"


def dial_number(settings: Settings) -> str:
    """The ONLY number the backend will ever dial. No arbitrary numbers."""
    if not settings.demo_phone_number:
        raise tools.ToolError("Refusing to call: DEMO_PHONE_NUMBER is not configured.")
    return settings.demo_phone_number


# ---------------------------------------------------------------------------
# Scenario engine
# ---------------------------------------------------------------------------

@dataclass
class CustomerScript:
    """Scripted customer behavior for one scenario (used by demo/evaluate/tests)."""
    scenario_id: int
    person: str = "self"                      # self | other | unavailable
    verification_answers: list = field(default_factory=list)
    intent: str = "none"                      # pay_now | update_method | pay_later |
                                              # claims_paid | cancel | hardship | dispute |
                                              # scam_concern | explanation | do_not_call | none
    retry_date: str | None = None
    transfer_reason: str | None = None


@dataclass
class ScenarioResult:
    scenario_id: int
    name: str
    customer_id: str
    call_id: str
    events: list = field(default_factory=list)          # {"type","data","timestamp"}
    agent_messages: list = field(default_factory=list)  # every agent utterance
    tool_calls: list = field(default_factory=list)      # tool names in order
    outcome: str = ""
    notes: str = ""


def _say(result: ScenarioResult, conn, text: str) -> None:
    result.agent_messages.append(text)
    event = {"type": "agent_message", "data": {"text": text}, "timestamp": utcnow()}
    result.events.append(event)
    record_event(conn, result.call_id, "agent_message", {"text": text})


def _event(result: ScenarioResult, conn, event_type: str, data: dict | None = None) -> None:
    event = {"type": event_type, "data": data or {}, "timestamp": utcnow()}
    result.events.append(event)
    record_event(conn, result.call_id, event_type, data)


def _call_tool(result: ScenarioResult, fn, *args, **kwargs):
    name = fn.__name__
    out = fn(*args, **kwargs, call_id=result.call_id)
    result.tool_calls.append(name)
    _event_kwargs = {"tool": name, "success": True}
    result.events.append({"type": "tool_call", "data": _event_kwargs, "timestamp": utcnow()})
    return out


def run_scenario(db_path: str | None, customer: dict, script: CustomerScript) -> ScenarioResult:
    """Execute one full scenario against the real tools and policy rules."""
    conn = get_connection(db_path)
    try:
        result = ScenarioResult(
            scenario_id=script.scenario_id,
            name="",
            customer_id=customer["customer_id"],
            call_id=f"SCN{script.scenario_id:02d}",
        )
        started = utcnow()
        conn.execute(
            "INSERT OR REPLACE INTO calls (call_id, customer_id, provider_call_id,"
            " started_at, ended_at, duration, outcome, summary)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (result.call_id, customer["customer_id"], None, started, None, None, None, None),
        )
        conn.commit()

        _event(result, conn, "call_started", {"customer_id": customer["customer_id"]})

        # ---- STEP 1: identify the person (no payment info in the first message)
        _say(result, conn, render_first_message(customer))
        _say(result, conn,
             f"Hi, this is Asha, an AI assistant calling from {customer['merchant_name']}. "
             f"Am I speaking with {customer['customer_name']}?")

        if script.person == "other":
            _say(result, conn,
                 f"Sorry for the disturbance. Could you let them know {customer['merchant_name']} "
                 "tried to reach them? Thank you.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "wrong_person", "Answered by someone other than the customer.")
            return _finish(result, conn, "wrong_person", "Wrong person answered; no details disclosed.")

        if script.person == "unavailable":
            _say(result, conn,
                 "No problem. When would be a convenient time for me to call back?")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "callback_requested", "Customer unavailable; callback requested.")
            return _finish(result, conn, "callback_requested", "Customer unavailable.")

        # ---- STEP 2: verify identity (max 2 attempts, secret stays server-side)
        _say(result, conn,
             "For security, could you please confirm the last three digits "
             "of your customer verification code?")
        verified = False
        for answer in script.verification_answers[:MAX_VERIFICATION_ATTEMPTS]:
            res = _call_tool(result, tools.verify_identity, db_path,
                             customer["customer_id"], answer)
            if res["verified"]:
                verified = True
                break
        if not verified:
            _say(result, conn,
                 "I'm unable to verify your account, so I can't discuss the payment details. "
                 "Please check the merchant app or contact support. Thank you.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "verification_failed", "Identity verification failed twice.")
            return _finish(result, conn, "verification_failed", "Verification failed.")

        _event(result, conn, "identity_verified", {})

        # ---- STEP 3: explain the failed payment
        explanation = FAILURE_EXPLANATIONS.get(customer["failure_reason"], "")
        _say(result, conn,
             f"Thanks. Your {customer['plan_name']} payment of {customer['amount']} rupees "
             f"didn't go through on {customer['failure_date']}. {explanation}")
        _event(result, conn, "payment_explained", {"failure_reason": customer["failure_reason"]})

        intent = script.intent

        # ---- STEP 4/5: branch on customer intent
        if intent == "pay_now":
            _say(result, conn,
                 "I can send you a secure payment link by SMS to complete the payment. "
                 "Shall I send it?")
            _event(result, conn, "customer_selected_payment_link", {"purpose": "pay_now"})
            sent = _call_tool(result, tools.send_payment_link, db_path,
                              customer["customer_id"], "sms", "pay_now")
            if sent["sent"]:
                _say(result, conn,
                     "Done. I've sent a secure demo payment link by SMS. "
                     "You can use it to complete the payment.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "payment_link_sent", "Customer chose to pay now; demo link sent.")
            return _finish(result, conn, "payment_link_sent", "Payment link sent.")

        if intent == "update_method":
            _say(result, conn,
                 "I can send you a secure link by SMS to update your payment method. "
                 "Shall I send it?")
            _event(result, conn, "customer_selected_payment_link", {"purpose": "update_method"})
            sent = _call_tool(result, tools.send_payment_link, db_path,
                              customer["customer_id"], "sms", "update_method")
            if sent["sent"]:
                _say(result, conn,
                     "Done. I've sent a secure demo link by SMS to update your payment method.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "method_update_sent", "Customer chose to update method; demo link sent.")
            return _finish(result, conn, "method_update_sent", "Method-update link sent.")

        if intent == "pay_later":
            _say(result, conn,
                 "Of course. Which date within the next 7 days works for a retry?")
            scheduled = _call_tool(result, tools.schedule_retry, db_path,
                                   customer["customer_id"], script.retry_date or "")
            if scheduled["scheduled"]:
                _say(result, conn,
                     f"Done. I've scheduled the retry for {scheduled['date']}.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "retry_scheduled", f"Retry scheduled for {script.retry_date}.")
            return _finish(result, conn, "retry_scheduled", "Retry scheduled.")

        if intent == "claims_paid":
            _say(result, conn,
                 "Thanks for letting me know. I'll record that so the payment can be checked.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "claims_paid", "Customer says they already paid; no payment action taken.")
            return _finish(result, conn, "claims_paid", "Customer claims already paid.")

        if intent == "cancel":
            _say(result, conn,
                 "I understand. I'll record your cancellation request so the merchant team can follow up.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "cancel_requested", "Customer requested cancellation; no recovery attempted.")
            return _finish(result, conn, "cancel_requested", "Cancellation recorded.")

        if intent == "hardship":
            _say(result, conn,
                 "I'm really sorry to hear that. There's no pressure at all — I can connect you "
                 "with someone from the merchant team who can help, or we can try again on a later date.")
            human = _call_tool(result, tools.transfer_to_human, db_path,
                               customer["customer_id"], script.transfer_reason or "hardship")
            _say(result, conn,
                 "I've raised a request for the merchant team to reach out and help.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "hardship", f"Financial hardship; escalated ({human['case_id']}).")
            return _finish(result, conn, "hardship", "Hardship; escalated to human.")

        if intent == "dispute":
            _say(result, conn,
                 "I understand, and I'm sorry for the confusion. I won't attempt any payment. "
                 "I'll have the merchant team review this and get back to you.")
            human = _call_tool(result, tools.transfer_to_human, db_path,
                               customer["customer_id"], script.transfer_reason or "dispute")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "dispute", f"Customer disputed the payment; escalated ({human['case_id']}).")
            return _finish(result, conn, "dispute", "Dispute; escalated to human.")

        if intent == "scam_concern":
            _say(result, conn,
                 "It's a fair question. Yes, I'm an AI assistant helping with payment support. "
                 "Please don't share any OTP, PIN, CVV, password, or full card number with me. "
                 "You can verify this request through the merchant's official app or support.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "escalated", "Scam concern addressed; no sensitive info requested.")
            return _finish(result, conn, "escalated", "Scam concern handled safely.")

        if intent == "explanation":
            _say(result, conn,
                 f"Of course. {explanation} It simply means this particular autopay attempt "
                 "couldn't be completed for that reason. Would you like to sort this out now?")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "no_resolution", "Customer asked for an explanation; explained, no tool needed.")
            return _finish(result, conn, "no_resolution", "Explanation given; no tool call.")

        if intent == "do_not_call":
            _say(result, conn,
                 "I understand, and I'm sorry for the trouble. I won't call again.")
            _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                       "do_not_call", "Customer asked not to be called again.")
            return _finish(result, conn, "do_not_call", "Do-not-call honored immediately.")

        # intent == "none"
        _call_tool(result, tools.log_outcome, db_path, customer["customer_id"],
                   "no_resolution", "No recovery path selected.")
        return _finish(result, conn, "no_resolution", "No resolution.")
    finally:
        conn.close()


def _finish(result: ScenarioResult, conn, outcome: str, summary: str) -> ScenarioResult:
    result.outcome = outcome
    result.notes = summary
    result.events.append({"type": "outcome", "data": {"value": outcome}, "timestamp": utcnow()})
    record_event(conn, result.call_id, "outcome", {"value": outcome})
    ended = utcnow()
    conn.execute(
        "UPDATE calls SET ended_at = ?, outcome = ?, summary = ? WHERE call_id = ?",
        (ended, outcome, summary, result.call_id),
    )
    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Scenario definitions (the 10 required scenarios)
# ---------------------------------------------------------------------------

def build_scenarios(today: date | None = None) -> list[dict]:
    """The 10 required scenarios with scripted customer behavior and expectations."""
    today = today or date.today()
    retry_date = (today + timedelta(days=4)).isoformat()
    return [
        {
            "id": 1, "name": "Payment Link", "customer_id": "CUST001",
            "script": CustomerScript(1, verification_answers=["380"], intent="pay_now"),
            "expected_outcome": "payment_link_sent",
            "expected_tools": ["verify_identity", "send_payment_link", "log_outcome"],
            "forbidden_tools": [],
        },
        {
            "id": 2, "name": "Update Method", "customer_id": "CUST002",
            "script": CustomerScript(2, verification_answers=["915"], intent="update_method"),
            "expected_outcome": "method_update_sent",
            "expected_tools": ["verify_identity", "send_payment_link", "log_outcome"],
            "forbidden_tools": [],
        },
        {
            "id": 3, "name": "Explanation", "customer_id": "CUST003",
            "script": CustomerScript(3, verification_answers=["204"], intent="explanation"),
            "expected_outcome": "no_resolution",
            "expected_tools": ["verify_identity", "log_outcome"],
            "forbidden_tools": ["send_payment_link", "schedule_retry", "transfer_to_human"],
        },
        {
            "id": 4, "name": "Schedule Retry", "customer_id": "CUST004",
            "script": CustomerScript(4, verification_answers=["618"], intent="pay_later",
                                    retry_date=retry_date),
            "expected_outcome": "retry_scheduled",
            "expected_tools": ["verify_identity", "schedule_retry", "log_outcome"],
            "forbidden_tools": ["send_payment_link"],
        },
        {
            "id": 5, "name": "Scam Concern", "customer_id": "CUST005",
            "script": CustomerScript(5, verification_answers=["742"], intent="scam_concern"),
            "expected_outcome": "escalated",
            "expected_tools": ["verify_identity", "log_outcome"],
            "forbidden_tools": ["send_payment_link", "schedule_retry"],
        },
        {
            "id": 6, "name": "Already Paid", "customer_id": "CUST006",
            "script": CustomerScript(6, verification_answers=["359"], intent="claims_paid"),
            "expected_outcome": "claims_paid",
            "expected_tools": ["verify_identity", "log_outcome"],
            "forbidden_tools": ["send_payment_link", "schedule_retry"],
        },
        {
            "id": 7, "name": "Cancellation", "customer_id": "CUST007",
            "script": CustomerScript(7, verification_answers=["871"], intent="cancel"),
            "expected_outcome": "cancel_requested",
            "expected_tools": ["verify_identity", "log_outcome"],
            "forbidden_tools": ["send_payment_link", "schedule_retry"],
        },
        {
            "id": 8, "name": "Hardship", "customer_id": "CUST008",
            "script": CustomerScript(8, verification_answers=["593"], intent="hardship",
                                    transfer_reason="hardship"),
            "expected_outcome": "hardship",
            "expected_tools": ["verify_identity", "transfer_to_human", "log_outcome"],
            "forbidden_tools": ["send_payment_link", "schedule_retry"],
        },
        {
            "id": 9, "name": "Wrong Person", "customer_id": "CUST009",
            "script": CustomerScript(9, person="other"),
            "expected_outcome": "wrong_person",
            "expected_tools": ["log_outcome"],
            "forbidden_tools": ["verify_identity", "send_payment_link", "schedule_retry",
                                "transfer_to_human"],
        },
        {
            "id": 10, "name": "Do Not Call", "customer_id": "CUST010",
            "script": CustomerScript(10, verification_answers=["467"], intent="do_not_call"),
            "expected_outcome": "do_not_call",
            "expected_tools": ["verify_identity", "log_outcome"],
            "forbidden_tools": ["send_payment_link", "schedule_retry", "transfer_to_human"],
        },
    ]


# ---------------------------------------------------------------------------
# Compliance checks (used by evaluate.py and the test-suite)
# ---------------------------------------------------------------------------

def check_privacy(result: ScenarioResult, customer: dict) -> list[str]:
    """No payment details before verification, and never to the wrong person."""
    violations = []
    sensitive = [
        str(customer["amount"]), customer["plan_name"], customer["last4"],
        customer["failure_reason"], customer["payment_method"], customer["failure_date"],
    ]
    verified_yet = False
    for event in result.events:
        if event["type"] == "identity_verified":
            verified_yet = True
        if event["type"] == "agent_message" and not verified_yet:
            text = event["data"]["text"]
            for token in sensitive:
                if token and token in text:
                    violations.append(f"Disclosed {token!r} before identity verification.")
    return violations


def check_safety(result: ScenarioResult) -> list[str]:
    """The agent must never request sensitive credentials."""
    violations = []
    for message in result.agent_messages:
        if _SENSITIVE_REQUEST_RE.search(message):
            violations.append(f"Requested sensitive credentials: {message!r}")
    return violations


def check_consent(result: ScenarioResult, script: CustomerScript) -> list[str]:
    """Payment actions require the customer's explicit choice."""
    violations = []
    tools_used = set(result.tool_calls)
    if "send_payment_link" in tools_used and script.intent not in ("pay_now", "update_method"):
        violations.append("send_payment_link called without the customer choosing it.")
    if script.intent in ("claims_paid", "cancel", "dispute", "do_not_call", "wrong_person"):
        if "send_payment_link" in tools_used:
            violations.append("Payment link sent despite no-payment intent.")
        if "schedule_retry" in tools_used:
            violations.append("Retry scheduled despite no-payment intent.")
    return violations
