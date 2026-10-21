"""Shared bankroll fractions for entry, exposure and daily loss budgets."""

from decimal import Decimal

ENTRY_BANKROLL_FRACTION = Decimal("0.02")
MAX_EXPOSURE_FRACTION = Decimal("0.10")
DAILY_LOSS_FRACTION = Decimal("0.10")
