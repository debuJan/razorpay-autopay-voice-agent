"""Browser web-call support for Asha (no phone number needed).

Why this exists: a call started from the Vapi dashboard has no internal CALL-XXXX
record, so every tool call is rejected ("Unknown call") and the assistant has no
customer context. This module starts a call the same way /calls/start does:

1. POST /calls/start-web  -> creates the internal call record bound to ONE customer
   and returns the per-customer assistant overrides (prompt, tools, first message).
2. The browser page (GET /webcall) starts the Vapi web call with those overrides.
3. POST /calls/{call_id}/attach -> links Vapi's call id to our internal record.

Tool calls then flow through the SAME authorization path as phone calls
(customer binding + verified-gate). Only the transport differs.

Wiring (in app/main.py):
    from .webcall import router as webcall_router
    app.include_router(webcall_router)
"""
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import vapi
from .config import default_db_path, get_settings
from .customers import get_customer, public_customer
from .database import get_connection, record_event, utcnow

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebCallStartRequest(BaseModel):
    customer_id: str = Field(..., examples=["CUST001"])


class AttachRequest(BaseModel):
    provider_call_id: str = Field(..., min_length=1)


def _public_key() -> str:
    # Vapi's *public* key is designed to be used in browsers. Never put the
    # private API key (VAPI_API_KEY) in a page.
    return os.environ.get("VAPI_PUBLIC_KEY", "").strip()


@router.get("/webcall", include_in_schema=False)
def webcall_page():
    return FileResponse(STATIC_DIR / "webcall.html", media_type="text/html")


@router.post("/calls/start-web")
def start_web_call(req: WebCallStartRequest):
    settings = get_settings()
    db_path = default_db_path()

    customer = get_customer(db_path, req.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")

    missing = []
    if not _public_key():
        missing.append("VAPI_PUBLIC_KEY")
    if not settings.vapi_assistant_id:
        missing.append("VAPI_ASSISTANT_ID")
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Missing configuration for web calls: {', '.join(missing)}. See .env.example.",
        )

    call_id = f"CALL-{uuid.uuid4().hex[:8].upper()}"

    # public_customer() strips verification_suffix; it must never reach the browser.
    overrides = vapi.build_assistant_overrides(public_customer(customer), settings)
    overrides["metadata"] = {**overrides.get("metadata", {}), "internal_call_id": call_id}
    secret = customer.get("verification_suffix", "")
    if hasattr(vapi, "assert_no_secret_leak"):
        vapi.assert_no_secret_leak(overrides, secret)

    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO calls (call_id, customer_id, provider_call_id, started_at)"
            " VALUES (?, ?, ?, ?)",
            (call_id, customer["customer_id"], None, utcnow()),
        )
        conn.commit()
        record_event(conn, call_id, "call_started",
                     {"customer_id": customer["customer_id"], "transport": "web"})
    finally:
        conn.close()

    return {
        "call_id": call_id,
        "customer_id": customer["customer_id"],
        "public_key": _public_key(),
        "assistant_id": settings.vapi_assistant_id,
        "assistant_overrides": overrides,
    }


@router.post("/calls/{call_id}/attach")
def attach_provider_call(call_id: str, req: AttachRequest):
    """Link the Vapi call id to the internal record (only if not yet linked)."""
    conn = get_connection(default_db_path())
    try:
        row = conn.execute(
            "SELECT provider_call_id FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        if row["provider_call_id"] and row["provider_call_id"] != req.provider_call_id:
            raise HTTPException(status_code=409, detail="Call already linked to a provider call.")
        conn.execute(
            "UPDATE calls SET provider_call_id = ? WHERE call_id = ?",
            (req.provider_call_id, call_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"call_id": call_id, "provider_call_id": req.provider_call_id}


def resolve_internal_call_for_message(db_path: str, message: dict) -> dict | None:
    """Drop-in upgrade of main._resolve_internal_call that takes the whole webhook message.

    1. Look up by Vapi call id (normal path, phone calls and attached web calls).
    2. Fallback: use metadata.internal_call_id from the call's assistant overrides,
       so a tool call that arrives before /attach still resolves. On success the
       provider id is backfilled so later lookups take path 1.
    """
    call = (message or {}).get("call") or {}
    provider_call_id = call.get("id")

    conn = get_connection(db_path)
    try:
        if provider_call_id:
            row = conn.execute(
                "SELECT call_id, customer_id, verified FROM calls WHERE provider_call_id = ?",
                (provider_call_id,),
            ).fetchone()
            if row:
                return dict(row)

        meta = (call.get("metadata") or {}) or (
            (call.get("assistantOverrides") or {}).get("metadata") or {}
        )
        internal_id = meta.get("internal_call_id") if isinstance(meta, dict) else None
        if not internal_id:
            return None
        row = conn.execute(
            "SELECT call_id, customer_id, verified, provider_call_id FROM calls WHERE call_id = ?",
            (internal_id,),
        ).fetchone()
        if row is None:
            return None
        if provider_call_id and not row["provider_call_id"]:
            conn.execute(
                "UPDATE calls SET provider_call_id = ? WHERE call_id = ?",
                (provider_call_id, internal_id),
            )
            conn.commit()
        return {"call_id": row["call_id"], "customer_id": row["customer_id"],
                "verified": row["verified"]}
    finally:
        conn.close()


def bind_call_customer(args: dict, internal: dict) -> dict:
    """Force the tool's customer_id to the customer this call is bound to.

    The model is never told a customer_id (the prompt only has name/merchant/amount),
    so letting it supply one means it guesses, and every tool call then fails with
    "Unknown customer_id" or a call/customer mismatch. The server already knows who the
    call is for, so it decides. This is also safer: the LLM cannot pick another customer.
    """
    return {**(args or {}), "customer_id": internal["customer_id"]}
