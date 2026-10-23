"""Shared bankroll fractions for entry, exposure and daily loss budgets."""

from decimal import Decimal


def kelly_fraction(probability: Decimal, unit_cost: Decimal) -> Decimal:
    """Quarter Kelly for a settlement-only $1/zero payoff, capped at 4%."""
    if not probability.is_finite() or not 0 <= probability <= 1:
        raise ValueError("Settlement probability must be finite and between zero and one")
    if not unit_cost.is_finite() or unit_cost <= 0:
        raise ValueError("Unit cost must be finite and positive")
    if unit_cost >= 1 or probability <= unit_cost:
        return Decimal("0")
    return min(
        KELLY_MAX_BANKROLL_FRACTION, KELLY_MULTIPLIER * (probability - unit_cost) / (1 - unit_cost)
    )


KELLY_MULTIPLIER = Decimal("0.25")
KELLY_MAX_BANKROLL_FRACTION = Decimal("0.04")
ENTRY_BANKROLL_FRACTION = Decimal("0.02")
MAX_EXPOSURE_FRACTION = Decimal("0.10")
DAILY_LOSS_FRACTION = Decimal("0.10")
