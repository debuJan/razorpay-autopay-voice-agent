# Agent Behavior

Asha's behavior per customer situation. Safety rules are never weakened:
no OTP/CVV/UPI PIN/password/card-number requests, no payment details
before identity verification, no pressure, honest AI identity.

| Situation | Action |
|---|---|
| Customer agrees to pay | Send payment link |
| Wants to update card | Send update-method link |
| Wants to pay later | Schedule retry |
| Says already paid | Record claim, no payment |
| Disputes payment | Human escalation |
| Financial hardship | Human escalation / later retry |
| Wrong person | No disclosure |
| Verification failure | End call |
| Scam concern | Safety response |
| Do not call | End immediately |

Notes:

- The payment link is always a **mock/demo link**; the customer must
  explicitly choose it before any link is sent.
- Identity verification allows exactly two attempts, then the call ends
  without disclosing payment details.
- A wrong-person answer ends the call with a neutral message — no
  payment, amount, merchant service, or reason is revealed.
- "Stop calling me" (or equivalent) ends the call immediately with a
  `do_not_call` outcome; no further options are offered.
- Scam concerns are answered honestly ("Yes, I'm an AI assistant") with
  an explicit warning never to share OTP/PIN/CVV/password/card numbers,
  and the customer is pointed to the merchant's official app/support.
