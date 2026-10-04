# Demo Recording Guide

Record three short demos. Each demo shows **both** the voice conversation
and the backend/tool outcome (screen-share the terminal running the API or
the mock/evaluate output alongside the call audio).

Use only a phone number you own or have explicit permission to call
(`DEMO_PHONE_NUMBER` in `.env`). Keep each call under ~5 minutes.

## Setup before recording

1. Fill in `.env` (copy from `.env.example`): `VAPI_API_KEY`,
   `VAPI_ASSISTANT_ID`, `VAPI_PHONE_NUMBER_ID`, `DEMO_PHONE_NUMBER`,
   and `PUBLIC_BASE_URL` (e.g. an ngrok tunnel to your local API).
2. Seed the database: `python scripts/seed.py`
3. Start the API: `uvicorn app.main:app --reload`

## Demo 1 — Successful recovery

1. `python scripts/demo.py --live CUST001`
2. Answer, confirm you are Rohan Mehta, verify with code **380**.
3. Say: "Yes, send me the payment link."
4. Show: `send_payment_link()` result and the `payment_link_sent` outcome
   via `GET /calls/{call_id}/trace`.

## Demo 2 — Financial hardship

1. `python scripts/demo.py --live CUST008`
2. Answer, confirm you are Divya Nair, verify with code **593**.
3. Say you cannot afford the payment right now.
4. Show: no pressure, `transfer_to_human(reason="hardship")`, and the
   `hardship` outcome in the trace.

## Demo 3 — Wrong person

1. `python scripts/demo.py --live CUST009`
2. Answer as someone else ("No, he's not here").
3. Show: Asha reveals nothing about the payment, and the trace records
   `wrong_person`.

## No-phone alternative

If you cannot place calls while recording, demonstrate with mock mode:

- `python scripts/demo.py --mock` — full Scenario 1 walkthrough, no call.
- `python scripts/evaluate.py` — all 10 scenarios executed with PASS/FAIL.
