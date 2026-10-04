"""Vapi integration: outbound call creation, assistant configuration, tool definitions.

Uses the real Vapi REST API (https://api.vapi.ai). The verification_suffix is
never included in any payload sent to Vapi — asserted by assert_no_secret_leak
and covered by tests.
"""
import requests

from .config import Settings
from .prompts import render_first_message, render_system_prompt

VAPI_CALLS_URL = "https://api.vapi.ai/call"
VAPI_ASSISTANTS_URL = "https://api.vapi.ai/assistant"


class VapiError(Exception):
    pass


def assert_no_secret_leak(payload: object, secret: str) -> None:
    """Fail loudly if the server-side verification value appears in a payload."""
    import json

    text = json.dumps(payload, default=str)
    if secret and secret in text:
        raise VapiError("Refusing to send: payload contains the verification secret.")


def _server_url(settings: Settings) -> str:
    """Public URL Vapi calls back for tools/webhooks.

    Fails loudly when PUBLIC_BASE_URL is missing — live Vapi functionality
    cannot work without it (Vapi would have nowhere to send tool calls or
    end-of-call reports).
    """
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        raise VapiError(
            "PUBLIC_BASE_URL is not configured. Live Vapi functionality requires it "
            "(e.g. https://your-tunnel.ngrok.io) so Vapi can reach "
            "/webhooks/vapi for tool calls and end-of-call reports. "
            "See .env.example."
        )
    return base + "/webhooks/vapi"


def _server_config(settings: Settings) -> dict:
    """Server block Vapi uses when calling our webhook, incl. the shared-secret header."""
    server = {"url": _server_url(settings)}
    secret = (getattr(settings, "vapi_webhook_secret", "") or "").strip()
    if secret:
        server["headers"] = {"X-Webhook-Secret": secret}
    return server


def build_tool_definitions(settings: Settings) -> list[dict]:
    """Vapi function-tool definitions. Tools execute against our webhook."""
    base = {"type": "function", "server": _server_config(settings)}

    def tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
        return {
            **base,
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    return [
        tool(
            "verify_identity",
            "Verify the caller's identity using the 3-digit code they spoke. "
            "The expected value is checked server-side and is never revealed.",
            {
                "customer_id": {"type": "string"},
                "answer": {"type": "string", "description": "The 3-digit code the caller spoke."},
            },
            ["customer_id", "answer"],
        ),
        tool(
            "send_payment_link",
            "Send a secure DEMO payment link by SMS. Only call after the customer "
            "explicitly chooses to pay now or update their payment method. "
            "The link is a mock and processes no real payment.",
            {
                "customer_id": {"type": "string"},
                "channel": {"type": "string", "enum": ["sms"]},
                "purpose": {"type": "string", "enum": ["pay_now", "update_method"]},
            },
            ["customer_id", "channel", "purpose"],
        ),
        tool(
            "schedule_retry",
            "Schedule an autopay retry for a date within the next 7 days (YYYY-MM-DD).",
            {
                "customer_id": {"type": "string"},
                "date": {"type": "string", "description": "Retry date as YYYY-MM-DD."},
            },
            ["customer_id", "date"],
        ),
        tool(
            "transfer_to_human",
            "Escalate to a human agent (simulated unless a real transfer is configured).",
            {
                "customer_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            ["customer_id", "reason"],
        ),
        tool(
            "log_outcome",
            "Record the final outcome of the call. Call exactly once at the end.",
            {
                "customer_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": [
                        "payment_link_sent", "method_update_sent", "retry_scheduled",
                        "claims_paid", "cancel_requested", "hardship", "dispute",
                        "wrong_person", "verification_failed", "callback_requested",
                        "do_not_call", "escalated", "no_resolution",
                    ],
                },
                "notes": {"type": "string"},
            },
            ["customer_id", "outcome"],
        ),
    ]


def build_assistant_overrides(customer: dict, settings: Settings) -> dict:
    """Per-call assistant overrides: rendered prompt + first message + voice.

    Customer context is included; verification_suffix is never included.
    """
    
    return {
        "name": "Asha",
        "firstMessage": render_first_message(customer),
        # Vapi posts server messages (tool calls, end-of-call reports) here.
        "server": _server_config(settings),
        "serverMessages": [
            "tool-calls",        # required: backend tool execution
            "end-of-call-report",  # required: outcome + call trace finalization
            "status-update",     # useful: call lifecycle visibility
        ],
        "model": {
            "provider": "openai",
            "model": "gpt-4o",
            "messages": [{"role": "system", "content": render_system_prompt(customer)}],
            "tools": build_tool_definitions(settings),
        },
        "voice": {
            "provider": settings.vapi_voice_provider or "11labs",
            **({"voiceId": settings.vapi_voice_id} if settings.vapi_voice_id else {}),
        },
        # Multilingual: English / Hindi / Hinglish. Start in the customer's language.
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
            "language": "multi",
        },
        "endCallMessage": "Thank you for your time. Goodbye!",
        "maxDurationSeconds": 300,  # keep calls under ~5 minutes
    }


