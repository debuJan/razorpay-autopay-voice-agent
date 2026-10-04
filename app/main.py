"""FastAPI backend for the Autopay Recovery Voice Agent."""
import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import tools, vapi
from .config import default_db_path, get_settings
from .customers import get_customer, list_customers, public_customer
from .database import get_connection, init_db, record_event, utcnow
from .models import (
    CallRecord,
    CallStartRequest,
    CallStartResponse,
    CallTrace,
    CustomerPublic,
    TraceEvent,
)
from .policies import authorize_call, dial_number
from .webcall import (
    bind_call_customer,
    resolve_internal_call_for_message,
    router as webcall_router,
)

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_HEADER = "X-Webhook-Secret"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Autopay Recovery Voice Agent", version="1.0.0", lifespan=lifespan)
app.include_router(webcall_router)


def _db() -> str:
    return default_db_path()


# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/customers", response_model=list[CustomerPublic])
def get_customers():
    return [public_customer(c) for c in list_customers(_db())]


@app.get("/customers/{customer_id}", response_model=CustomerPublic)
def get_customer_by_id(customer_id: str):
    customer = get_customer(_db(), customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return public_customer(customer)


@app.post("/calls/start", response_model=CallStartResponse)
def start_call(req: CallStartRequest):
    settings = get_settings()
    customer = get_customer(_db(), req.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    ok, reason = authorize_call(settings)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Call not authorized: {reason}")

    call_id = f"CALL-{uuid.uuid4().hex[:8].upper()}"
    dialed = dial_number(settings)  # always the demo number

    try:
        provider = vapi.create_outbound_call(public_customer(customer), settings)
    except vapi.VapiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    conn = get_connection(_db())
    try:
        conn.execute(
            "INSERT INTO calls (call_id, customer_id, provider_call_id, started_at)"
            " VALUES (?, ?, ?, ?)",
            (call_id, customer["customer_id"], provider["provider_call_id"], utcnow()),
        )
        conn.commit()
        record_event(conn, call_id, "call_started",
                     {"customer_id": customer["customer_id"],
                      "provider_call_id": provider["provider_call_id"]})
    finally:
        conn.close()

    return CallStartResponse(
        call_id=call_id,
        customer_id=customer["customer_id"],
        provider_call_id=provider["provider_call_id"],
        status=provider["status"],
        dialed_number=dialed,
    )


@app.get("/calls", response_model=list[CallRecord])
def get_calls():
    conn = get_connection(_db())
    try:
        rows = conn.execute("SELECT * FROM calls ORDER BY started_at DESC").fetchall()
    finally:
        conn.close()
    return [CallRecord(**dict(r)) for r in rows]


@app.get("/calls/{call_id}", response_model=CallRecord)
def get_call(call_id: str):
    conn = get_connection(_db())
    try:
        row = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    return CallRecord(**dict(row))


@app.get("/calls/{call_id}/trace", response_model=CallTrace)
def get_call_trace(call_id: str):
    conn = get_connection(_db())
    try:
        call = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        event_rows = conn.execute(
            "SELECT type, data, timestamp FROM call_events WHERE call_id = ? ORDER BY id",
            (call_id,),
        ).fetchall()
        tool_rows = conn.execute(
            "SELECT tool_name, arguments, result, timestamp FROM tool_calls"
            " WHERE call_id = ? ORDER BY id",
            (call_id,),
        ).fetchall()
        outcome_rows = conn.execute(
            "SELECT outcome, notes, created_at FROM outcomes WHERE call_id = ? ORDER BY id",
            (call_id,),
        ).fetchall()
    finally:
        conn.close()

    events: list[TraceEvent] = []
    for r in event_rows:
        data = json.loads(r["data"]) if r["data"] else {}
        if r["type"] == "agent_message":
            continue  # agent utterances are internal; trace keeps structural events
        events.append(TraceEvent(type=r["type"], data=data, timestamp=r["timestamp"]))
    for r in tool_rows:
        events.append(TraceEvent(
            type="tool_call",
            data={"tool": r["tool_name"], "arguments": json.loads(r["arguments"])},
            timestamp=r["timestamp"],
        ))
        events.append(TraceEvent(
            type="tool_result",
            data={"tool": r["tool_name"], "result": json.loads(r["result"]), "success": True},
            timestamp=r["timestamp"],
        ))
    for r in outcome_rows:
        events.append(TraceEvent(
            type="outcome",
            data={"value": r["outcome"], "notes": r["notes"]},
            timestamp=r["created_at"],
        ))

    # Chronological order. sort() is stable, so a tool_call stays before its
    # tool_result when they share a timestamp.
    events.sort(key=lambda e: e.timestamp)

    return CallTrace(call_id=call_id, customer_id=call["customer_id"], events=events)


# ---------------------------------------------------------------------------
def _check_webhook_auth(request: Request) -> None:
    """Authenticate the Vapi webhook with a shared secret.

    Compares the `X-Webhook-Secret` header against `VAPI_WEBHOOK_SECRET`
    using a constant-time comparison. Rejects with 401 when the secret is
    configured but the header is missing or wrong.

    If VAPI_WEBHOOK_SECRET is not configured at all, the webhook is left
    open (development convenience only) and a warning is logged — see
    docs/limitations.md. Configure the secret for anything beyond local demos.
    """
    expected = get_settings().vapi_webhook_secret
    if not expected:
        logger.warning(
            "VAPI_WEBHOOK_SECRET is not configured; the Vapi webhook accepts "
            "unauthenticated requests. Set it before any non-local use."
        )
        return
    provided = request.headers.get(WEBHOOK_SECRET_HEADER, "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")


@app.post("/webhooks/vapi")
async def vapi_webhook(request: Request):
    """Handle Vapi webhooks: tool calls and end-of-call reports.

    Tool calls execute against the real backend tools and return results to Vapi.
    End-of-call reports finalize the call record and log the outcome.
    """
    _check_webhook_auth(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    message = payload.get("message", {}) if isinstance(payload, dict) else {}
    msg_type = message.get("type", "")

    if msg_type == "tool-calls":
        return _handle_tool_calls(message)
    if msg_type == "end-of-call-report":
        return _handle_end_of_call_report(message)
    # status-update and anything else: acknowledge
    return JSONResponse({"received": True, "type": msg_type or "unknown"})


_TOOL_HANDLERS = {
    "verify_identity": tools.verify_identity,
    "send_payment_link": tools.send_payment_link,
    "schedule_retry": tools.schedule_retry,
    "transfer_to_human": tools.transfer_to_human,
    "log_outcome": tools.log_outcome,
}


def _single_line_json(payload: dict) -> str:
    """Tool results returned to Vapi must be single-line JSON strings."""
    return json.dumps(payload, separators=(",", ":"))


def _handle_tool_calls(message: dict):
    db_path = _db()
    results = []

    # Current Vapi format uses `toolCallList`; fall back to the older `toolCalls`.
    tool_calls = message.get("toolCallList") or message.get("toolCalls") or []

    provider_call_id = (message.get("call") or {}).get("id")
    internal = resolve_internal_call_for_message(db_path, message)
    internal_call_id = internal["call_id"] if internal else None

    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id")
        fn = tool_call.get("function") or {}
        name = fn.get("name", "")
        args = fn.get("arguments", {}) or {}

        # Vapi may deliver arguments as a JSON string — parse before dispatching.
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                results.append({
                    "toolCallId": tool_call_id,
                    "result": _single_line_json({"error": "Tool arguments are not valid JSON."}),
                })
                continue

        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            results.append({
                "toolCallId": tool_call_id,
                "result": _single_line_json({"error": f"Unknown tool: {name}"}),
            })
            continue
        if internal is None:
            results.append({
                "toolCallId": tool_call_id,
                "result": _single_line_json(
                    {"error": f"Unknown call: no internal record for provider call {provider_call_id!r}."}
                ),
            })
            continue

        # The server decides which customer this call is for, never the model.
        args = bind_call_customer(args, internal)
        try:
            # Map webhook args onto tool signatures (retry uses retry_date).
            if name == "schedule_retry" and "date" in args and "retry_date" not in args:
                args = {**args, "retry_date": args.pop("date")}
            # The internal CALL-XXXX id (never the provider id) is threaded
            # through so tool records, traces, and outcomes all line up.
            out = handler(db_path, call_id=internal_call_id, **args)
            results.append({"toolCallId": tool_call_id, "result": _single_line_json(out)})
        except tools.ToolError as exc:
            results.append({
                "toolCallId": tool_call_id,
                "result": _single_line_json({"error": str(exc)}),
            })
        except TypeError as exc:
            results.append({
                "toolCallId": tool_call_id,
                "result": _single_line_json({"error": f"Bad arguments: {exc}"}),
            })

    # Results only (never the spoken code): makes tool errors visible while debugging.
    for r in results:
        logger.warning("tool result -> %s", r["result"])
    return JSONResponse({"results": results})


def _handle_end_of_call_report(message: dict):
    db_path = _db()
    summary = message.get("summary", "")
    ended_reason = message.get("endedReason", "")

    internal = resolve_internal_call_for_message(db_path, message)
    if internal is None:
        return JSONResponse({"received": True, "matched": False})
    call_id, customer_id = internal["call_id"], internal["customer_id"]

    conn = get_connection(db_path)
    try:
        outcome_row = conn.execute(
            "SELECT outcome FROM outcomes WHERE call_id = ? ORDER BY id DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        outcome = outcome_row["outcome"] if outcome_row else "no_resolution"
        if not outcome_row:
            conn.execute(
                "INSERT INTO outcomes (call_id, customer_id, outcome, notes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (call_id, customer_id, "no_resolution",
                 f"Call ended ({ended_reason}) without a logged outcome.", utcnow()),
            )
        conn.execute(
            "UPDATE calls SET ended_at = ?, outcome = ?, summary = ? WHERE call_id = ?",
            (utcnow(), outcome, summary, call_id),
        )
        conn.commit()
        record_event(conn, call_id, "call_ended",
                     {"ended_reason": ended_reason, "outcome": outcome})
    finally:
        conn.close()
    return JSONResponse({"received": True, "matched": True, "outcome": outcome})