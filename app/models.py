"""Pydantic request/response models for the FastAPI layer."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class CustomerPublic(BaseModel):
    """Customer record safe for API responses — verification_suffix is NEVER exposed."""

    customer_id: str
    customer_name: str
    phone: str
    merchant_name: str
    plan_name: str
    amount: int
    currency: str
    failure_reason: str
    failure_date: str
    payment_method: str
    last4: str
    language: str
    attempt_no: int


class CallStartRequest(BaseModel):
    customer_id: str = Field(..., examples=["CUST001"])


class CallStartResponse(BaseModel):
    call_id: str
    customer_id: str
    provider_call_id: Optional[str] = None
    status: str
    dialed_number: str


class CallRecord(BaseModel):
    call_id: str
    customer_id: str
    provider_call_id: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None
    duration: Optional[int] = None
    outcome: Optional[str] = None
    summary: Optional[str] = None


class TraceEvent(BaseModel):
    type: str
    data: dict[str, Any] = {}
    timestamp: str


class CallTrace(BaseModel):
    call_id: str
    customer_id: str
    events: list[TraceEvent]


class ToolCallRecord(BaseModel):
    id: int
    call_id: Optional[str] = None
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    timestamp: str
