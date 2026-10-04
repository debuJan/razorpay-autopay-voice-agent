# Limitations

1. All customer data is fictional.
2. Payment links are simulated.
3. No real payment is processed.
4. Only an authorized demo number is called.
5. Human transfer is simulated unless explicitly configured.
6. Production deployment would require proper telecom and payment compliance review.
7. Production deployment would require stronger authentication and authorization.
8. Production deployment would require proper data retention/privacy controls.
9. Production deployment would require monitoring and fraud controls.
10. Calling rules may vary by jurisdiction and merchant use case.
11. `scripts/evaluate.py` evaluates the deterministic backend/policy/tool
    layer (scripted scenario engine + real tools). It does NOT evaluate the
    LLM's actual conversational behavior on a live call — that must be
    reviewed from real call recordings, transcripts, and call traces.
12. The Vapi webhook (`POST /webhooks/vapi`) is authenticated with a shared
    secret (`VAPI_WEBHOOK_SECRET`, sent as the `X-Webhook-Secret` header).
    If the secret is not configured, the webhook accepts unauthenticated
    requests — acceptable for local demos only, never for anything
    internet-facing.

This prototype is a take-home demonstration. It is not production-compliant,
and no claim is made that it satisfies telecom, payment, or data-protection
regulations in any jurisdiction.
