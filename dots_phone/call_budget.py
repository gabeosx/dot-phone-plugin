"""Conservative pre-dial reservation policy, shared by duration selection and checks."""

from decimal import ROUND_FLOOR, ROUND_UP, Decimal

PER_MINUTE_USD = Decimal("0.22")
CONTINGENCY_USD = Decimal("0.50")
CLOSING_SECONDS = 30


def reservation_usd(seconds: int) -> Decimal:
    return (Decimal(seconds + CLOSING_SECONDS) / 60 * PER_MINUTE_USD + CONTINGENCY_USD).quantize(
        Decimal("0.0001"), rounding=ROUND_UP
    )


def affordable_seconds(budget: Decimal, installation_seconds: int) -> int:
    seconds = (budget - CONTINGENCY_USD) * 60 / PER_MINUTE_USD - CLOSING_SECONDS
    return max(0, min(installation_seconds, int(seconds.to_integral_value(rounding=ROUND_FLOOR))))
