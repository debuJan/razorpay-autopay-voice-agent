"""Mock/demo payment-link helpers. No real payment URL is ever created."""


def is_valid_channel(channel: str) -> bool:
    return channel == "sms"


def is_valid_purpose(purpose: str) -> bool:
    return purpose in ("pay_now", "update_method")


def make_demo_payment_link(customer_id: str, purpose: str, link_id: str) -> dict:
    """Build a clearly fake, non-routable demo payment link.

    The `.invalid` TLD (RFC 2606) guarantees the URL can never resolve,
    so this can never be mistaken for a real payment link.
    """
    return {
        "sent": True,
        "demo": True,
        "payment_link_id": link_id,
        "payment_url": f"https://pay-demo.invalid/recover/{customer_id}/{purpose}/{link_id}",
    }
