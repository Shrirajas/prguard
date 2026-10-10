"""Tiny in-memory payments ledger used as PRGuard's demo target."""

_BALANCES: dict[str, int] = {}


class PaymentError(Exception):
    """Raised when a charge or refund cannot be applied."""


def charge_customer(customer_id: str, amount_cents: int) -> int:
    """Charge a customer and return their total charged amount in cents."""
    if amount_cents <= 0:
        raise PaymentError("amount must be positive")
    _BALANCES[customer_id] = _BALANCES.get(customer_id, 0) + amount_cents
    return _BALANCES[customer_id]


def refund(customer_id: str) -> int:
    """Refund everything a customer was charged and return the refunded amount."""
    _BALANCES[customer_id] = 0
    return _BALANCES[customer_id]
