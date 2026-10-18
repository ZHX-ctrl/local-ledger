"""Small, strict money helpers used by the ledger service.

The public API uses decimal strings with exactly two fractional digits while
the database stores integer cents.  Rates are deliberately kept as Decimal
values until the final conversion so that the rounding rule is explicit.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


_AMOUNT_RE = re.compile(r"^-?[0-9]+\.[0-9]{2}$")
_POSITIVE_AMOUNT_RE = re.compile(r"^[0-9]+\.[0-9]{2}$")


class MoneyError(ValueError):
    """Raised when an amount or rate cannot be represented safely."""


def parse_amount(value: object, *, signed: bool = False) -> int:
    """Parse a two-decimal interface amount into integer cents.

    Scientific notation, a decimal comma, and values with more than two
    fractional digits are rejected rather than silently rounded.
    """

    if not isinstance(value, str):
        raise MoneyError("amount must be a string")
    # Bound the input before Decimal/int conversion so a hostile request
    # cannot force an unbounded integer allocation.
    if len(value) > 32:
        raise MoneyError("amount is too large")
    pattern = _AMOUNT_RE if signed else _POSITIVE_AMOUNT_RE
    if not pattern.fullmatch(value):
        raise MoneyError("amount must contain exactly two decimal places")
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:  # pragma: no cover - regex already filters it
        raise MoneyError("invalid amount") from exc
    if not dec.is_finite():
        raise MoneyError("amount must be finite")
    cents = int(dec * 100)
    # SQLite INTEGER is signed 64-bit.  Leave a small margin for arithmetic
    # performed while computing wallet balances.
    if abs(cents) > 9_000_000_000_000_000_000:
        raise MoneyError("amount is too large")
    if not signed and cents <= 0:
        raise MoneyError("amount must be positive")
    return cents


def parse_rate(value: object) -> Decimal:
    """Return a positive finite Decimal rate."""

    if isinstance(value, Decimal):
        rate = value
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        if isinstance(value, str) and len(value) > 64:
            raise MoneyError("exchange rate is too large")
        try:
            rate = Decimal(str(value))
        except InvalidOperation as exc:
            raise MoneyError("invalid exchange rate") from exc
    elif isinstance(value, float):
        # A float is accepted for injected test resolvers, but converting via
        # str avoids exposing its binary representation to the stored quote.
        try:
            rate = Decimal(str(value))
        except InvalidOperation as exc:
            raise MoneyError("invalid exchange rate") from exc
    else:
        raise MoneyError("invalid exchange rate")
    if not rate.is_finite() or rate <= 0:
        raise MoneyError("exchange rate must be positive and finite")
    return rate


def format_cents(value: int | None) -> str | None:
    """Format integer cents for the public interface."""

    if value is None:
        return None
    cents = int(value)
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    return f"{sign}{whole}.{fraction:02d}"


def rate_text(value: Decimal) -> str:
    """Produce a stable, non-scientific rate representation."""

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def convert_cents(amount_cents: int, rate: Decimal | str | int | float) -> int:
    """Convert an amount in SGD cents to CNY cents using HALF_UP rounding."""

    dec_rate = parse_rate(rate)
    converted = (Decimal(amount_cents) * dec_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    result = int(converted)
    if abs(result) > 9_000_000_000_000_000_000:
        raise MoneyError("converted amount is too large")
    return result
