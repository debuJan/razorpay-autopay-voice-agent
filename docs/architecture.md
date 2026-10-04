# Architecture

Concise overview of the Autopay Recovery Voice Agent prototype.

## Components

- **Vapi** — voice-agent provider. Places the outbound call, runs the
  Asha assistant (speech-to-text, LLM, text-to-speech), and delivers tool
  calls + end-of-call reports to our webhook.
- **FastAPI** (`app/main.py`) — HTTP backend. Serves customer/call/trace
  reads, starts calls, and hosts the Vapi webhook.
- **Tool layer** (`app/tools.py`) — the five backend tools Asha can invoke:
  `verify_identity`, `send_payment_link`, `schedule_retry`,
  `transfer_to_human`, `log_outcome`. Every invocation is validated
  server-side and recorded in the `tool_calls` table.
- **SQLite** — `customers`, `calls`, `tool_calls`, `outcomes`, plus
  `call_events` (structural trace events).
- **Customer records** (`data/customers.json`) — 10 fictional Indian
  customers. Seeded via `scripts/seed.py`. The `verification_suffix`
  never leaves the server.
- **Webhooks** (`POST /webhooks/vapi`) — receives Vapi `tool-calls`
  (executed against the real tools) and `end-of-call-report` (finalizes
  the call record and outcome). Authenticated with the `X-Webhook-Secret`
  shared secret. Vapi provider call IDs are mapped to the internal
  `CALL-XXXX` records, so tool calls and outcomes always land on the
  right call.
- **Server-side authorization** — every tool call is bound to its call
  record: the tool's `customer_id` must match the call's customer, and
  `send_payment_link` / `schedule_retry` require a verified call.
  This is enforced in `app/tools.py`, not just in the LLM prompt.
- **Call trace** (`GET /calls/{call_id}/trace`) — replays the structured
  event log: `call_started → identity_verified → payment_explained →
  tool_call/tool_result → outcome`.
- **Business policies** (`app/policies.py`) — calling window (9 AM–8 PM),
  demo-number-only dialing, identity verification (max 2 attempts),
  and the deterministic 10-scenario call-flow engine used by the
  evaluator.
- **Mock payment system** (`app/payments.py`) — generates clearly fake
  demo links on the non-routable `.invalid` domain. No real payment
  gateway is integrated; no real payment is ever processed.

## Request flow (live call)

1. `POST /calls/start` → safety checks (demo number configured, calling
   window) → Vapi outbound call created to the **demo number only**.
2. Vapi calls back with `tool-calls`; the backend executes the real tool
   and returns the result.
3. On hangup Vapi sends `end-of-call-report`; the backend finalizes the
   call and logs the outcome.
4. `GET /calls/{call_id}/trace` exposes the full structured trace.