def create_assistant(settings: Settings) -> dict:
    """Create the Asha assistant in Vapi (run once; store the returned id)."""
    _require(settings, ["vapi_api_key"])
    payload = {
        "name": "Asha",
        "firstMessage": "Hello, may I speak with {{customer_name}}?",
        "model": {
            "provider": "openai",
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "system",
                    # Generic template: per-call overrides inject the real context.
                    "content": SYSTEM_PROMPT_GENERIC,
                }
            ],
            "tools": build_tool_definitions(settings),
        },
    }
    resp = requests.post(
        VAPI_ASSISTANTS_URL,
        headers=_headers(settings),
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise VapiError(f"Vapi assistant creation failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def create_outbound_call(customer: dict, settings: Settings) -> dict:
    """Start a real outbound call via Vapi to the authorized demo number only.

    The customer's fictional phone number is NEVER dialed; only the configured
    DEMO_PHONE_NUMBER may be called.
    """
    _require(settings, ["vapi_api_key", "vapi_assistant_id", "vapi_phone_number_id",
                        "demo_phone_number"])
    from .policies import dial_number

    number = dial_number(settings)  # always the demo number; raises if missing
    overrides = build_assistant_overrides(customer, settings)
    assert_no_secret_leak(overrides, customer.get("verification_suffix", ""))

    payload = {
        "assistantId": settings.vapi_assistant_id,
        "phoneNumberId": settings.vapi_phone_number_id,
        "customer": {"number": number},
        "assistantOverrides": overrides,
    }
    resp = requests.post(VAPI_CALLS_URL, headers=_headers(settings), json=payload, timeout=30)
    if resp.status_code >= 400:
        raise VapiError(f"Vapi call failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    return {"provider_call_id": data.get("id"), "status": data.get("status", "started")}


def _require(settings: Settings, fields: list[str]) -> None:
    missing = [f for f in fields if not getattr(settings, f, "")]
    if missing:
        raise VapiError(
            f"Missing Vapi configuration: {', '.join(missing)}. See .env.example."
        )


def _headers(settings: Settings) -> dict:
    return {"Authorization": f"Bearer {settings.vapi_api_key}", "Content-Type": "application/json"}


# Generic (unrendered) system prompt used when creating the assistant object itself.
# Per-call overrides replace this with the rendered, customer-specific prompt.
SYSTEM_PROMPT_GENERIC = (
    "You are Asha, an AI payment assistance agent. You help customers resolve "
    "failed autopay payments. Full instructions, customer context, and safety "
    "rules are provided per call via assistant overrides. Never request OTP, "
    "CVV, UPI PIN, passwords, or card numbers. Always identify yourself as an AI."
)
