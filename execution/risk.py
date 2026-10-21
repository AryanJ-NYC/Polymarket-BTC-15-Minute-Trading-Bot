"""Shared bankroll limits and settlement-model staking."""

from decimal import Decimal


def kelly_fraction(probability: Decimal, unit_cost: Decimal) -> Decimal:
    """Quarter Kelly for a $1 binary payoff, capped by the normal entry limit."""
    if not probability.is_finite() or not 0 <= probability <= 1:
        raise ValueError("Settlement probability must be finite and between zero and one")
    if not unit_cost.is_finite() or unit_cost <= 0:
        raise ValueError("Unit cost must be finite and positive")
    if unit_cost >= 1 or probability <= unit_cost:
        return Decimal("0")
    return min(
        ENTRY_BANKROLL_FRACTION, KELLY_MULTIPLIER * (probability - unit_cost) / (1 - unit_cost)
    )


KELLY_MULTIPLIER = Decimal("0.25")
ENTRY_BANKROLL_FRACTION = Decimal("0.02")
MAX_EXPOSURE_FRACTION = Decimal("0.10")
DAILY_LOSS_FRACTION = Decimal("0.10")
